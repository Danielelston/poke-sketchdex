"""`/profile`: the merged player profile command.

Replaces two overlapping commands per the design doc's locked "Visual
direction update" + "Command consolidation" decisions:
- `/profile-card` (Pillow-composited flat PNG, PR #8) — the rendering
  approach is fully replaced here by a native `discord.Embed` +
  `discord.ui.View`.
- `/profile` (cogs/stats.py's old text-embed, per-server + global stats,
  no party/badge) — folded in below, as the "This Server" field.

Command surface decision: renamed from `/profile-card` to `/profile` (the old
`/profile-card` name is dropped entirely, not aliased) — once there is only
one command, the "-card" distinction stops meaning anything, and `/profile`
is the more discoverable, conventional name. `/help` is updated to match.

Thumbnail-slot decision: an embed has exactly one `thumbnail` slot, and the
old `/profile` (avatar) and `/profile-card` (rank-ball art) each wanted it.
The target's own Discord avatar wins it (recognizable, matches Discord's own
profile conventions); the rank-tier Poké Ball art instead becomes the
embed's `author` icon (small, top-of-embed, otherwise unused) via an
attached emblem PNG, so neither signal is silently dropped.

Title/role badge rule (LOCKED to be "derived from rank tier or leaderboard
position", exact mapping left as an implementation-time call — see
`_title_badge()`): global-EXP leaderboard rank #1 -> "Champion", #2-4 ->
"Elite Four", #5-10 -> "Gym Leader", otherwise falls back to "{rank tier}
Trainer" (e.g. "Great Ball Trainer"). Deliberately does not touch the
`GymBadge` table (separate concept: per-event trophies, guild-scoped).

Accuracy% (LOCKED formula: submission_count / days_since_first_submit):
`days_since_first_submit` is derived from MIN(Submission.created_at) for the
user (pokebox.days_since_first_submission), not GlobalUser.created_at — the
former is the literal, direct "days since joining" source; the latter is set
once at first insert and never touched again, but that's a step removed from
the actual first-submission event, so this avoids leaning on an inferred
proxy where a direct one exists. A same-day first submission (0 elapsed
days) is treated as 1 day to avoid a divide-by-zero, and the displayed
percentage is clamped to 100% (the formula can technically exceed 100% since
one calendar day can hold two submissions — daily + wild encounter — but
"accuracy" reads as a fraction of eligible days, so displaying e.g. "140%"
would be a more confusing signal than an informative one).

Kudos (LOCKED, new schema — db.Kudos): one-kudos-per-voter-per-target,
mirroring `Upvote`'s own uniqueness pattern (see db/models.py's Kudos
docstring) rather than unlimited repeat kudos from the same person. Self-
kudos is rejected (an uncapped self-serve counter would make the number
meaningless). Schema change is a brand-new table, so `db.create_all()`'s
existing "create tables if they do not exist" posture (see db/engine.py)
handles it with no migration tooling needed — this project has none, and
this change doesn't need one.

View persistence: this project has no existing persistent-view pattern
(no `bot.add_view` usage anywhere) — introducing one for a single command
would be new infra for a v1 feature. Uses a per-invocation `discord.ui.View`
with a 10-minute timeout instead (buttons disable after that; a given
kudos vote or party inspection already fully lands in the DB/response
before that point, so no data is lost, only the button interactivity after
the window — flagged in the PR description as the accepted v1 tradeoff).

"Inspect Party" reuses `pokebox.build_party_embeds()` (option (a) from the
design doc: the same logic /party already uses, refactored into a shared
helper, not a redirect-to-/pokebox stub) as an ephemeral followup so it
doesn't clutter the channel.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from .. import db, leveling, pokebox, rank_badges

log = logging.getLogger(__name__)

KUDOS_VIEW_TIMEOUT = 600.0  # 10 minutes — see module docstring's "View persistence" note.
MAX_PARTY_LINES = 6

_EXP_BAR_LEN = 12


def _exp_bar(exp: int) -> tuple[int, int, int, str]:
    lvl, into, need = leveling.exp_into_level(exp)
    filled = 0 if need == 0 else int(_EXP_BAR_LEN * into / need)
    bar = "█" * filled + "░" * (_EXP_BAR_LEN - filled)
    return lvl, into, need, bar


def _title_badge(leaderboard_rank: int | None, rank_tier: str) -> str:
    """Role/title badge: derived from global leaderboard position first,
    falling back to the rank tier when the user isn't ranked highly enough
    for a leaderboard-based title. See module docstring for the locked
    concrete rule."""
    if leaderboard_rank == 1:
        return "Champion"
    if leaderboard_rank is not None and 2 <= leaderboard_rank <= 4:
        return "Elite Four"
    if leaderboard_rank is not None and 5 <= leaderboard_rank <= 10:
        return "Gym Leader"
    return f"{rank_tier} Trainer"


def _accuracy_pct(submission_count: int, days_since_first_submit: int | None) -> float:
    """See module docstring's "Accuracy%" note for the formula, the
    divide-by-zero guard, and why the display is clamped to 100%."""
    if not submission_count or days_since_first_submit is None:
        return 0.0
    days = max(1, days_since_first_submit)
    return min(100.0, submission_count / days * 100.0)


async def _global_leaderboard_rank(s, user_id: int, user_exp: int) -> int | None:
    """1-based global-EXP leaderboard rank, or None for a user with 0 EXP
    (not meaningfully "ranked")."""
    if user_exp <= 0:
        return None
    higher = (
        await s.execute(select(func.count()).select_from(db.GlobalUser).where(db.GlobalUser.exp > user_exp))
    ).scalar_one()
    return higher + 1


def _party_lines(party: list[db.CaughtMon]) -> str:
    """Compact text list for the party section — inline fields/colored tiles
    aren't available in a real embed (see design doc caveat). Per-mon level
    is intentionally omitted: CaughtMon.mon_level is reserved-but-unused
    (always its default), so showing it would display a fake stat rather
    than real data."""
    if not party:
        return "No active party members yet — catch one with `/catch`!"
    lines = []
    for mon in party[:MAX_PARTY_LINES]:
        shiny_star = " ★" if mon.is_shiny else ""
        name = mon.nickname or pokebox.species_display_name(mon.name)
        lines.append(f"`{mon.slot}.` #{mon.dex_no:04d} {name}{shiny_star}")
    return "\n".join(lines)


async def _kudos_count(s, target_user_id: int) -> int:
    return (
        await s.execute(select(func.count()).select_from(db.Kudos).where(db.Kudos.target_user_id == target_user_id))
    ).scalar_one()


class ProfileView(discord.ui.View):
    """Two buttons attached to a `/profile` embed: Inspect Party (ephemeral
    followup) and Give Kudos (increments db.Kudos, edits the message).
    Neither is author-locked — a profile is meant to be viewed and kudos'd
    by anyone who sees it, not just the person who ran the command."""

    def __init__(self, bot: commands.Bot, target_id: int, target_display_name: str) -> None:
        super().__init__(timeout=KUDOS_VIEW_TIMEOUT)
        self.bot = bot
        self.target_id = target_id
        self.target_display_name = target_display_name
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Inspect Party", style=discord.ButtonStyle.primary, emoji="🔍")
    async def inspect_party(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with db.session() as s:
            party = await pokebox.party_listing(s, self.target_id)
        if not party:
            await interaction.response.send_message(
                f"{self.target_display_name} doesn't have any active party members yet.", ephemeral=True
            )
            return
        embeds, files = pokebox.build_party_embeds(self.target_display_name, party)
        await interaction.response.send_message(embeds=embeds, files=files, ephemeral=True)

    @discord.ui.button(label="Give Kudos (0)", style=discord.ButtonStyle.secondary, emoji="⭐")
    async def give_kudos(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id == self.target_id:
            await interaction.response.send_message("You can't give kudos to yourself.", ephemeral=True)
            return
        async with db.session() as s:
            existing = (
                await s.execute(
                    select(db.Kudos).where(
                        db.Kudos.target_user_id == self.target_id, db.Kudos.voter_id == interaction.user.id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                await interaction.response.send_message(
                    f"You've already given {self.target_display_name} kudos.", ephemeral=True
                )
                return
            s.add(db.Kudos(target_user_id=self.target_id, voter_id=interaction.user.id))
            await s.commit()
            count = await _kudos_count(s, self.target_id)

        button.label = f"Give Kudos ({count})"
        await interaction.response.edit_message(view=self)


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="profile",
        description="Your (or someone's) level, rank, stats, party, and kudos.",
    )
    @app_commands.describe(user="Whose profile to show (default: you)")
    async def profile(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        # More DB queries than either of the two commands this replaces had
        # alone (per-server AND global stats, accuracy%, dex%, shiny query,
        # kudos count) — defer proactively, same posture PR #8 already used.
        await interaction.response.defer(thinking=True)

        target = user or interaction.user
        async with db.session() as s:
            global_row = (
                await s.execute(select(db.GlobalUser).where(db.GlobalUser.user_id == target.id))
            ).scalar_one_or_none()

            if global_row is None:
                await interaction.followup.send(
                    f"{target.display_name} hasn't submitted any sketches yet.", ephemeral=True
                )
                return

            server_row = None
            server_sub_count = 0
            if interaction.guild_id is not None:
                server_row = (
                    await s.execute(
                        select(db.User).where(
                            db.User.guild_id == interaction.guild_id, db.User.user_id == target.id
                        )
                    )
                ).scalar_one_or_none()
                if server_row is not None:
                    server_sub_count = (
                        await s.execute(
                            select(func.count(db.Submission.id)).where(
                                db.Submission.guild_id == interaction.guild_id,
                                db.Submission.user_id == target.id,
                            )
                        )
                    ).scalar_one()

            global_sub_count = (
                await s.execute(
                    select(func.count(db.Submission.id)).where(db.Submission.user_id == target.id)
                )
            ).scalar_one()

            scanned, dex_total = await pokebox.pokebox_progress(s, target.id)
            shiny_count, shiny_example = await pokebox.shiny_summary(s, target.id)
            days_since_first = await pokebox.days_since_first_submission(s, target.id)
            party = await pokebox.party_listing(s, target.id)
            kudos_count = await _kudos_count(s, target.id)
            leaderboard_rank = await _global_leaderboard_rank(s, target.id, global_row.exp)

        level, into, need, bar = _exp_bar(global_row.exp)
        rank_tier = rank_badges.rank_for_level(level)
        title_badge = _title_badge(leaderboard_rank, rank_tier)
        accuracy = _accuracy_pct(global_sub_count, days_since_first)

        emblem_path = await rank_badges.get_tier_emblem(rank_tier, self.bot.config.badge_cache_dir)
        emblem_filename = "tier_emblem.png"
        emblem_file = discord.File(emblem_path, filename=emblem_filename)

        embed = discord.Embed(
            title=f"{target.display_name}'s Profile",
            description=f"**{title_badge}**",
            color=discord.Color.from_rgb(*rank_badges.accent_color_for_tier(rank_tier)),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=rank_tier, icon_url=f"attachment://{emblem_filename}")
        # `target` is a discord.Member for a self-invocation in a guild and a
        # discord.User for an explicit `user=` target — check the attribute
        # directly rather than `isinstance(target, discord.User)` (which the
        # two commands this merges both used, but which is False for the far
        # more common Member case, silently skipping the thumbnail then).
        avatar = getattr(target, "display_avatar", None)
        if avatar is not None:
            embed.set_thumbnail(url=avatar.url)

        embed.add_field(
            name="🌐 Global",
            value=(
                f"Level {level} · {into}/{need} EXP\n"
                f"`{bar}`\n"
                f"🔥 {global_row.global_streak}-day streak (best {global_row.longest_global_streak})\n"
                f"{global_sub_count} sketches total"
            ),
            inline=True,
        )

        if server_row is not None:
            slvl, sinto, sneed, sbar = _exp_bar(server_row.exp)
            embed.add_field(
                name="📍 This Server",
                value=(
                    f"Level {slvl} · {sinto}/{sneed} EXP\n"
                    f"`{sbar}`\n"
                    f"🔥 {server_row.personal_streak}-day streak (best {server_row.longest_streak})\n"
                    f"{server_sub_count} sketches here"
                ),
                inline=True,
            )

        embed.add_field(
            name="🎯 Accuracy",
            value=f"{accuracy:.1f}%\n({global_sub_count} sketches / {max(days_since_first or 0, 1)} days)",
            inline=True,
        )

        dex_pct = (scanned / dex_total * 100) if dex_total else 0.0
        embed.add_field(name="📖 Pokédex", value=f"{scanned}/{dex_total} ({dex_pct:.1f}%)", inline=True)

        shiny_value = f"{shiny_count} species" + (f"\nLatest: {shiny_example}" if shiny_example else "")
        embed.add_field(name="✨ Shinies", value=shiny_value, inline=True)

        embed.add_field(name="⭐ Kudos", value=str(kudos_count), inline=True)

        embed.add_field(name="🎒 Active Party", value=_party_lines(party), inline=False)

        embed.set_footer(text="PokeSketchDex")

        view = ProfileView(self.bot, target.id, target.display_name)
        view.give_kudos.label = f"Give Kudos ({kudos_count})"

        await interaction.followup.send(embed=embed, view=view, file=emblem_file)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profile(bot))
