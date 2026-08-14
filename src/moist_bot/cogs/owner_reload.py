# pyright: reportPrivateUsage=false

from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from anyio import Path
from discord.ext import commands

from moist_bot.bot import (
    COGS_PACKAGE_NAME,
    extension_module_name,
    normalize_extension_name,
)
from moist_bot.constants import (
    COGS_PROJECT_PATH,
    DEPENDENCY_FILES,
    MIGRATIONS_PROJECT_PATH,
    PROJECT_ROOT_PATH,
    ROOT_PACKAGE,
    ROOT_PACKAGE_PROJECT_PATH,
)
from moist_bot.utils.context import ConfirmationView
from moist_bot.utils.formats import format_file_list, format_process_error
from moist_bot.utils.process import run_git, run_process

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


log = logging.getLogger('discord.' + __name__)


UV_COMMAND = ('uv', 'sync', '--locked')
ALEMBIC_COMMAND = ('uv', 'run', 'alembic', 'upgrade', 'head')
COMMIT_LIST_LIMIT = 10
COMMIT_TEXT_LIMIT = 700


@dataclass(frozen=True, slots=True)
class ReloadTarget:
    """A module discovered from the git diff that can be reloaded."""

    module: str
    display_name: str
    depth: int
    is_extension: bool


def module_name_from_project_path(file: Path) -> str | None:
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

    if file.suffix != '.py':
        return None

    try:
        source_path = file.relative_to('src')
    except ValueError:
        return None

    if source_path.name == '__init__.py':
        source_path = source_path.parent
    else:
        source_path = source_path.with_suffix('')

    if not source_path.parts:
        return None

    return '.'.join(source_path.parts)


def needs_restart(changed_files: list[Path]) -> bool:
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

    for file in changed_files:
        if file in DEPENDENCY_FILES:
            return True

        if (
            file.is_relative_to(ROOT_PACKAGE_PROJECT_PATH)
            and file.suffix == '.py'
            and not file.is_relative_to(COGS_PROJECT_PATH)
        ):
            return True

    return False


def needs_uv_sync(changed_files: list[Path]) -> bool:
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


def needs_alembic_upgrade(changed_files: list[Path]) -> bool:
    """Return whether database migrations changed during the pull.

    Parameters
    ----------
    changed_files:
        Project-relative paths changed by the update.

    Returns
    -------
    bool
        Whether ``uv run alembic upgrade head`` should be run.
    """

    return any(file.is_relative_to(MIGRATIONS_PROJECT_PATH) for file in changed_files)


def github_repository_url(remote_url: str) -> str | None:
    """Convert a GitHub git remote into its browser repository URL."""

    remote_url = remote_url.strip().removesuffix('.git')
    if remote_url.startswith('git@github.com:'):
        repository = remote_url.removeprefix('git@github.com:')
    elif remote_url.startswith('ssh://git@github.com/'):
        repository = remote_url.removeprefix('ssh://git@github.com/')
    elif remote_url.startswith('https://github.com/'):
        repository = remote_url.removeprefix('https://github.com/')
    else:
        return None

    if repository.count('/') != 1:
        return None
    return f'https://github.com/{repository}'


def format_commit_list(output: str) -> str:
    """Format git log output as a compact Discord-safe commit list."""

    lines = output.splitlines()
    commits: list[str] = []
    current_length = 0
    for line in lines[:COMMIT_LIST_LIMIT]:
        short_sha, separator, subject = line.partition('\t')
        if not separator:
            continue

        # Make format
        safe_subject = discord.utils.escape_mentions(
            discord.utils.escape_markdown(subject)
        )
        commit = f'- `{short_sha}` - {safe_subject}'
        new_length = current_length + len(commit) + 1
        if new_length > COMMIT_TEXT_LIMIT:
            break

        commits.append(commit)
        current_length = new_length

    if len(commits) < len(lines):
        commits.append(f'- ...and {len(lines) - len(commits)} more commit(s).')

    return '\n'.join(commits) or '- Commit messages unavailable.'


