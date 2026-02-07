from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
from inspect import cleandoc
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypedDict, cast

import discord
import discord.utils
import numpy as np
from discord import app_commands
from discord.ext import commands
from numpy.random import default_rng

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator

    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


log = logging.getLogger('discord.' + __name__)


DO_PERF_TIMING: bool = False
GAME_INTERACTION_LOG = cleandoc(
    """
Interaction \x1b[44mtimings\x1b[0m for \x1b[36;1m%s\x1b[0m \x1b[90m(%s)\x1b[0m,
in guild \x1b[36;1m%s\x1b[0m \x1b[90m(%s)\x1b[0m:
Interaction start
|    ->Event loop start
|    | |
|    | Event loop took %sms
|    |
|    | Render cycle start
|    | |
|    | Render cycle took %sms
|    --> Frame latency %sms, %s fps (in theory)
|
|    ->Await discord gateway api call
|    |
|    -->Discord gateway response in %sms
|
Full interaction (including overheads) took %sms
"""
)

LABELS = {'up': 'Up', 'left': 'Left', 'down': 'Down', 'right': 'Right', 'quit': 'Quit'}


def perf_timer[**P](
    begin_out: str | None = None, end_out: str | None = None
) -> Callable[[Callable[P, None]], Callable[P, None]]:
    def inner(func: Callable[P, None]) -> Callable[P, None]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
            cls = cast('SnakeGameContainer', args[0])

            # Skip
            if not cls.perf_timing:
                func(*args, **kwargs)
                return

            if begin_out:
                setattr(cls, begin_out, time.perf_counter_ns())
            func(*args, **kwargs)
            if end_out:
                setattr(cls, end_out, time.perf_counter_ns())

        return wrapper

    return inner


class IterableRepeater:
    """Infinitely returns the next element of any iterable."""

    __slots__ = ('_item_generator', '_items')

    def __init__(self, items: Iterable[Any]) -> None:
        self._items = items
        self._item_generator = self.gen_items()

    @property
    def item(self) -> Any:
        return next(self._item_generator)

    def gen_items(self) -> Generator[Any]:
        items = self._items
        while True:
            yield from items

    def reset(self) -> None:
        self._item_generator = self.gen_items()

    def __iter__(self) -> Any:
        return self.item

    def __repr__(self) -> Any:
        return self.item


class SnakeGameAssets(TypedDict):
    empty: str
    apple: str
    snake_head: str
    snake_body: IterableRepeater


