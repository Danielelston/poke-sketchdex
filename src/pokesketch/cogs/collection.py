"""PokeBox, Party & Pokeballs: /pokebox, /catch, /party, /box, /swap, /release, /pokeballs.

PokeBox (free dex-completion tracking) is populated from cogs/submissions.py.
Everything here is global (not per-guild) — party, storage, and the pokeball
wallet are shared across every server the bot is in.
"""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from .. import db, pokebox
from ..formatting import species_display_name
from ..ui import PaginatorView

log = logging.getLogger(__name__)

BOX_PAGE_SIZE = 10

MAX_AUTOCOMPLETE_CHOICES = 25


def _display_name(mon: db.CaughtMon) -> str:
    shiny_tag = " ✨" if mon.is_shiny else ""
    species = f"#{mon.dex_no:04d} {species_display_name(mon.name)}{shiny_tag}"
    if mon.nickname:
        return f"{mon.nickname} ({species})"
    return species


class Collection(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="pokebox", description="Show dex completion: how many species you've ever sketched.")
    @app_commands.describe(user="Whose PokeBox to show (default: you)")
    async def pokebox_cmd(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        async with db.session() as s:
            scanned, total = await pokebox.pokebox_progress(s, target.id)
            by_gen = await pokebox.pokebox_by_generation(s, target.id)

        pct = (scanned / total * 100) if total else 0.0
        embed = discord.Embed(
            title=f"📖 {target.display_name}'s PokeBox",
            description=f"**{scanned} / {total} scanned ({pct:.1f}%)**",
            color=0x2ECC71,
        )
        gen_lines = [f"{label}: {gc}/{gt}" for label, gc, gt in by_gen]
        embed.add_field(name="By generation", value="\n".join(gen_lines), inline=False)
        await interaction.response.send_message(embed=embed)

    async def _catch_target_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        async with db.session() as s:
            eligible = await pokebox.catchable_submissions(s, interaction.user.id)

        results: list[tuple[str, str]] = []
        for sub, catch_window_hours in eligible:
            species, day_label, time_left = pokebox.catch_label_parts(sub, catch_window_hours)
            if sub.guild_id != interaction.guild_id:
                guild = self.bot.get_guild(sub.guild_id)
                guild_name = guild.name if guild else str(sub.guild_id)
                day_label = f"{day_label} ({guild_name})"
            label = f"{species} — {day_label}, {time_left}"
            results.append((label, pokebox.encode_catch_target(sub)))

        if current:
            cur = current.lower()
            results = [r for r in results if cur in r[0].lower()]

        return [
            app_commands.Choice(name=label, value=value)
            for label, value in results[:MAX_AUTOCOMPLETE_CHOICES]
        ]

    @app_commands.command(
        name="catch", description="Spend a pokeball to catch an eligible sketch into your party/storage."
    )
    @app_commands.describe(
        target="Which sketch to catch (leave blank for your most recent eligible one)",
        nickname="Optional nickname for this mon (max 12 chars)",
    )
    @app_commands.autocomplete(target=_catch_target_autocomplete)
    async def catch(
        self,
        interaction: discord.Interaction,
        target: str | None = None,
        nickname: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            async with db.session() as s:
                mon = await pokebox.catch_submission(
                    s, interaction.user.id, self.bot.config.party_cache_dir,
                    target=target, nickname=nickname,
                )
                await s.commit()
        except pokebox.CatchError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        placement = f"active slot {mon.slot}" if mon.is_active else f"box slot {mon.slot}"
        await interaction.followup.send(
            f"⚾ Caught **{_display_name(mon)}**! Placed in your {placement}.", ephemeral=True
        )

    @app_commands.command(name="party", description="Show your (or someone's) 6 active party slots.")
    @app_commands.describe(user="Whose party to show (default: you)")
    async def party(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        async with db.session() as s:
            mons = await pokebox.party_listing(s, target.id)

        if not mons:
            await interaction.response.send_message(
                f"{target.display_name} doesn't have any active party members yet. Catch one with `/catch`!"
            )
            return

        files: list[discord.File] = []
        embeds: list[discord.Embed] = []
        for mon in mons:
            embed = discord.Embed(title=f"Slot {mon.slot}: {_display_name(mon)}", color=0x5865F2)
            if mon.cached_image_path and os.path.exists(mon.cached_image_path):
                filename = f"slot{mon.slot}.png"
                files.append(discord.File(mon.cached_image_path, filename=filename))
                embed.set_image(url=f"attachment://{filename}")
            embeds.append(embed)
        embeds[0].set_author(name=f"{target.display_name}'s Active Party ({len(mons)}/{pokebox.MAX_ACTIVE})")

        await interaction.response.send_message(embeds=embeds, files=files)

    @app_commands.command(name="box", description="Paginated view of your (or someone's) storage box.")
    @app_commands.describe(user="Whose box to show (default: you)")
    async def box(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        async with db.session() as s:
            mons = await pokebox.box_listing(s, target.id)
            total = await pokebox.total_caught_count(s, target.id)

        if not mons:
            await interaction.response.send_message(f"{target.display_name}'s storage box is empty.")
            return

        chunks = [mons[i : i + BOX_PAGE_SIZE] for i in range(0, len(mons), BOX_PAGE_SIZE)]
        pages = []
        for page_no, chunk in enumerate(chunks, start=1):
            lines = [f"`Box {mon.slot:>2}` — {_display_name(mon)}" for mon in chunk]
            embed = discord.Embed(
                title=f"📦 {target.display_name}'s Storage Box",
                description="\n".join(lines),
                color=0x2ECC71,
            )
            embed.set_footer(
                text=f"{len(mons)} boxed · {total}/{pokebox.MAX_TOTAL} total caught · page {page_no}/{len(chunks)}"
            )
            pages.append(embed)

        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0])
            return
        view = PaginatorView(pages, author_id=interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="swap", description="Swap a boxed mon into an active party slot.")
    @app_commands.describe(box_slot="Box slot (1-20) to promote", active_slot="Active slot (1-6) to place it in")
    async def swap(self, interaction: discord.Interaction, box_slot: int, active_slot: int) -> None:
        try:
            async with db.session() as s:
                promoted, demoted = await pokebox.swap_mon(s, interaction.user.id, box_slot, active_slot)
                await s.commit()
        except pokebox.CatchError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        if demoted is not None:
            msg = (
                f"🔄 Swapped **{_display_name(promoted)}** into active slot {active_slot}, "
                f"moved **{_display_name(demoted)}** to box slot {demoted.slot}."
            )
        else:
            msg = f"🔄 Moved **{_display_name(promoted)}** into active slot {active_slot}."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="release", description="Release a caught mon and free its slot.")
    @app_commands.describe(slot="Slot number to release", active="Release from your active party instead of storage")
    async def release(self, interaction: discord.Interaction, slot: int, active: bool = False) -> None:
        try:
            async with db.session() as s:
                path = await pokebox.release_mon(s, interaction.user.id, slot, active)
                await s.commit()
        except pokebox.CatchError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        pokebox.delete_cached_file(path)
        kind = "active" if active else "box"
        await interaction.response.send_message(f"🕊️ Released the mon in {kind} slot {slot}.", ephemeral=True)

    @app_commands.command(name="pokeballs", description="Show your (or someone's) pokeball balance.")
    @app_commands.describe(user="Whose balance to show (default: you)")
    async def pokeballs(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        async with db.session() as s:
            wallet = await pokebox.get_or_create_wallet(s, target.id)
            await s.commit()
        next_grant = pokebox.next_weekly_grant_at()
        await interaction.response.send_message(
            f"⚾ {target.display_name} has **{wallet.balance}** pokeball"
            f"{'s' if wallet.balance != 1 else ''}.\n"
            f"Next weekly grant: <t:{int(next_grant.timestamp())}:R>."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Collection(bot))
