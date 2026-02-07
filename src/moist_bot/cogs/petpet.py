from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw

from moist_bot.constants import ASSETS_FOLDER_PATH
from moist_bot.utils.gif_converter import TransparentAnimatedGifConverter

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


RESOLUTION = 150, 150
PETPET_FRAMES_PATH = ASSETS_FOLDER_PATH / 'petpet'
PETPET_FRAMES: list[Image.Image] = []

for frame in PETPET_FRAMES_PATH.iterdir():
    petpet_frame = (
        Image.open(frame)
        .convert('RGBA')
        .resize(RESOLUTION, resample=Image.Resampling.BICUBIC)
    )
    PETPET_FRAMES.append(petpet_frame)


class PetPetCreator:
    _pet_hand_frames = PETPET_FRAMES
    _converter: TransparentAnimatedGifConverter
    _base_img: Image.Image
    gif_buffer: BytesIO

    def __init__(self, image_buffer: BytesIO) -> None:
        self._image_buffer = image_buffer

        self.resolution = RESOLUTION
        self.max_frames = len(self._pet_hand_frames)
        self.frames: list[Image.Image] = []

    def create_gif(self) -> BytesIO:
        """Creates the gif from the image buffer."""

        # Load the image from the buffer and set it up
        self._base_img = Image.open(self._image_buffer)
        self._base_img = self._base_img.convert('RGBA').resize(self.resolution)

        # Init
        self._converter = TransparentAnimatedGifConverter(alpha_threshold=15)
        self.gif_buffer = BytesIO()

        # Round corners of base image
        x, y = self.resolution
        mask = self._gen_mask(start_size=(x * 4, y * 4), final_size=self.resolution)
        canvas = Image.new('RGBA', self.resolution, color=(128, 128, 128, 0))
        self._base_img = Image.composite(self._base_img, canvas, mask=mask)

        # Process and render
        self._process_frames()
        self._render_gif()

        return self.gif_buffer

    def _process_frames(self) -> None:
        for i in range(self.max_frames):
            squeeze = i if i < self.max_frames / 2 else self.max_frames - i

            width = 0.8 + squeeze * 0.02
            height = 0.8 - squeeze * 0.05
            offset_x = (1 - width) * 0.5 + 0.1
            offset_y = (1 - height) - 0.08

            x, y = self.resolution
            new_size = round(width * x), round(height * y)
            box = round(offset_x * x), round(offset_y * y)

            new_img = self._base_img.resize(new_size)
            canvas = Image.new('RGBA', size=(x + 10, y), color=(255, 255, 255, 0))
            canvas.paste(new_img, box=box)

            pat_hand = self._pet_hand_frames[i]

            canvas.paste(pat_hand, mask=pat_hand)
            self.frames.append(canvas)

    def _render_gif(self, durations: int | list[int] = 20) -> None:
        new_frames: list[Image.Image] = []

        for frame in self.frames:
            self._converter.img_rgba = frame
            frame_p = self._converter.process()
            new_frames.append(frame_p)

        output_image = new_frames[0]
        output_image.save(
            self.gif_buffer,
            format='GIF',
            save_all=True,
            optimize=False,
            append_images=new_frames[1:],
            duration=durations,
            disposal=2,  # Other disposals don't work
            loop=0,
        )
        self.gif_buffer.seek(0)

    @staticmethod
    def _gen_mask(
        *,
        start_size: tuple[int, int],
        final_size: tuple[int, int],
        descale: float = 0,
    ) -> Image.Image:
        """Generate a smooth ellipse mask."""

        # Up-scaled ellipse mask
        x, y = start_size
        mask = Image.new('L', start_size)
        draw = ImageDraw.Draw(mask)
        draw.ellipse(xy=(0 + descale, 0 + descale, x - descale, y - descale), fill=255)

        return mask.resize(
            final_size, Image.Resampling.BICUBIC
        )  # Downscale to smooth ellipse mask
        # mask = mask.filter(ImageFilter.SMOOTH)  # Smooth edges


class PetPetEmbed(discord.Embed):
    def __init__(
        self,
        user: discord.User,
    ):
        super().__init__(
            color=user.accent_color or discord.Color.random(),
            type='image',
        )
        self.set_image(url='attachment://petpet.gif')
        self.set_author(
            name=f"{user.display_name}'s petpet", icon_url='attachment://petpet.gif'
        )


class PetPet(commands.Cog):
    def __init__(self, client: MoistBot):
        self.client: MoistBot = client
        self.execute = self.client.loop.run_in_executor

    @staticmethod
    def _get_buffer(img_buffer: BytesIO) -> BytesIO:
        return PetPetCreator(img_buffer).create_gif()

    @commands.cooldown(rate=1, per=4, type=commands.BucketType.user)
    @app_commands.describe(user='The target user.')
    @commands.hybrid_command(name='petpet', description='Petpet')
    async def petpet(self, ctx: Context, user: discord.User = commands.Author):
        await ctx.typing()

        reply = ctx.replied_message
        img_buffer = BytesIO()

        # Fetch image bytes
        # Order of priority: specified user -> attachment -> reply attachment -> author
        if user != ctx.author:
            await user.display_avatar.with_format('png').save(fp=img_buffer)
        elif ctx.message.attachments:
            await ctx.message.attachments[0].save(fp=img_buffer, use_cached=True)
        elif reply and reply.attachments:
            await reply.attachments[0].save(fp=img_buffer, use_cached=True)
        else:
            await user.display_avatar.with_format('png').save(fp=img_buffer)

        # Avoid blocking
        img_buffer = await self.execute(
            self.client.executor, self._get_buffer, img_buffer
        )

        # Send image
        file = discord.File(fp=img_buffer, filename='petpet.gif')
        await ctx.reply(file=file, embed=PetPetEmbed(user))


async def setup(client: MoistBot) -> None:
    await client.add_cog(PetPet(client))
