# ruff: noqa: F401, S102, S307
# pyright: reportUnusedImport=false, reportPrivateUsage=false

from __future__ import annotations

import asyncio
import datetime
import gc
import importlib
import inspect
import io
import logging
import math
import os
import sys
import textwrap
import time
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass
from importlib.metadata import distribution, packages_distributions
from pathlib import Path
from typing import TYPE_CHECKING, cast

import discord
import discord.utils
import psutil
from discord.ext import commands
from jishaku.modules import package_version

from moist_bot.bot import (
    COGS_PACKAGE_NAME,
    extension_module_name,
    normalize_extension_name,
)
from moist_bot.constants import COGS_FOLDER_PATH, ROOT_PACKAGE
from moist_bot.utils.formats import format_file_list, format_process_error
from moist_bot.utils.process import run_git, run_process

if TYPE_CHECKING:
    from typing import Any

    from moist_bot.bot import MoistBot
    from moist_bot.cogs.stats import Stats
    from moist_bot.utils.context import Context


log = logging.getLogger('discord.' + __name__)


PROJECT_ROOT_PATH = str(COGS_FOLDER_PATH.parents[2])
DEPENDENCY_FILES = frozenset({'pyproject.toml', 'uv.lock'})


@dataclass(frozen=True, slots=True)
class ReloadTarget:
    """A module discovered from the git diff that can be reloaded."""

    module: str
    display_name: str
    depth: int
    is_extension: bool


