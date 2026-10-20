from __future__ import annotations

from typing import TYPE_CHECKING, Self

import discord
from aiohttp import ClientTimeout
from pydantic import AwareDatetime, BaseModel, Field, HttpUrl

if TYPE_CHECKING:
    from aiohttp import ClientSession


RELEASES_URL = 'https://github.com/fleasion/Fleasion/releases'
LATEST_RELEASE_API_URL = (
    'https://api.github.com/repos/fleasion/Fleasion/releases/latest'
)


class ReleaseAsset(BaseModel):
    name: str = Field(min_length=1)
    browser_download_url: HttpUrl
    size: int = Field(ge=0)
    state: str

    @property
    def platform(self) -> str:
        name = self.name.casefold()
        if 'windows' in name or name.endswith(('.exe', '.msi')):
            return 'Windows'
        if 'macos' in name or 'darwin' in name or name.endswith('.dmg'):
            return 'macOS'
        if 'linux' in name or name.endswith(('.appimage', '.deb', '.rpm')):
            return 'Linux'
        return 'Other'


class FleasionRelease(BaseModel):
    tag_name: str = Field(min_length=1)
    html_url: HttpUrl
    published_at: AwareDatetime
    assets: tuple[ReleaseAsset, ...]


async def fetch_latest_release(session: ClientSession) -> FleasionRelease:
    async with session.get(
        LATEST_RELEASE_API_URL,
        headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'moist-bot (Fleasion downloads)',
            'X-GitHub-Api-Version': '2022-11-28',
        },
        timeout=ClientTimeout(total=15),
    ) as response:
        response.raise_for_status()
        return FleasionRelease.model_validate_json(await response.read())


class FleasionDownloadView(discord.ui.LayoutView):
    def __init__(self, release: FleasionRelease, *, help_channel_mention: str) -> None:
        super().__init__(timeout=None)

        version = discord.utils.escape_markdown(release.tag_name[:100])
        container = discord.ui.Container[Self](
            discord.ui.TextDisplay(
                f'## [Download Fleasion {version}]({release.html_url})'
                f'\nStable release · Published '
                f'{discord.utils.format_dt(release.published_at, "D")}'
            ),
            accent_colour=discord.Colour.blurple(),
        )

        # Make assets list
        assets = sorted(
            (asset for asset in release.assets if asset.state == 'uploaded'),
            key=lambda asset: (
                ('Windows', 'Linux', 'macOS', 'Other').index(asset.platform),
                asset.name,
            ),
        )
        if assets:
            container.add_item(discord.ui.Separator())
        else:
            container.add_item(
                discord.ui.TextDisplay(
                    'No downloadable files have been uploaded yet. '
                    'Open the release page using the title above.'
                )
            )

        # Leave room for the heading and footer within Discord's component limit
        for asset in assets[:9]:
            size = asset.size / (1024 * 1024)
            container.add_item(
                discord.ui.Section[Self](
                    discord.ui.TextDisplay(f'### {asset.platform}\n-# {size:.1f} MiB'),
                    accessory=discord.ui.Button(
                        label='Download',
                        url=str(asset.browser_download_url),
                    ),
                )
            )

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.ActionRow[Self](
                discord.ui.Button(
                    label='Source code (GitHub)',
                    url='https://github.com/fleasion/Fleasion',
                ),
            )
        )
        container.add_item(
            discord.ui.TextDisplay(f'Need help? Ask in {help_channel_mention}.')
            # discord.ui.TextDisplay('Need help? Ask in #help-chat.')
        )
        self.add_item(container)
