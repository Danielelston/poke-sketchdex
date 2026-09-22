"""Discord embed construction for daily posts and profiles."""

from __future__ import annotations

import discord

from .pokeapi import PokemonRef

# Rough type -> color map for embed accents.
TYPE_COLORS = {
    "normal": 0xA8A77A, "fire": 0xEE8130, "water": 0x6390F0, "electric": 0xF7D02C,
    "grass": 0x7AC74C, "ice": 0x96D9D6, "fighting": 0xC22E28, "poison": 0xA33EA1,
    "ground": 0xE2BF65, "flying": 0xA98FF3, "psychic": 0xF95587, "bug": 0xA6B91A,
    "rock": 0xB6A136, "ghost": 0x735797, "dragon": 0x6F35FC, "dark": 0x705746,
    "steel": 0xB7B7CE, "fairy": 0xD685AD,
}


def daily_embed(ref: PokemonRef, shiny: bool, images: list[str]) -> discord.Embed:
    color = TYPE_COLORS.get(ref.types[0], 0x5865F2) if ref.types else 0x5865F2
    shiny_tag = " ✨(Shiny!)" if shiny else ""
    title = f"Today's sketch: #{ref.dex_no:04d} {ref.display_name()}{shiny_tag}"
    type_line = " / ".join(t.title() for t in ref.types) if ref.types else "—"
    embed = discord.Embed(
        title=title,
        description=(
            f"**Type:** {type_line}\n\n"
            "Grab your pencils! Post your sketch with `/submit` in today's thread. "
            "React 👍 on entries you like."
        ),
        color=color,
    )
    if images:
        embed.set_image(url=images[0])
    if len(images) > 1:
        embed.set_thumbnail(url=images[1])
    embed.set_footer(text="PokeSketchDex • one Pokemon a day")
    return embed