class OwnerReload(commands.Cog):
    """Extension management commands for the bot owner."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot
        self.last_ext: str = 'cmds'

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{HAMMER AND WRENCH}')

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[reportIncompatibleMethodOverride]
        if not await ctx.bot.is_owner(ctx.author):
            raise commands.NotOwner('You do not own this bot.')
        return True

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

    def find_reload_targets(self, changed_files: list[Path]) -> list[ReloadTarget]:
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
            module = module_name_from_project_path(file)
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

        message = await ctx.reply(':arrow_down: Pulling updates...')
        # Capture the current commit before pulling
        before_status, before_stdout, before_stderr = await run_git(
            'rev-parse', 'HEAD', cwd=PROJECT_ROOT_PATH
        )
        if before_status != 0:
            await message.edit(
                content=':anger: Unable to read the current git commit.\n'
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
            await message.edit(
                content=':anger: `git pull --ff-only` failed.\n'
                + format_process_error('git pull --ff-only', pull_stdout, pull_stderr)
            )
            return

        # Compare the new commit to the previous one
        after_status, after_stdout, after_stderr = await run_git(
            'rev-parse', 'HEAD', cwd=PROJECT_ROOT_PATH
        )
        if after_status != 0:
            await message.edit(
                content=':anger: Unable to read the updated git commit.\n'
                + format_process_error('git rev-parse HEAD', after_stdout, after_stderr)
            )
            return

        before_sha = before_stdout.strip()
        after_sha = after_stdout.strip()
        if before_sha == after_sha:
            output = pull_stdout.strip() or pull_stderr.strip() or 'Already up to date.'
            await message.edit(content=f':white_check_mark: {output}')
            return

        remote_status, remote_stdout, _ = await run_git(
            'remote', 'get-url', 'origin', cwd=PROJECT_ROOT_PATH
        )
        repository_url = (
            github_repository_url(remote_stdout) if remote_status == 0 else None
        )
        compare_url = (
            f'{repository_url}/compare/{before_sha}...{after_sha}'
            if repository_url is not None
            else None
        )

        log_status, log_stdout, _ = await run_git(
            'log',
            '--format=%h%x09%s',
            f'{before_sha}..{after_sha}',
            cwd=PROJECT_ROOT_PATH,
        )
        commit_text = format_commit_list(log_stdout if log_status == 0 else '')
        update_text = f'`{before_sha[:7]}...{after_sha[:7]}`'
        if compare_url is not None:
            update_text = f'[{before_sha[:7]}...{after_sha[:7]}](<{compare_url}>)'

        # Limit reload decisions to files changed by this pull
        diff_status, diff_stdout, diff_stderr = await run_git(
            'diff',
            '--name-only',
            f'{before_sha}..{after_sha}',
            cwd=PROJECT_ROOT_PATH,
        )
        if diff_status != 0:
            await message.edit(
                content=':anger: Unable to inspect the updated files.\n'
                + format_process_error(
                    f'git diff --name-only {before_sha}..{after_sha}',
                    diff_stdout,
                    diff_stderr,
                )
            )
            return

        changed_files = [
            Path(file) for file in diff_stdout.splitlines() if file.strip()
        ]
        targets = self.find_reload_targets(changed_files)
        restart_required = needs_restart(changed_files)

        # Update the virtual environment when dependency metadata changed
        if needs_uv_sync(changed_files):
            status, stdout, stderr = await run_process(
                *UV_COMMAND, cwd=PROJECT_ROOT_PATH
            )
            if status != 0:
                command_text = ' '.join(UV_COMMAND)
                await message.edit(
                    content=f':anger: `{command_text}` failed after pulling updates.\n'
                    + format_process_error(command_text, stdout, stderr)
                )
                return

        if needs_alembic_upgrade(changed_files):
            status, stdout, stderr = await run_process(
                *ALEMBIC_COMMAND, cwd=PROJECT_ROOT_PATH
            )
            if status != 0:
                command_text = ' '.join(ALEMBIC_COMMAND)
                await message.edit(
                    content=f':anger: `{command_text}` failed after pulling updates.\n'
                    + format_process_error(command_text, stdout, stderr)
                )
                return

        changed_text = format_file_list(changed_files, limit=650)
        if not targets:
            embed = (
                discord.Embed(
                    title='Update complete',
                    description=f'Updated {update_text}.',
                    colour=discord.Colour.green(),
                )
                .add_field(name='Commits', value=commit_text, inline=False)
                .add_field(name='Changed files', value=changed_text, inline=False)
                .add_field(
                    name='Reload status',
                    value='No reloadable cog files changed.',
                    inline=False,
                )
            )
            if restart_required:
                embed.add_field(
                    name='\N{WARNING SIGN} Restart required',
                    value='Use `restart` for the non-cog changes to fully take effect.',
                    inline=False,
                )
            await message.edit(content=None, embed=embed)
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
            embed = discord.Embed(
                title='Update complete',
                description=f'Updated {update_text}.',
                colour=discord.Colour.green(),
            )
            embed.add_field(name='Commits', value=commit_text, inline=False)
            embed.add_field(name='Changed files', value=changed_text, inline=False)
            embed.add_field(
                name='Reload status',
                value='No already-loaded cog modules changed.',
                inline=False,
            )
            if skipped_targets:
                skipped_text = '\n'.join(
                    f'{index}. `{target.display_name}`'
                    for index, target in enumerate(skipped_targets, start=1)
                )
                embed.add_field(
                    name='Skipped unloaded modules',
                    value=skipped_text,
                    inline=False,
                )
            if restart_required:
                embed.add_field(
                    name='\N{WARNING SIGN} Restart required',
                    value='Use `restart` for the non-cog changes to fully take effect.',
                    inline=False,
                )
            await message.edit(content=None, embed=embed)
            return

        modules_text = '\n'.join(
            f'{index}. `{target.display_name}`'
            for index, target in enumerate(loaded_targets, start=1)
        )
        prompt_text = (
            f':arrow_down: Pulled {update_text}.\n\n'
            f'**Commits**\n{commit_text}\n\n'
            f'**Modules to reload**\n{modules_text}'
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

        confirmation = ConfirmationView(
            timeout=60.0, delete_after=False, author_id=ctx.author.id
        )
        confirmation.message = message
        await message.edit(content=prompt_text, view=confirmation)
        await confirmation.wait()
        confirm = confirmation.value
        if not confirm:
            result = 'Reload cancelled.' if confirm is False else 'Reload timed out.'
            await message.edit(
                content=f':x: {result}\n\nPulled {update_text}.\n\n**Commits**\n{commit_text}',
                view=None,
            )
            return

        # Reload deeper helper modules before top-level cog extensions
        statuses: list[tuple[str, str]] = []
        failed_reloads = 0
        for target in loaded_targets:
            try:
                await self.reload_target(target)
            except KeyError, commands.ExtensionError:
                log.exception(f'Unable to reload {target.display_name}.')
                statuses.append((ctx.tick(opt=False), target.display_name))
                failed_reloads += 1
            else:
                statuses.append((ctx.tick(opt=True), target.display_name))

        status_text = '\n'.join(f'{status}: `{module}`' for status, module in statuses)
        embed = discord.Embed(
            title=(
                'Reload complete'
                if not failed_reloads
                else 'Reload completed with errors'
            ),
            description=f'Updated {update_text}.',
            colour=(
                discord.Colour.green()
                if not failed_reloads
                else discord.Colour.orange()
            ),
        )
        embed.add_field(name='Commits', value=commit_text, inline=False)
        embed.add_field(name='Reload results', value=status_text, inline=False)
        embed.add_field(name='Changed files', value=changed_text, inline=False)
        if restart_required:
            embed.add_field(
                name='\N{WARNING SIGN} Restart required',
                value='Use `restart` for the non-cog changes to fully take effect.',
                inline=False,
            )

        await message.edit(content=None, embed=embed, view=None)

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


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(OwnerReload(bot))