class Owner(commands.Cog):
    """Debug commands that only the bot owner can use."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

        # Cog
        self._last_result: Any = None
        self.process = psutil.Process()
        self.sessions: set[int] = set()
        self.last_ext: str = 'cmds'

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{HAMMER AND WRENCH}')

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[reportIncompatibleMethodOverride]
        if not await ctx.bot.is_owner(ctx.author):
            raise commands.NotOwner('You do not own this bot.')
        return True

    @staticmethod
    def cleanup_code(content: str) -> str:
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith('```') and content.endswith('```'):
            return '\n'.join(content.split('\n')[1:-1])

        # remove `foo`
        return content.strip('` \n')

    @staticmethod
    def get_syntax_error(e: SyntaxError) -> str:
        if e.text is None:
            return f'```py\n{e.__class__.__name__}: {e}\n```'
        return f'```py\n{e.text}{"^":>{e.offset}}\n{e.__class__.__name__}: {e}```'

    @commands.group(hidden=True, invoke_without_command=True)
    async def reload(self, ctx: Context, ext: str | None = None):
        """Reload a cog."""

        # If not provided, use the last extension used
        if ext is None:
            ext = self.last_ext

        try:
            await self.bot.reload_extension(ext)

        except commands.ExtensionNotLoaded, commands.ExtensionNotFound:
            return await ctx.reply(":anger: specified cog name doesn't exits bozo")

        except commands.ExtensionFailed as e:
            msg = f'Reloading raised an exception: `{type(e.__class__)}`\n'
            log.exception(msg, exc_info=e.__traceback__)  # type: ignore[]
            await ctx.reply(f':anger: {msg}\n`{e}`')
            return None

        await ctx.reply(f':repeat: Reloaded {ext}.')
        self.last_ext = ext

    @staticmethod
    def module_name_from_project_path(file: str) -> str | None:
        """Convert a project-relative Python path into an import path.

        Parameters
        ----------
        file:
            A project-relative file path from git.

        Returns
        -------
        str | None
            The import path for Python files under ``src``.
        """

        path = Path(file)
        if path.suffix != '.py':
            return None

        try:
            source_path = path.relative_to('src')
        except ValueError:
            return None

        if source_path.name == '__init__.py':
            source_path = source_path.parent
        else:
            source_path = source_path.with_suffix('')

        if not source_path.parts:
            return None

        return '.'.join(source_path.parts)

    def find_reload_targets(self, changed_files: list[str]) -> list[ReloadTarget]:
        """Find changed cog modules that can be reloaded in-process.

        Parameters
        ----------
        changed_files:
            Project-relative paths changed by the update.

        Returns
        -------
        list[ReloadTarget]
            Reloadable cog modules sorted so nested modules reload first.
        """

        cog_prefix = f'{ROOT_PACKAGE}.{COGS_PACKAGE_NAME}.'
        targets: list[ReloadTarget] = []

        for file in changed_files:
            module = self.module_name_from_project_path(file)
            if module is None or not module.startswith(cog_prefix):
                continue

            cog_module = module.removeprefix(cog_prefix)
            is_extension = '.' not in cog_module
            target_module = (
                extension_module_name(cog_module) if is_extension else module
            )
            targets.append(
                ReloadTarget(
                    module=target_module,
                    display_name=module,
                    depth=cog_module.count('.'),
                    is_extension=is_extension,
                )
            )

        targets.sort(key=lambda target: target.depth, reverse=True)
        return targets

    def is_reload_target_loaded(self, target: ReloadTarget) -> bool:
        """Return whether a target is already active in this process.

        Parameters
        ----------
        target:
            The changed module selected from the git diff.

        Returns
        -------
        bool
            Whether the target can be reloaded without loading a new extension.
        """

        if target.is_extension:
            loaded_extensions = {
                normalize_extension_name(module) for module in self.bot.extensions
            }
            return normalize_extension_name(target.module) in loaded_extensions

        return target.module in sys.modules

    @staticmethod
    def needs_restart(changed_files: list[str]) -> bool:
        """Return whether changed files require a full process restart.

        Parameters
        ----------
        changed_files:
            Project-relative paths changed by the update.

        Returns
        -------
        bool
            Whether a process restart is needed for the changes to apply.
        """

        return any(
            file in DEPENDENCY_FILES
            or (
                file.startswith('src/moist_bot/')
                and file.endswith('.py')
                and not file.startswith('src/moist_bot/cogs/')
            )
            for file in changed_files
        )

    @staticmethod
    def needs_uv_sync(changed_files: list[str]) -> bool:
        """Return whether dependency files changed during the pull.

        Parameters
        ----------
        changed_files:
            Project-relative paths changed by the update.

        Returns
        -------
        bool
            Whether ``uv sync --locked`` should be run.
        """

        return any(file in DEPENDENCY_FILES for file in changed_files)

    async def reload_target(self, target: ReloadTarget) -> None:
        """Reload a changed cog or nested cog helper module.

        Parameters
        ----------
        target:
            The changed module selected from the git diff.
        """

        if target.is_extension:
            await self.bot.reload_extension(target.module)
            return

        module = sys.modules[target.module]
        importlib.reload(module)

    @reload.command(name='all', hidden=True)
    async def reload_all(self, ctx: Context) -> None:  # noqa: PLR0911
        """Pull from git and reload changed cogs."""

        async with ctx.typing():
            # Capture the current commit before pulling
            before_status, before_stdout, before_stderr = await run_git(
                'rev-parse', 'HEAD', cwd=PROJECT_ROOT_PATH
            )
            if before_status != 0:
                await ctx.reply(
                    ':anger: Unable to read the current git commit.\n'
                    + format_process_error(
                        'git rev-parse HEAD', before_stdout, before_stderr
                    )
                )
                return

            # Keep deploys linear and avoid surprise merge commits
            pull_status, pull_stdout, pull_stderr = await run_git(
                'pull', '--ff-only', cwd=PROJECT_ROOT_PATH
            )
            if pull_status != 0:
                await ctx.reply(
                    ':anger: `git pull --ff-only` failed.\n'
                    + format_process_error(
                        'git pull --ff-only', pull_stdout, pull_stderr
                    )
                )
                return

            # Compare the new commit to the previous one
            after_status, after_stdout, after_stderr = await run_git(
                'rev-parse', 'HEAD', cwd=PROJECT_ROOT_PATH
            )
            if after_status != 0:
                await ctx.reply(
                    ':anger: Unable to read the updated git commit.\n'
                    + format_process_error(
                        'git rev-parse HEAD', after_stdout, after_stderr
                    )
                )
                return

            before_sha = before_stdout.strip()
            after_sha = after_stdout.strip()
            if before_sha == after_sha:
                output = (
                    pull_stdout.strip() or pull_stderr.strip() or 'Already up to date.'
                )
                await ctx.reply(f':white_check_mark: {output}')
                return

            # Limit reload decisions to files changed by this pull
            diff_status, diff_stdout, diff_stderr = await run_git(
                'diff',
                '--name-only',
                f'{before_sha}..{after_sha}',
                cwd=PROJECT_ROOT_PATH,
            )
            if diff_status != 0:
                await ctx.reply(
                    ':anger: Unable to inspect the updated files.\n'
                    + format_process_error(
                        f'git diff --name-only {before_sha}..{after_sha}',
                        diff_stdout,
                        diff_stderr,
                    )
                )
                return

            changed_files = [file for file in diff_stdout.splitlines() if file.strip()]
            targets = self.find_reload_targets(changed_files)
            restart_required = self.needs_restart(changed_files)

            # Update the virtual environment when dependency metadata changed
            if self.needs_uv_sync(changed_files):
                sync_status, sync_stdout, sync_stderr = await run_process(
                    'uv', 'sync', '--locked', cwd=PROJECT_ROOT_PATH
                )
                if sync_status != 0:
                    await ctx.reply(
                        ':anger: `uv sync --locked` failed after pulling updates.\n'
                        + format_process_error(
                            'uv sync --locked', sync_stdout, sync_stderr
                        )
                    )
                    return

        changed_text = format_file_list(changed_files)
        if not targets:
            message = (
                f':arrow_down: Updated '
                f'`{before_sha[:7]}` -> `{after_sha[:7]}`.\n'
                f'Changed files:\n{changed_text}\n\n'
                'No reloadable cog files changed.'
            )
            if restart_required:
                message += '\nUse `restart` for these changes to fully take effect.'
            await ctx.reply(message)
            return

        loaded_targets: list[ReloadTarget] = []
        skipped_targets: list[ReloadTarget] = []
        for target in targets:
            destination = (
                loaded_targets
                if self.is_reload_target_loaded(target)
                else skipped_targets
            )
            destination.append(target)

        if not loaded_targets:
            message = (
                f':arrow_down: Updated '
                f'`{before_sha[:7]}` -> `{after_sha[:7]}`.\n'
                f'Changed files:\n{changed_text}\n\n'
                'No already-loaded cog modules changed.'
            )
            if skipped_targets:
                skipped_text = '\n'.join(
                    f'{index}. `{target.display_name}`'
                    for index, target in enumerate(skipped_targets, start=1)
                )
                message += f'\n\nSkipped unloaded module(s):\n{skipped_text}'
            if restart_required:
                message += '\nUse `restart` for these changes to fully take effect.'
            await ctx.reply(message)
            return

        modules_text = '\n'.join(
            f'{index}. `{target.display_name}`'
            for index, target in enumerate(loaded_targets, start=1)
        )
        prompt_text = (
            f'Pulled `{before_sha[:7]}` -> `{after_sha[:7]}`.\n'
            f'This will reload the following module(s):\n{modules_text}'
        )
        if skipped_targets:
            skipped_text = '\n'.join(
                f'{index}. `{target.display_name}`'
                for index, target in enumerate(skipped_targets, start=1)
            )
            prompt_text += f'\n\nSkipping unloaded module(s):\n{skipped_text}'
        if restart_required:
            prompt_text += (
                '\n\nSome non-cog code or dependency files also changed. '
                'Reloading cogs will not apply those parts until a restart.'
            )

        confirm = await ctx.prompt(prompt_text)
        if not confirm:
            await ctx.reply('Aborting reload.')
            return

        # Reload deeper helper modules before top-level cog extensions
        statuses: list[tuple[str, str]] = []
        for target in loaded_targets:
            try:
                await self.reload_target(target)
            except KeyError, commands.ExtensionError:
                log.exception(f'Unable to reload {target.display_name}.')
                statuses.append((ctx.tick(opt=False), target.display_name))
            else:
                statuses.append((ctx.tick(opt=True), target.display_name))

        status_text = '\n'.join(f'{status}: `{module}`' for status, module in statuses)
        message = f'{status_text}\n\nChanged files:\n{changed_text}'
        if restart_required:
            message += '\n\n:warning: Use `restart` to apply non-cog changes.'

        await ctx.reply(message)

    @commands.command(hidden=True)
    async def load(self, ctx: Context, ext: str):
        """Load a cog."""

        try:
            await self.bot.load_extension(ext)

        except commands.ExtensionAlreadyLoaded, commands.ExtensionNotFound:
            await ctx.reply(
                ":anger: specified cog is already loaded or doesn't exits bozo"
            )
            return

        except commands.ExtensionFailed as e:
            msg = f'Loading raised an exception: `{type(e.__class__)}`\n'
            log.exception(msg, exc_info=e.__traceback__)  # type: ignore[]
            await ctx.reply(f':anger: {msg}\n`{e}`')
            return

        await ctx.reply(f':white_check_mark: Loaded {ext}.')

    @commands.command(hidden=True)
    async def unload(self, ctx: Context, ext: str):
        """Unload a cog."""

        try:
            await self.bot.unload_extension(ext)
        except commands.ExtensionNotFound, commands.ExtensionNotLoaded:
            await ctx.reply(":anger: specified cog name doesn't exits bozo")
            return

        await ctx.reply(f':white_check_mark: Unloaded {ext}.')

    @commands.command(hidden=True)
    async def restart(self, ctx: Context) -> None:
        """Restart the bot process."""

        await ctx.reply(':arrows_counterclockwise: Restarting...')
        log.warning(f'Restart requested by {ctx.author} ({ctx.author.id}).')
        argv = [sys.executable, *sys.argv]
        try:
            await self.bot.close()
        except asyncio.CancelledError:
            pass
        finally:
            # Closing the bot can cancel the command task before restart
            os.execv(sys.executable, argv)  # noqa: S606

    @commands.command(hidden=True, name='eval')
    async def _eval(self, ctx: Context, *, body: str):
        """Evaluates python code."""

        # I'm sorry but this was way too cool not to yoink :3
        # https://github.com/Rapptz/RoboDanny/blob/a52a212d1fff1024fb00c14b9e125071f87e0323/cogs/admin.py#L215C31-L215C31

        env = {
            'ctx': ctx,
            'self': self,
            'guild': ctx.guild,
            'author': ctx.author,
            'client': self.bot,
            'channel': ctx.channel,
            'message': ctx.message,
            '_': self._last_result,
        }

        env.update(globals())

        body = self.cleanup_code(body)
        stdout = io.StringIO()

        to_compile = f'async def func():\n{textwrap.indent(body, "  ")}'

        try:
            exec(to_compile, env)
        except Exception as e:  # noqa: BLE001
            return await ctx.send(f'```py\n{e.__class__.__name__}: {e}\n```')

        func = env['func']
        try:
            with redirect_stdout(stdout):
                ret = await func()  # type: ignore[]
        except Exception:  # noqa: BLE001
            value = stdout.getvalue()
            await ctx.send(f'```py\n{value}{traceback.format_exc()}\n```')
        else:
            value = stdout.getvalue()
            try:
                await ctx.message.add_reaction('\u2705')
            except Exception:  # noqa: BLE001, S110
                pass

            if ret is None:
                if value:
                    await ctx.send(f'```py\n{value}\n```')
            else:
                self._last_result = ret
                await ctx.send(f'```py\n{value}{ret}\n```')

    @commands.command(hidden=True)
    async def repl(self, ctx: Context):
        """Launches an interactive REPL session."""

        # This is so cool I couldn't resist qwq
        # https://github.com/Rapptz/RoboDanny/blob/a52a212d1fff1024fb00c14b9e125071f87e0323/cogs/admin.py#L262

        variables = {
            'ctx': ctx,
            'self': self,
            'bot': self.bot,
            'guild': ctx.guild,
            'client': self.bot,
            'author': ctx.author,
            'message': ctx.message,
            'channel': ctx.channel,
            '_': None,
        }

        if ctx.channel.id in self.sessions:
            await ctx.send(
                'Already running a REPL session in this channel. Exit it with `quit`.'
            )
            return

        self.sessions.add(ctx.channel.id)
        await ctx.send('Enter code to execute or evaluate. `exit()` or `quit` to exit.')

        def check(m: discord.Message):
            return (
                m.author.id == ctx.author.id
                and m.channel.id == ctx.channel.id
                and m.content.startswith('`')
            )

        while True:
            try:
                response = await self.bot.wait_for(
                    'message', check=check, timeout=10.0 * 60.0
                )
            except TimeoutError:
                await ctx.send('Exiting REPL session.')
                self.sessions.remove(ctx.channel.id)
                break

            cleaned = self.cleanup_code(response.content)

            if cleaned in {'quit', 'exit', 'exit()'}:
                await ctx.send('Exiting.')
                self.sessions.remove(ctx.channel.id)
                return

            executor = exec
            code = ''
            if cleaned.count('\n') == 0:
                # Single statement, potentially 'eval'
                try:
                    code = compile(cleaned, '<repl session>', 'eval')
                except SyntaxError:
                    pass
                else:
                    executor = eval

            if executor is exec:
                try:
                    code = compile(cleaned, '<repl session>', 'exec')
                except SyntaxError as e:
                    await ctx.send(self.get_syntax_error(e))
                    continue

            variables['message'] = response

            fmt = None
            stdout = io.StringIO()

            try:
                with redirect_stdout(stdout):
                    result = executor(code, variables)
                    if inspect.isawaitable(result):
                        result = await result
            except Exception:  # noqa: BLE001
                value = stdout.getvalue()
                fmt = f'```py\n{value}{traceback.format_exc()}\n```'
            else:
                value = stdout.getvalue()
                if result is not None:
                    fmt = f'```py\n{value}{result}\n```'
                    variables['_'] = result
                elif value:
                    fmt = f'```py\n{value}\n```'

            try:
                if fmt is not None:
                    if len(fmt) > 2000:
                        await ctx.send('Content too big to be printed.')
                    else:
                        await ctx.send(fmt)
            except discord.Forbidden:
                pass
            except discord.HTTPException as e:
                await ctx.send(f'Unexpected error: `{e}`')

    @commands.command(name='health', aliases=['about'])
    async def _bot_stats(self, ctx: Context):
        """Various bot stat monitoring tools."""

        HEALTHY = discord.Color(value=0x43B581)  # noqa: N806
        UNHEALTHY = discord.Color(value=0xF04947)  # noqa: N806
        # WARNING = discord.Color(value=0xF09E47)

        # Process stats
        process = self.process
        with process.oneshot():
            cpu_count = psutil.cpu_count() or 1
            cpu_usage = process.cpu_percent() / cpu_count
            thread_count = process.num_threads()
            memory = process.memory_full_info()
            system_memory = psutil.virtual_memory()
            pid = process.pid

            physical_memory = memory.rss / 1024**2
            unique_memory = memory.uss / 1024**2
            free_memory = system_memory.available / 1024**2

        # Message cache stats
        if self.bot._connection.max_messages:  # noqa: SLF001
            message_cache = (
                f'{len(self.bot.cached_messages)}/{self.bot._connection.max_messages}'  # noqa: SLF001
            )
        else:
            message_cache = 'Disabled'

        # Tasks stats
        all_tasks = asyncio.all_tasks(loop=self.bot.loop)
        event_tasks = [
            t for t in all_tasks if 'Client._run_event' in repr(t) and not t.done()
        ]

        future_tasks = [t for t in event_tasks if 'Future pending' in repr(t)]

        # # Distribution stats
        # Try to locate what vends the `discord` package
        distributions: list[str] = [
            dist
            for dist in packages_distributions()['discord']  # type: ignore[]
            if any(
                file.parts == ('discord', '__init__.py')  # type: ignore[]
                for file in distribution(dist).files  # type: ignore[]
            )
        ]

        if distributions:
            dist_version = f'{distributions[0]}: v{package_version(distributions[0])}'
        else:
            dist_version = f'unknown: v{discord.__version__}'

        commit_status, commit_stdout, _ = await run_git(
            'rev-parse', '--short', 'HEAD', cwd=PROJECT_ROOT_PATH
        )
        current_commit = commit_stdout.strip() if commit_status == 0 else 'unknown'

        python_version, _, _ = sys.version.partition('(')

        stats_cog = self.bot.get_cog('Stats')
        commands_run = 0
        socket_events = 0
        if stats_cog is not None:
            stats = cast('Stats', stats_cog)
            commands_run = sum(stats.command_stats.values())
            socket_events = sum(stats.socket_stats.values())

        embed = (
            discord.Embed(
                title='Bot Stats Report',
                color=HEALTHY,
                timestamp=discord.utils.utcnow(),
            )
            .add_field(
                name='Process',
                value=f'{cpu_usage:.2f}% CPU\n'
                f'CPU Threads: {cpu_count}\n'
                f'Process Threads: {thread_count}\n'
                f'PID: {pid}',
                inline=True,
            )
            .add_field(
                name='Memory',
                value=f'Physical: {physical_memory:.2f} MiB\n'
                f'Unique: {unique_memory:.2f} MiB\n'
                f'Free: {free_memory:.2f} MiB',
                inline=True,
            )
            .add_field(
                name='Cache',
                value=f'Guilds: {len(self.bot.guilds)}\n'
                f'Users: {len(self.bot.users)}\n'
                f'Messages: {message_cache}',
                inline=True,
            )
            .add_field(
                name='Events Waiting',
                value=f'Total: {len(event_tasks)}\nFuture task: {len(future_tasks)}',
                inline=True,
            )
            .add_field(
                name='Session Counters',
                value=f'Commands run: {commands_run!s}\n'
                f'Socket events: {socket_events!s}',
                inline=True,
            )
            .add_field(
                name='Distribution',
                value=f'Commit: `{current_commit}`\n'
                f'{dist_version}\n'
                f'Jishaku: v{package_version("jishaku")}\n'
                f'Python: v{python_version}\n'
                f'Platform: {sys.platform}',
                inline=False,
            )
            .set_footer(text='Made with ❤️ by Leah 🌸')
        )

        description: list[str] = []

        started_at = discord.utils.format_dt(self.bot.started_at, 'R')
        description.append(f'Started: {started_at}')

        global_rate_limit = not self.bot.http._global_over.is_set()  # noqa: SLF001
        description.append(f'Global Rate Limit: {global_rate_limit}')

        if global_rate_limit:
            embed.color = UNHEALTHY

        embed.description = '\n'.join(description)
        await ctx.reply(embed=embed)


async def setup(client: MoistBot) -> None:
    await client.add_cog(Owner(client))
