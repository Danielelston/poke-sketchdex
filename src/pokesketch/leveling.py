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
EXP_PER_UPVOTE = 1
EXP_UPVOTE_DAILY_CAP = 25
EXP_DAILY_WINNER = 25
EXP_THEME_CHALLENGE = 50
EXP_SHINY_BONUS = 15


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
