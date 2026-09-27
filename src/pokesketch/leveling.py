"""EXP + leveling rules. Constants are centralized here for easy tuning."""

from __future__ import annotations

# --- EXP award constants (see design doc EXP model) ---
EXP_SUBMIT = 10
# Distinct, smaller than EXP_SUBMIT: the wild-encounter thread is a secondary,
# optional-alongside-the-main-daily activity (per the Weekly Vote & Wild
# Encounters plan's open question) — same streak-bonus/upvote rules apply on
# top of this via the shared streak logic in cogs/submissions.py.
EXP_WILD_ENCOUNTER = 5
EXP_PER_STREAK_DAY = 2
EXP_STREAK_CAP = 20  # max bonus from the streak term
# Receiving an upvote on your sketch: the bigger of the two upvote-EXP amounts.
EXP_PER_UPVOTE_RECEIVED = 2
EXP_UPVOTE_RECEIVED_DAILY_CAP = 20
# Giving an upvote to someone else's sketch: a tiny amount, encourages engaging
# with others' work rather than just self-submitting. Smaller than receiving.
EXP_PER_UPVOTE_GIVEN = 1
EXP_UPVOTE_GIVEN_DAILY_CAP = 10
EXP_DAILY_WINNER = 25
EXP_THEME_CHALLENGE = 50
EXP_SHINY_BONUS = 15
# Receiving a super like on your sketch (see Super Likes on Submissions Plan):
# bigger than a free upvote-received (EXP_PER_UPVOTE_RECEIVED, capped) because
# it costs the giver a scarce, submission-earned resource, smaller than
# winning the whole day (EXP_DAILY_WINNER). Deliberately uncapped per
# recipient per day — the giver's own limited stock is the only throttle.
EXP_PER_SUPER_LIKE = 15


def streak_bonus(streak: int) -> int:
    """Bonus EXP for maintaining a personal streak (capped)."""
    return min(EXP_PER_STREAK_DAY * max(streak, 0), EXP_STREAK_CAP)


def level_for_exp(exp: int) -> int:
    """Map total EXP to a level.

    Uses a gentle quadratic curve: cumulative EXP to *reach* level L is
    50 * (L-1) * L / 2 ... i.e. level 2 at 50, level 3 at 150, level 4 at 300.
    Kept simple and monotonic; tune freely without losing history because raw
    exp_events are stored.
    """
    level = 1
    while exp >= exp_to_reach(level + 1):
        level += 1
    return level


def exp_to_reach(level: int) -> int:
    """Total cumulative EXP required to be at `level`."""
    if level <= 1:
        return 0
    n = level - 1
    return 25 * n * (n + 1)  # 50, 150, 300, 500, ...


def exp_into_level(exp: int) -> tuple[int, int, int]:
    """Return (current_level, exp_into_level, exp_needed_for_next)."""
    lvl = level_for_exp(exp)
    base = exp_to_reach(lvl)
    nxt = exp_to_reach(lvl + 1)
    return lvl, exp - base, nxt - base


# --- Party mon EXP award constants (see Party Mon Leveling Plan design doc) ---
MON_EXP_SUBMIT = 50
MON_EXP_UPVOTE = 20
MON_EXP_KUDOS = 250
# Party mon EXP for a super like (see Super Likes on Submissions Plan): same
# ~10x multiplier MON_EXP_UPVOTE (20) already uses over
# EXP_PER_UPVOTE_RECEIVED (2), applied to EXP_PER_SUPER_LIKE (15).
# User-confirmed 2026-09-26. Still subject to MON_EXP_DAILY_CAP like every
# other mon-EXP source, via the shared award_mon_exp() helper.
MON_EXP_SUPER_LIKE = 150
MON_EXP_DAILY_CAP = 150
MON_LEVEL_CAP = 100


def mon_exp_to_reach(level: int) -> int:
    """Total cumulative EXP required for a party mon to be at `level`.

    The player curve (`exp_to_reach`, `25 * n * (n + 1)`) at one-fifth scale.
    Clamped at `MON_LEVEL_CAP` (49,500 EXP): levels above the cap all report
    the cap's requirement, so this stays well-defined for absurd inputs too.
    """
    if level <= 1:
        return 0
    level = min(level, MON_LEVEL_CAP)
    n = level - 1
    return 5 * n * (n + 1)  # 10, 30, 60, 100, ...


def mon_level_for_exp(exp: int) -> int:
    """Map total party-mon EXP to a level, never exceeding `MON_LEVEL_CAP`."""
    if exp >= mon_exp_to_reach(MON_LEVEL_CAP):
        return MON_LEVEL_CAP
    level = 1
    while level < MON_LEVEL_CAP and exp >= mon_exp_to_reach(level + 1):
        level += 1
    return level


def mon_exp_into_level(exp: int) -> tuple[int, int, int]:
    """Return (current_level, exp_into_level, exp_needed_for_next) for a party mon.

    Mirrors `exp_into_level()`. At the level cap, `exp_needed_for_next` equals
    `exp_into_level` (a zero-width band) since there is no next level.
    """
    lvl = mon_level_for_exp(exp)
    base = mon_exp_to_reach(lvl)
    nxt = mon_exp_to_reach(min(lvl + 1, MON_LEVEL_CAP))
    return lvl, exp - base, nxt - base
