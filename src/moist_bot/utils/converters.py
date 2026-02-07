from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, overload
from urllib import error as url_error
from urllib.parse import urlparse

if TYPE_CHECKING:
    from moist_bot.utils.context import Context


type N = int | float


@overload
def remove_decimal(number: int, ndigits: int = 2) -> int: ...


@overload
def remove_decimal(number: float, ndigits: int = 2) -> N: ...


def remove_decimal(number: N, ndigits: int = 2) -> N:
    if isinstance(number, int):
        return number
    if number.is_integer():
        return int(number)
    return round(number, ndigits)


def is_url(text: str) -> bool:
    try:
        result = urlparse(text)
        return all((result.scheme, result.netloc))
    except url_error.URLError:
        return False


async def get_media_from_ctx(
    ctx: Context, arg: str | None = None, buffer: BytesIO | None = None
) -> BytesIO | None:

    buffer = buffer or BytesIO()
    reply = ctx.replied_message
    bot = ctx.bot
    media = b''

    # Fetch media bytes
    if arg and is_url(arg):
        media = await bot.http.get_from_cdn(arg)
    elif reply:
        if reply.attachments:
            media = await reply.attachments[0].read(use_cached=True)
        elif is_url(reply.content):
            media = await bot.http.get_from_cdn(reply.content)

    if not media:
        return None

    buffer.write(media)
    buffer.seek(0)
    return buffer