class SnakeGameContainer:
    """Container for a snake game holding the current game state and logic"""

    __slots__ = (
        'alive',
        'apple',
        'ate_apple',
        'empty_field',
        'field',
        'field_area',
        'field_dim',
        'game_score',
        'perf_move_snake_begin',
        'perf_move_snake_end',
        'perf_render_begin',
        'perf_render_end',
        'rendered_field',
        'snake_body',
        'snake_body_len',
        'snake_head',
        'won',
    )

    assets: ClassVar[SnakeGameAssets] = {
        'empty': '⬛',
        'apple': '🟥',
        'snake_head': '🟢',
        'snake_body': IterableRepeater(('🟩', '🟨')),
    }
    rng = default_rng()
    empty_body = np.array((-1, -1), dtype='int8')
    perf_timing: bool = DO_PERF_TIMING

    def __init__(self, size_x: int = 10, size_y: int = 10):
        """Initial game variables"""
        if size_x < 0 or size_y < 0:
            raise ValueError('A size value cannot be negative.')

        # Initial game state
        self.field_dim = np.array((size_x, size_y), dtype='uint8')
        self.field_area: int = self.field_dim.prod()

        self.empty_field = np.full(self.field_dim, self.assets['empty'], dtype='str_')
        self.field = self.empty_field.copy()
        self.rendered_field: str = ''

        # Initial game objects
        self.snake_body_len: int = 2

        self.snake_head = np.array(
            tuple((round(x / 2) for x in self.field_dim)), dtype='int8'
        )

        # By default, generates initial snake bodies to the right of `self.snake_head`,
        # lengthened with `self.init_snake_body_len`
        self.snake_body = np.full((self.field_area, 2), -1, dtype='int8')
        self.snake_body[: self.snake_body_len] = np.array(
            [
                (self.snake_head[0] + o, self.snake_head[1])
                for o in range(1, self.snake_body_len + 1)
            ],
            dtype='int8',
        )

        self.apple = np.array(
            (abs(self.snake_head[0] - 3), self.snake_head[1]), dtype='uint8'
        )

        # Game info
        self.ate_apple: bool = False
        self.game_score: int = 0
        self.alive: bool = True
        self.won: bool = False

        # Perf timings
        self.perf_move_snake_begin: int = 0
        self.perf_move_snake_end: int = 0
        self.perf_render_begin: int = 0
        self.perf_render_end: int = 0

    def _respawn_apple(self):
        """Respawn apple
        note this is highly inefficient and flawed (oh well... uwu)
        """

        rand_idx = self.rng.integers(0, self.field_area, dtype='uint8')
        i = self.field_dim[1]
        x = rand_idx % i
        y = rand_idx // i
        self.apple[:] = (x, y)

        if np.any(
            np.all(self.apple == self.snake_body[: self.snake_body_len], axis=1)
        ) or np.array_equal(self.apple, self.snake_head):
            self._respawn_apple()

    def _move_snake(self, x: int, y: int) -> None:
        """Increment `self.snake_head` by `x` or `y`"""

        self.snake_body = np.roll(self.snake_body, 1, axis=0)
        self.snake_body.put((0, 1), self.snake_head)

        self.snake_head[:] += np.array((x, y), dtype='int8')

        # If the snake is about to eat an apple
        if np.array_equal(self.snake_head, self.apple):
            self.ate_apple = True
            self.snake_body_len += 1  # I hate how this has to be here
            self.game_score += 1
        else:
            # If the snake didn't eat an apple
            # remove the last snake body
            self.snake_body[self.snake_body_len + 1] = self.empty_body

    @perf_timer('perf_move_snake_begin', 'perf_move_snake_end')
    def move_snake(self, x: int = 0, y: int = 0) -> None:
        """Move the snake, respawn apples and do movement checks.
        This essentially works as the event loop.
        """
        self._move_snake(x, y)

        # Win condition
        if self.snake_body_len + 1 == self.field_area:
            self.game_win()
            return

        # Respawn apple
        if self.ate_apple:
            self.ate_apple = False
            self._respawn_apple()

        # Game over conditions
        if (
            # If the snake hit its own body
            np.any(
                np.all(
                    self.snake_head == self.snake_body[: self.snake_body_len], axis=1
                )
            )
            # If the snake hit a wall
            or np.any(self.snake_head + (1, 1) > self.field_dim)  # noqa: RUF005
            or np.any(self.snake_head < (0, 0))
        ):
            self.game_over()

    @contextmanager
    def _render(self) -> Iterator[str]:
        """Flatten the current field array into a single string"""

        try:
            self.rendered_field = '\n'.join(''.join(e) for e in self.field)
            yield self.rendered_field
        finally:
            # Reset the field to it's empty form for future render passes
            self.field = self.empty_field.copy()
            self.perf_render_end = time.perf_counter_ns()

    def render(self) -> str:
        """Add game objects and invoke `self._render`"""
        self.perf_render_begin: int = time.perf_counter_ns()

        for obj in self.snake_body[: self.snake_body_len]:
            self.field[obj[1], obj[0]] = self.assets['snake_body']

        # Guh
        self.assets['snake_body'].reset()

        self.field[self.apple[1], self.apple[0]] = self.assets['apple']
        self.field[self.snake_head[1], self.snake_head[0]] = self.assets['snake_head']

        with self._render() as r:
            return r

    def game_over(self) -> None:
        """This is called when the player dies.

        The game can technically persist after this
        so ending the game is optional.
        """
        self.alive = False

    def game_win(self) -> None:
        """This is called when the player reaches the max size."""
        # TODO(leah): make event
        if self.perf_timing:
            log.info('Win')

        self.won = True
        self.game_over()


