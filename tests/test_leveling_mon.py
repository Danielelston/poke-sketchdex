"""Unit tests for the party-mon EXP curve and constants in `leveling.py`.

Pure functions only — no DB, no Discord. See:
`Design/Party Mon Leveling Plan.md` and its Task Breakdown, Unit 1.
"""

from pokesketch import leveling


def test_mon_exp_constants():
    assert leveling.MON_EXP_SUBMIT == 50
    assert leveling.MON_EXP_UPVOTE == 20
    assert leveling.MON_EXP_KUDOS == 250
    assert leveling.MON_EXP_DAILY_CAP == 150
    assert leveling.MON_LEVEL_CAP == 100


def test_mon_exp_to_reach_level_100_is_49500():
    assert leveling.mon_exp_to_reach(100) == 49500


def test_mon_exp_to_reach_level_1_is_zero():
    assert leveling.mon_exp_to_reach(1) == 0
    assert leveling.mon_exp_to_reach(0) == 0


def test_mon_exp_to_reach_is_one_fifth_player_curve():
    for level in range(1, 101):
        assert leveling.mon_exp_to_reach(level) == leveling.exp_to_reach(level) // 5


def test_round_trip_at_exact_thresholds():
    for level in range(1, 101):
        assert leveling.mon_level_for_exp(leveling.mon_exp_to_reach(level)) == level


def test_round_trip_just_below_next_threshold():
    for level in range(1, 100):
        just_below = leveling.mon_exp_to_reach(level + 1) - 1
        assert leveling.mon_level_for_exp(just_below) == level


def test_mon_level_for_exp_caps_at_100_for_absurd_values():
    assert leveling.mon_level_for_exp(49500) == 100
    assert leveling.mon_level_for_exp(49501) == 100
    assert leveling.mon_level_for_exp(10_000_000_000) == 100
    assert leveling.mon_level_for_exp(2**63) == 100


def test_mon_level_for_exp_zero_and_negative():
    assert leveling.mon_level_for_exp(0) == 1
    assert leveling.mon_level_for_exp(-5) == 1


def test_mon_exp_into_level_matches_shape_of_exp_into_level():
    lvl, into, need = leveling.mon_exp_into_level(35)
    assert lvl == leveling.mon_level_for_exp(35)
    assert into == 35 - leveling.mon_exp_to_reach(lvl)
    assert need == leveling.mon_exp_to_reach(lvl + 1) - leveling.mon_exp_to_reach(lvl)


def test_mon_exp_into_level_at_cap():
    lvl, into, need = leveling.mon_exp_into_level(60000)
    assert lvl == 100
    assert into == 60000 - 49500
    assert need == 0


def test_mon_exp_is_never_clamped_by_curve_functions():
    # Raw EXP values far past the cap must not raise or be silently rewritten;
    # only the derived level is capped.
    huge = 10_000_000_000
    assert leveling.mon_level_for_exp(huge) == 100
    lvl, into, _need = leveling.mon_exp_into_level(huge)
    assert lvl == 100
    assert into == huge - 49500
