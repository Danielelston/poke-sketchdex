"""`/profile`: the merged player profile command.

Replaces two overlapping commands per the design doc's locked "Visual
direction update" + "Command consolidation" decisions:
- `/profile-card` (Pillow-composited flat PNG, PR #8) — the original
  Pillow-rendering approach was briefly dropped for a pure `discord.Embed` +
  `discord.ui.View`, then reintroduced (see profile_card_render.py) as a
  rendered PNG attached via `embed.set_image()` once plain embed fields
  proved too visually flat — but the interactive `discord.ui.View` buttons
  from that native-embed rebuild stay exactly as they are; this is a hybrid,
  not a full revert.
- `/profile` (cogs/stats.py's old text-embed, per-server + global stats,
  no party/badge) — folded in below; the per-server + global stats data now
  flows into the rendered card image (see `_build_card_data()`) rather than
  an `embed.add_field()` "This Server" field.

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

import io
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from .. import db, leveling, pokebox, rank_badges
from ..profile_card_render import PartyCardSlot, ProfileCardData, ServerCardStats, render_profile_panel

log = logging.getLogger(__name__)

KUDOS_VIEW_TIMEOUT = 600.0  # 10 minutes — see module docstring's "View persistence" note.
MAX_PARTY_LINES = 6


async def _get_avatar_path(bot: commands.Bot, user: discord.abc.User) -> str | None:
    """Fetch (once) + cache-to-disk-forever the target's Discord avatar PNG,
    for the header's circular avatar — same on-disk cache-forever posture as
    `PokeApiClient.get_sprite_image_path()` (cache dir keyed by user id +
    avatar hash, so a changed avatar naturally gets a new file rather than
    serving a stale cached image). A fetch/download failure just means the
    render falls back to the initial-letter placeholder circle, not a
    crashed /profile — same non-fatal posture as the party sprite fetch."""
    avatar = getattr(user, "display_avatar", None)
    if avatar is None:
        return None
    cache_dir = os.path.join(bot.config.image_cache_dir, "avatars")
    os.makedirs(cache_dir, exist_ok=True)
    # avatar.key is stable per (user_id, avatar_hash) — a changed avatar
    # naturally busts the cache since the key changes too.
    path = os.path.join(cache_dir, f"{user.id}_{avatar.key}.png")
    if os.path.exists(path):
        return path
    try:
        await avatar.save(path)
    except (discord.HTTPException, OSError):
        log.warning("Failed to fetch/save avatar for user_id=%s", user.id, exc_info=True)
        return None
    return path


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


def _build_card_data(
    *,
    username: str,
    level: int,
    rank_tier: str,
    title_badge: str,
    exp_into: int,
    exp_need: int,
    global_streak: int,
    global_streak_best: int,
    global_sketch_count: int,
    accuracy_pct: float,
    dex_scanned: int,
    dex_total: int,
    shiny_count: int,
    shiny_example: str | None,
    kudos_count: int,
    upvotes_received_count: int,
    party: list[db.CaughtMon],
    sprite_paths: dict[int, str | None],
    server_level: int | None,
    server_streak: int | None,
    server_streak_best: int | None,
    server_sketch_count: int | None,
    accent_color: tuple[int, int, int] | None,
    avatar_path: str | None,
) -> ProfileCardData:
    """Pure conversion from already-computed primitives (no Discord/DB
    objects, except the CaughtMon party list which is converted here) into a
    `profile_card_render.ProfileCardData`. This is the regression-guard seam:
    it's what proves per-server stats still reach the rendered card now that
    the embed no longer carries them as text fields.

    `sprite_paths` is keyed by `CaughtMon.slot` — the caller resolves each
    party member's sprite via `PokeApiClient.get_sprite_image_path` (async
    network I/O) *before* calling this, since this function and the render
    module it feeds stay synchronous."""
    party_slots = [
        PartyCardSlot(
            slot=mon.slot,
            dex_no=mon.dex_no,
            species_name=pokebox.species_display_name(mon.name),
            is_shiny=mon.is_shiny,
            nickname=mon.nickname,
            sprite_path=sprite_paths.get(mon.slot),
            mon_level=mon.mon_level,
            mon_exp=mon.mon_exp,
        )
        for mon in party[:MAX_PARTY_LINES]
    ]
    server = None
    if server_level is not None:
        server = ServerCardStats(
            level=server_level,
            streak=server_streak or 0,
            streak_best=server_streak_best or 0,
            sketch_count=server_sketch_count or 0,
        )
    return ProfileCardData(
        username=username,
        level=level,
        rank_tier=rank_tier,
        title_badge=title_badge,
        exp_current=exp_into,
        exp_needed=exp_need,
        global_streak=global_streak,
        global_streak_best=global_streak_best,
        global_sketch_count=global_sketch_count,
        accuracy_pct=accuracy_pct,
        dex_scanned=dex_scanned,
        dex_total=dex_total,
        shiny_count=shiny_count,
        shiny_example=shiny_example,
        kudos_count=kudos_count + upvotes_received_count,
        party=party_slots,
        server=server,
        accent_color=accent_color,
        avatar_path=avatar_path,
    )


async def _kudos_count(s, target_user_id: int) -> int:
    return (
        await s.execute(select(func.count()).select_from(db.Kudos).where(db.Kudos.target_user_id == target_user_id))
    ).scalar_one()


async def _upvotes_received_count(s, target_user_id: int) -> int:
    """Total upvotes across every submission the user has ever made (not
    guild-scoped, same cross-server posture as kudos) — combined with
    `_kudos_count` into the profile card's single Kudos tile number."""
    return (
        await s.execute(
            select(func.count())
            .select_from(db.Upvote)
            .join(db.Submission, db.Submission.id == db.Upvote.submission_id)
            .where(db.Submission.user_id == target_user_id)
        )
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
            upvotes_received_count = await _upvotes_received_count(s, target.id)
            leaderboard_rank = await _global_leaderboard_rank(s, target.id, global_row.exp)

        level, into, need = leveling.exp_into_level(global_row.exp)
        rank_tier = rank_badges.rank_for_level(level)
        title_badge = _title_badge(leaderboard_rank, rank_tier)
        accuracy = _accuracy_pct(global_sub_count, days_since_first)

        # discord.User.accent_color is only populated on a freshly-fetched
        # User, never on a cached Member/User — must explicitly fetch it.
        # Most users never set one (None), and the fetch itself can fail
        # (rate limit, deleted account) without that being fatal to /profile.
        accent_color: tuple[int, int, int] | None = None
        try:
            fetched_user = await self.bot.fetch_user(target.id)
        except (discord.NotFound, discord.HTTPException):
            fetched_user = None
        if fetched_user is not None and fetched_user.accent_color is not None:
            c = fetched_user.accent_color
            accent_color = (c.r, c.g, c.b)

        avatar_path = await _get_avatar_path(self.bot, target)

        # Party sprites: network I/O happens here (async), so only a resolved
        # local file path gets threaded into ProfileCardData — the Pillow
        # render itself stays synchronous. A per-mon fetch failure just
        # drops that one sprite, not the whole card.
        sprite_paths: dict[int, str | None] = {}
        for mon in party[:MAX_PARTY_LINES]:
            try:
                sprite_paths[mon.slot] = await self.bot.api.get_sprite_image_path(mon.dex_no, shiny=mon.is_shiny)
            except Exception:
                log.warning(
                    "Failed to fetch party sprite for dex_no=%s shiny=%s",
                    mon.dex_no, mon.is_shiny, exc_info=True,
                )
                sprite_paths[mon.slot] = None

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

        server_level = server_streak = server_streak_best = None
        if server_row is not None:
            server_level, _sinto, _sneed = leveling.exp_into_level(server_row.exp)
            server_streak = server_row.personal_streak
            server_streak_best = server_row.longest_streak

        card_data = _build_card_data(
            username=target.display_name,
            level=level,
            rank_tier=rank_tier,
            title_badge=title_badge,
            exp_into=into,
            exp_need=need,
            global_streak=global_row.global_streak,
            global_streak_best=global_row.longest_global_streak,
            global_sketch_count=global_sub_count,
            accuracy_pct=accuracy,
            dex_scanned=scanned,
            dex_total=dex_total,
            shiny_count=shiny_count,
            shiny_example=shiny_example,
            kudos_count=kudos_count,
            upvotes_received_count=upvotes_received_count,
            party=party,
            sprite_paths=sprite_paths,
            server_level=server_level,
            server_streak=server_streak,
            server_streak_best=server_streak_best,
            server_sketch_count=server_sub_count,
            accent_color=accent_color,
            avatar_path=avatar_path,
        )
        card_bytes = render_profile_panel(card_data)
        card_filename = "profile_card.png"
        card_file = discord.File(io.BytesIO(card_bytes), filename=card_filename)
        embed.set_image(url=f"attachment://{card_filename}")

        embed.set_footer(text="PokeSketchDex")

        view = ProfileView(self.bot, target.id, target.display_name)
        view.give_kudos.label = f"Give Kudos ({kudos_count})"

        await interaction.followup.send(embed=embed, view=view, files=[emblem_file, card_file])
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profile(bot))
