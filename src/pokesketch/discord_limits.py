"""Discord embed size limits, shared by every cog that builds embeds.

See https://discord.com/developers/docs/resources/message#embed-object-embed-limits
"""

from __future__ import annotations

import discord

DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBED_FOOTER_LIMIT = 2048
DISCORD_EMBED_MAX_FIELDS = 25
DISCORD_EMBED_TOTAL_LIMIT = 6000
DISCORD_EMBED_FIELD_LIMIT = DISCORD_EMBED_FIELD_VALUE_LIMIT  # back-compat alias


def guarded_add_field(embed: discord.Embed, name: str, value: str, *, inline: bool = False) -> None:
    """`embed.add_field`, but fail loudly instead of letting Discord 400 on send.

    Catches an over-limit field at build time instead of surfacing as a silent
    interaction timeout in production.
    """
    if len(name) > DISCORD_EMBED_FIELD_NAME_LIMIT:
        raise ValueError(
            f"embed field name {name!r} is {len(name)} chars, "
            f"over Discord's {DISCORD_EMBED_FIELD_NAME_LIMIT}-char field name limit"
        )
    if len(value) > DISCORD_EMBED_FIELD_VALUE_LIMIT:
        raise ValueError(
            f"embed field {name!r} is {len(value)} chars, "
            f"over Discord's {DISCORD_EMBED_FIELD_VALUE_LIMIT}-char field limit — split it further"
        )
    embed.add_field(name=name, value=value, inline=inline)