class SnakeGameView(discord.ui.View):
    game_timeout: float
    _tm_fmt: str

    def __init__(
        self,
        ctx: Context,
        game_instance: SnakeGameContainer,
        *,
        embed: discord.Embed,
        message: discord.Message | None = None,
        timeout: float = 15,
    ):
        super().__init__(timeout=timeout)
        self.ctx: Context = ctx
        self.embed: discord.Embed = embed
        self.message: discord.Message = message  # type: ignore[]
        self.game_instance: SnakeGameContainer = game_instance

        # Buttons
        self.opposite_button: discord.ui.Button[SnakeGameView] = None  # type: ignore[]
        self.last_opposite_button: discord.ui.Button[SnakeGameView] = None  # type: ignore[]

        # Debug extras
        self._perf_i_check: int = 0
        self.perf_await_api_begin: int = 0
        if self.game_instance.perf_timing:
            author = self.ctx.author
            guild = self.ctx.guild
            log.info(
                'Interaction \x1b[42;1mstarted\x1b[0m for \x1b[36;1m%s\x1b[0m \x1b[90m(%s)\x1b[0m \n'
                'in guild \x1b[36;1m%s\x1b[0m \x1b[90m(%s)\x1b[0m!',
                author,
                author.id,
                *(guild.name, guild.id) if guild else ('None', 'DMs'),
            )

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        self._perf_i_check = time.perf_counter_ns()
        if interaction.user and interaction.user.id == self.ctx.author.id:
            return True

        await interaction.response.send_message(
            'This game button is not for you.', ephemeral=True
        )
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,  # noqa: ARG002
        error: Any,  # noqa: ARG002
        item: Any,
        /,
    ) -> None:
        log.error('Ignoring exception in view %r for item %r', self, item)
        await self._on_game_over(':warning: An unknown error occurred!')

    async def on_timeout(self) -> None:
        await self._on_game_over(':hourglass: Took too long to move!')

    def _perf_log(self):
        perf_end = time.perf_counter_ns()
        author = self.ctx.author
        guild = self.ctx.guild
        g = self.game_instance

        log.info(
            GAME_INTERACTION_LOG,
            author,
            author.id,
            *(guild.name, guild.id) if guild else ('None', 'DMs'),
            (a := g.perf_move_snake_end - g.perf_move_snake_begin) / 1_000_000,
            (b := g.perf_render_end - g.perf_render_begin) / 1_000_000,
            (c := a + b) / 1_000_000,
            round(10**9 / c),
            (perf_end - self.perf_await_api_begin) / 1_000_000,
            (perf_end - self._perf_i_check) / 1_000_000,
        )

    async def _on_game_over(self, message: str = ':x: **You died!**') -> None:
        """Function for when the game has ended"""
        self.embed.description = self.game_instance.rendered_field
        self.embed.color = discord.Color.dark_red()

        await self.message.edit(
            content=f'{message} Final score: {self.game_instance.game_score}',
            embed=self.embed,
            view=None,
        )
        self.stop()

        if self.game_instance.perf_timing:
            log.info(
                'Interaction \x1b[41;1mended\x1b[0m for \x1b[36;1m%s\x1b[0m \x1b[90m(%s)\x1b[0m.',
                self.ctx.author,
                self.ctx.author.id,
            )

    def _set_opposite_button(self, label: str) -> None:
        """Finds and sets the `discord.ui.Button` object
        that has the specified label from a str
        """
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == LABELS[label]:
                self.opposite_button = item
                break

    @discord.ui.button(label=LABELS['quit'], style=discord.ButtonStyle.red, row=0)
    async def quit(
        self, interaction: discord.Interaction, button: discord.ui.Button[SnakeGameView]
    ):
        # Confirm quit if the game score is over 2
        if self.game_instance.game_score > 2:
            button.disabled = True
            await interaction.response.edit_message(embed=self.embed, view=self)

            confirm = await self.ctx.prompt('Are you sure you want to quit?')
            if not confirm:
                button.disabled = False
                return

        self.game_instance.alive = False
        await self._on_game_over(':x: You quit!')

    @discord.ui.button(label=LABELS['up'], style=discord.ButtonStyle.primary, row=0)
    async def up(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[SnakeGameView],
    ):
        self._set_opposite_button('down')
        await self.on_button_interaction(interaction, y=-1)

    @discord.ui.button(
        label='\u2800', style=discord.ButtonStyle.gray, row=0, disabled=True
    )
    async def empty1(
        self, interaction: discord.Interaction, button: discord.ui.Button[SnakeGameView]
    ):
        pass

    @discord.ui.button(label=LABELS['left'], style=discord.ButtonStyle.primary, row=1)
    async def left(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[SnakeGameView],
    ):
        self._set_opposite_button('right')
        await self.on_button_interaction(interaction, x=-1)

    @discord.ui.button(label=LABELS['down'], style=discord.ButtonStyle.primary, row=1)
    async def down(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[SnakeGameView],
    ):
        self._set_opposite_button('up')
        await self.on_button_interaction(interaction, y=1)

    @discord.ui.button(label=LABELS['right'], style=discord.ButtonStyle.primary, row=1)
    async def right(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[SnakeGameView],
    ):
        self._set_opposite_button('left')
        await self.on_button_interaction(interaction, x=1)

    async def on_button_interaction(
        self, interaction: discord.Interaction, **kwargs: Any
    ):
        """Callback function of every movement button.
        This handles updating the game and disabling buttons.
        """

        # Disable opposite movement button so that
        # the snake can't move into itself
        if self.opposite_button is not self.last_opposite_button:
            self.opposite_button.disabled = True
            if self.last_opposite_button:
                self.last_opposite_button.disabled = False

        self.last_opposite_button = self.opposite_button

        # Move
        self.game_instance.move_snake(**kwargs)

        # Set performance counter pre message edit
        self.perf_await_api_begin = time.perf_counter_ns()

        # Edit original message to the updated game state
        if self.game_instance.alive:
            tm_in = discord.utils.utcnow() + timedelta(seconds=self.game_timeout)
            self._tm_fmt = tm_fmt = discord.utils.format_dt(tm_in, 'R')

            self.embed.description = self.game_instance.render()
            await interaction.response.edit_message(
                content=f'AFK-quit {tm_fmt}.', embed=self.embed, view=self
            )

        elif self.game_instance.won:
            self.game_instance.render()
            await self._on_game_over(':crown: **You won!**')

        else:
            await self._on_game_over()

        # Log perf timings
        if self.game_instance.perf_timing:
            self._perf_log()


