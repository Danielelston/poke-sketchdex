"""Discord embed construction for daily posts, profiles, and onboarding."""

from __future__ import annotations

import discord

from .discord_limits import guarded_add_field
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
    embed.set_footer(text="PokeSketchDex • Sketch 'em all")
    return embed


def tutorial_embed() -> discord.Embed:
    """Tutorial DM sent to a player when they run `/register`."""
    embed = discord.Embed(
        title="🎨 Welcome to PokeSketchDex!",
        description="You're registered! Here's how it all works.",
        color=0x5865F2,
    )
    guarded_add_field(
        embed,
        "🗓️ The daily challenge",
        (
            "Every day a Pokémon is posted in the server with reference art. Post your sketch with "
            "`/submit` in that day's thread — grab your pencils! Missed a day? You can still submit "
            "in recent threads for a few days after."
        ),
    )
    guarded_add_field(
        embed,
        "📈 Leveling up",
        (
            "Every submission earns EXP, tracked both in this server *and* globally across every "
            "server you sketch in. Giving and getting 👍 upvotes earns a little more. Submit on "
            "consecutive days to build your streak. Check `/profile` anytime."
        ),
    )
    guarded_add_field(
        embed,
        "⭕ Catch, Party & Storage",
        (
            "You start with **5 pokeballs**. You earn more every Monday — 1 a week to begin with, "
            "rising as you level up (`/pokeballs`). Spend one with `/catch` on **your own** sketches "
            "to keep them: 6 active party slots plus a 20-slot storage box. Manage them with "
            "`/party`, `/box`, `/swap`, `/release`, and `/rename`."
        ),
    )
    guarded_add_field(
        embed,
        "🎨 Sketch Party Art",
        (
            "From level 2, you can turn on **Sketch Party Art** with the 🎨 button on `/profile` "
            "— your party tiles then show *your own sketches* instead of the default sprites."
        ),
    )
    guarded_add_field(
        embed,
        "📐 Sketch sizes",
        (
            "Any image works, but **landscape fills the frame best** — roughly 3:2 or 16:10. "
            "Tall/portrait sketches get letterboxed with bars either side in party thumbnails. "
            "**1080px on the long edge** is the sweet spot; bigger is fine but gets scaled down for "
            "thumbnails. Discord caps attachments at 10–20 MB depending on your tier and the "
            "server's boost level."
        ),
    )
    guarded_add_field(
        embed,
        "⭐ Super Likes",
        (
            "Each sketch you submit earns you one Super Like. Spend it on the ⭐ button under "
            "someone else's submission to give them a bigger EXP boost than a regular 👍."
        ),
    )
    guarded_add_field(
        embed,
        "📕 Your PokéBox 🚧",
        (
            "Every sketch you submit automatically scans that species into your personal dex "
            "tracker — free and unlimited. Check it with `/pokebox`. *Showing per-generation counts "
            "only for now; which species you've scanned is coming soon.*"
        ),
    )
    guarded_add_field(
        embed,
        "🌿 Weekly wild encounters",
        (
            "Once a week the server votes on a themed wild Pokémon, which gets its own bonus "
            "challenge thread all week long — `/submit` works there too, for its own EXP."
        ),
    )
    guarded_add_field(
        embed,
        "Coming soon",
        (
            "A browsable gallery archive of past sketches, and alternate-form reference picking.\n\n"
            "Run `/help` anytime for the full command list and live EXP numbers. "
            "🚧 marks features still being finished."
        ),
    )
    embed.set_footer(text="PokeSketchDex • Sketch 'em all")
    return embed
