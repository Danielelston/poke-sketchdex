"""Super-like spend flow: click validation, wallet decrement, EXP grants.

See `Design/Super Likes on Submissions Plan.md`. Earning (+1
`SuperLikeWallet.balance` per accepted submission) lives in
`pokebox.grant_super_like`, called from `cogs/submissions.py` (Card 2/5).
This module is the *spend* side: validating and atomically applying a
single button click (Card 3/5) — see `ui.py`'s `SuperLikeButton` for the
`discord.ui.DynamicItem` that calls into this module.

Mirrors `daily_spotlight.py`'s existing precedent of importing
`_award_exp` straight out of `cogs/submissions.py` rather than duplicating
the get-or-create-user/level-recompute logic a second time.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import db, leveling, pokebox
from .cogs.submissions import _award_exp

log = logging.getLogger(__name__)


class SuperLikeError(Exception):
    """Expected, user-facing rejection — caller shows `str(exc)` ephemerally.

    The giver's balance and every other row are left untouched whenever
    this is raised (see `give_super_like`'s docstring) — rejections never
    partially apply.
    """


def _is_outside_grace_window(local_date: date | None, today_local: date, grace_period_days: int) -> bool:
    """Same cutoff rule as `cogs/submissions.py`'s `_is_outside_grace_window`,
    generalized to a bare `local_date` (the daily's or wild encounter's own
    challenge day) since this module doesn't import that cog's private
    helper for a pure function. A missing `local_date` (e.g. an orphaned
    row whose parent daily/wild-encounter was deleted) is treated as
    outside the window — fail closed, not open.
    """
    if local_date is None:
        return True
    return local_date < today_local - timedelta(days=grace_period_days)


async def _resolve_target(
    s, *, wild: bool, target_id: int
) -> tuple[db.Submission | db.WildEncounterSubmission, int, int, date | None]:
    """Resolve a (wild, target_id) pair to (submission row, owner user_id,
    guild_id, the submission's originating challenge day). Raises
    SuperLikeError if the submission id no longer exists (e.g. a stale
    custom_id after a data wipe) — a malformed custom_id itself is rejected
    earlier, in `ui.py`'s regex parse, before this function is ever called.
    """
    if wild:
        sub = (
            await s.execute(
                select(db.WildEncounterSubmission).where(db.WildEncounterSubmission.id == target_id)
            )
        ).scalar_one_or_none()
        if sub is None:
            raise SuperLikeError("That submission no longer exists.")
        wild_encounter = await s.get(db.WildEncounter, sub.wild_encounter_id)
        local_date = wild_encounter.local_date if wild_encounter is not None else None
        return sub, sub.user_id, sub.guild_id, local_date

    sub = (
        await s.execute(select(db.Submission).where(db.Submission.id == target_id))
    ).scalar_one_or_none()
    if sub is None:
        raise SuperLikeError("That submission no longer exists.")
    daily = await s.get(db.DailyPokemon, sub.daily_id)
    local_date = daily.local_date if daily is not None else None
    return sub, sub.user_id, sub.guild_id, local_date


async def give_super_like(s, *, giver_id: int, wild: bool, target_id: int) -> tuple[int, int]:
    """Validate and apply one Super Like button click inside `s`'s transaction.

    Re-validates everything server-side at click time (never trusts the
    button having rendered as evidence of current eligibility — see the
    design doc's "always re-validate server-side" constraint): submission
    still exists, not self, not a duplicate (giver, submission) pair, not
    past the guild's `grace_period_days` window, giver has balance > 0.
    Raises `SuperLikeError` for any of those, leaving every row (including
    the giver's wallet) unchanged.

    On success: decrements the giver's `SuperLikeWallet.balance` by 1,
    inserts the `SuperLike` row (its unique constraint is the last line of
    defense against a racing double-click, on top of the pre-check above),
    grants `leveling.EXP_PER_SUPER_LIKE` player EXP to the receiver (logged
    as an `ExpEvent` of type `"super_like_received"`), and — failure-
    isolated, matching every other mon-EXP hook site — `MON_EXP_SUPER_LIKE`
    to the one `CaughtMon` sourced from this submission (active or boxed),
    a silent no-op if none was ever caught, subject to `MON_EXP_DAILY_CAP`.

    Returns `(receiver_user_id, mon_exp_awarded)` — `mon_exp_awarded` is 0
    both when there's no caught mon and when the mon's daily cap absorbed
    the whole grant, so callers shouldn't infer "no mon" from a zero value.
    """
    _sub, owner_id, guild_id, local_date = await _resolve_target(s, wild=wild, target_id=target_id)

    if owner_id == giver_id:
        raise SuperLikeError("You can't super-like your own submission.")

    cfg = await s.get(db.GuildConfig, guild_id)
    grace_period_days = cfg.grace_period_days if cfg is not None else 7
    tz_name = cfg.timezone if cfg is not None else "UTC"
    try:
        today_local = datetime.now(ZoneInfo(tz_name)).date()
    except KeyError:
        today_local = datetime.now(UTC).date()
    if _is_outside_grace_window(local_date, today_local, grace_period_days):
        raise SuperLikeError(
            f"This submission is outside its {grace_period_days}-day grace window "
            "and can no longer be super-liked."
        )

    dup_column = db.SuperLike.wild_encounter_submission_id if wild else db.SuperLike.submission_id
    existing = (
        await s.execute(
            select(db.SuperLike).where(dup_column == target_id, db.SuperLike.voter_id == giver_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise SuperLikeError("You've already super-liked this submission.")

    wallet = await pokebox.get_or_create_super_like_wallet(s, giver_id)
    if wallet.balance <= 0:
        raise SuperLikeError("You have no super likes to give — submit a sketch to earn one.")

    wallet.balance -= 1
    if wild:
        s.add(db.SuperLike(wild_encounter_submission_id=target_id, voter_id=giver_id))
    else:
        s.add(db.SuperLike(submission_id=target_id, voter_id=giver_id))
    await s.flush()  # surface a racing double-click's IntegrityError before EXP lands

    await _award_exp(s, guild_id, owner_id, "super_like_received", leveling.EXP_PER_SUPER_LIKE)

    mon_exp_awarded = 0
    try:
        mon = await pokebox.mon_for_submission(s, target_id, wild=wild)
        if mon is not None:
            mon_exp_awarded = await pokebox.award_mon_exp(
                s, mon, leveling.MON_EXP_SUPER_LIKE, today=datetime.now(UTC).date()
            )
    except Exception:
        log.exception(
            "mon-EXP: super-like grant failed for submission_id=%s wild=%s", target_id, wild
        )

    return owner_id, mon_exp_awarded