active_snake_games: dict[int, SnakeGameView] = {}


class SnakeGame(commands.Cog):
    def __init__(self, client: MoistBot):
        self.client: MoistBot = client

    @commands.hybrid_command(fallback='start')
    @commands.cooldown(rate=1, per=10, type=commands.BucketType.user)
    @app_commands.choices(
        size=[
            app_commands.Choice(name='5x5', value=5),
            app_commands.Choice(name='6x6', value=6),
            app_commands.Choice(name='8x8', value=8),
            app_commands.Choice(name='10x10', value=10),
            app_commands.Choice(name='13x13', value=13),
        ]
    )
    async def snake(self, ctx: Context, size: Literal[5, 6, 8, 10, 13] = 10):
        """Play a snake game on discord!"""

        # TODO(leah): maybe non square game sizes
        x, y = size, size

        timeout = 60
        tm_in = discord.utils.utcnow() + timedelta(seconds=timeout)
        tm_fmt = discord.utils.format_dt(tm_in, 'R')

        game_instance = SnakeGameContainer(x, y)

        embed = discord.Embed(
            description=game_instance.render(), color=discord.Color.green()
        )
        embed.set_author(
            name=f"{ctx.author.display_name}'s snake game",
            icon_url=ctx.author.display_avatar,
        )

        view = SnakeGameView(ctx, game_instance, embed=embed, timeout=timeout)
        view.message = message = await ctx.send(
            f'AFK-quit {tm_fmt}.', embed=embed, view=view
        )
        active_snake_games.update({message.id: view})

        # Cleanup
        await view.wait()  # Blocking
        active_snake_games.pop(message.id)
        del game_instance, view


async def setup(client: MoistBot):
    await client.add_cog(SnakeGame(client))


"""
If this file is opened in a terminal,
the game runs with keyboard controls
(mostly used for debugging).
"""
if __name__ == '__main__':
    import sys

    import keyboard  # type: ignore pylint: disable=import-error

    key_to_direction_map: dict[str, dict[str, int]] = {
        'w': {'y': -1},
        'a': {'x': -1},
        's': {'y': 1},
        'd': {'x': 1},
    }
    opposite_key_map: dict[str, str] = {'w': 's', 'a': 'd', 's': 'w', 'd': 'a'}

    # Init
    game = SnakeGameContainer(20, 20)
    print(game.render())

    last_event_name = None
    while True:
        # Block till the next event
        event: keyboard.KeyboardEvent = keyboard.read_event()

        if event.event_type != keyboard.KEY_DOWN:
            continue

        key = event.name or ''
        direction = key_to_direction_map.get(key)

        # Make it so the snake can't move into itself
        if opposite_key_map.get(key) == last_event_name or direction is None:
            continue

        # Update
        game.move_snake(**direction)

        # Post update
        last_event_name = event.name

        # Quit if the player has died
        if not game.alive:
            print(f'You died! Final score: {game.game_score}')
            sys.exit()

        print(game.render())
