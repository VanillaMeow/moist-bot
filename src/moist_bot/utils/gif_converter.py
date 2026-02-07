from __future__ import annotations

from collections import defaultdict
from itertools import chain
from random import randrange
from typing import ClassVar, cast

from PIL import Image as _Image
from PIL.Image import Image

EMPTY_IMAGE = _Image.new(mode='RGBA', size=(0, 0))


class TransparentAnimatedGifConverter:
    _img_p: Image
    _img_p_data: bytearray
    _palette_replaces: dict[str, list[int]]
    _img_p_parsed_palette: dict[int, tuple[int, ...]]
    _img_p_used_palette_idxs: set[int]

    _PALETTE_SLOT_SET: ClassVar[set[int]] = set(range(256))

    def __init__(self, img_rgba: Image = EMPTY_IMAGE, alpha_threshold: int = 0):
        self._alpha_threshold = alpha_threshold
        self._img_rgba: Image = img_rgba

        self._transparent_pixels: set[int] = set()

    @property
    def img_rgba(self) -> Image:
        return self._img_rgba

    @img_rgba.setter
    def img_rgba(self, value: Image) -> None:
        self._img_rgba = value

    def _process_pixels(self) -> None:
        """Set the transparent pixels to the color 0."""
        alpha_data = cast('list[int]', self._img_rgba.getchannel(channel='A').getdata())  # pyright: ignore[reportUnknownMemberType]
        self._transparent_pixels = {
            idx
            for idx, alpha in enumerate(alpha_data)
            if alpha <= self._alpha_threshold
        }

    def _set_parsed_palette(self) -> None:
        """Parse the RGB palette color `tuple`s from the palette."""
        palette = self._img_p.getpalette()
        if palette is None:
            msg = 'Image has no palette'
            raise ValueError(msg)

        self._img_p_used_palette_idxs = {
            idx
            for pal_idx, idx in enumerate(self._img_p_data)
            if pal_idx not in self._transparent_pixels
        }
        self._img_p_parsed_palette = {
            idx: tuple(palette[idx * 3 : idx * 3 + 3])
            for idx in self._img_p_used_palette_idxs
        }

    def _get_similar_color_idx(self) -> int:
        """Return a palette index with the closest similar color."""
        old_color = self._img_p_parsed_palette[0]
        dict_distance: defaultdict[int, list[int]] = defaultdict(list)
        for idx in range(1, 256):
            color_item = self._img_p_parsed_palette[idx]
            if color_item == old_color:
                return idx
            distance = sum(
                (
                    abs(old_color[0] - color_item[0]),  # Red
                    abs(old_color[1] - color_item[1]),  # Green
                    abs(old_color[2] - color_item[2]),  # Blue
                )
            )
            dict_distance[distance].append(idx)
        return dict_distance[min(dict_distance)][0]

    def _remap_palette_idx_zero(self) -> None:
        """Since the first color is used in the palette, remap it."""
        free_slots = self._PALETTE_SLOT_SET - self._img_p_used_palette_idxs
        new_idx = free_slots.pop() if free_slots else self._get_similar_color_idx()
        self._img_p_used_palette_idxs.add(new_idx)
        self._palette_replaces['idx_from'].append(0)
        self._palette_replaces['idx_to'].append(new_idx)
        self._img_p_parsed_palette[new_idx] = self._img_p_parsed_palette[0]
        del self._img_p_parsed_palette[0]

    def _get_unused_color(self) -> tuple[int, int, int]:
        """Return a color for the palette that does not collide with any other already in the palette."""
        used_colors = set(self._img_p_parsed_palette.values())
        while True:
            new_color = (randrange(256), randrange(256), randrange(256))  # noqa: S311
            if new_color not in used_colors:
                return new_color

    def _process_palette(self) -> None:
        """Adjust palette to have the zeroth color set as transparent.
        Basically, get another palette index for the zeroth color.
        """
        self._set_parsed_palette()
        if 0 in self._img_p_used_palette_idxs:
            self._remap_palette_idx_zero()
        self._img_p_parsed_palette[0] = self._get_unused_color()

    def _adjust_pixels(self) -> None:
        """Convert the pixels into their new values."""
        if self._palette_replaces['idx_from']:
            trans_table = bytearray.maketrans(
                bytes(self._palette_replaces['idx_from']),
                bytes(self._palette_replaces['idx_to']),
            )
            self._img_p_data = self._img_p_data.translate(trans_table)
        for idx_pixel in self._transparent_pixels:
            self._img_p_data[idx_pixel] = 0
        self._img_p.frombytes(data=bytes(self._img_p_data))

    def _adjust_palette(self) -> None:
        """Modify the palette in the new `Image`."""
        unused_color = self._get_unused_color()
        final_palette = tuple(
            chain.from_iterable(
                self._img_p_parsed_palette.get(x, unused_color) for x in range(256)
            )
        )
        self._img_p.putpalette(data=final_palette)

    def process(self) -> Image:
        """Return the processed mode `P` `Image`."""
        self._img_p = self._img_rgba.convert(mode='P')
        self._img_p_data = bytearray(self._img_p.tobytes())
        self._palette_replaces = {'idx_from': [], 'idx_to': []}
        self._process_pixels()
        self._process_palette()
        self._adjust_pixels()
        self._adjust_palette()
        self._img_p.info['transparency'] = 0
        self._img_p.info['background'] = 0
        return self._img_p
