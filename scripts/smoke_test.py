"""Offline smoke test — no Discord token needed.

Exercises DB init, model creation, no-repeat selection, and leveling math.
"""

import asyncio
import os
import tempfile

os.environ.setdefault("POKESKETCH_DISCORD_TOKEN", "dummy-not-used")


async def main() -> None:
    from pokesketch import db, leveling
    from pokesketch.selection import pick_dex_no

    tmp = tempfile.mkdtemp()
    db.init_engine(os.path.join(tmp, "test.db"))
    await db.create_all()

    # Seed a guild config.
    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=1, dex_min=1, dex_max=5, selection_mode="no_repeat"))
        await s.commit()

    # No-repeat: 5 picks should exhaust the pool exactly, no repeats.
    picks = [await pick_dex_no(1, 1, 5, "no_repeat") for _ in range(5)]
    assert sorted(picks) == [1, 2, 3, 4, 5], f"expected full pool, got {picks}"
    # 6th pick triggers a reset and returns a valid number again.
    sixth = await pick_dex_no(1, 1, 5, "no_repeat")
    assert 1 <= sixth <= 5, sixth
    print(f"selection OK: first cycle={picks}, after-reset={sixth}")

    # Manual pool reset (/admin reset-pool): seed rows for a guild, clear
    # them, and confirm a previously-used dex number can be reselected.
    from pokesketch.selection import clear_used_pool

    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=42, dex_min=1, dex_max=2, selection_mode="no_repeat"))
        await s.commit()
    first, second = (
        await pick_dex_no(42, 1, 2, "no_repeat"),
        await pick_dex_no(42, 1, 2, "no_repeat"),
    )
    assert sorted([first, second]) == [1, 2], (first, second)
    cleared = await clear_used_pool(42)
    assert cleared == 2, cleared
    reselect = await pick_dex_no(42, 1, 2, "no_repeat")
    assert reselect in (1, 2), reselect
    # Pool was cleared, so both numbers are available again post-reset.
    async with db.session() as s:
        from sqlalchemy import select as _select

        remaining_rows = (
            await s.execute(_select(db.UsedPokemon).where(db.UsedPokemon.guild_id == 42))
        ).scalars().all()
    assert len(remaining_rows) == 1, remaining_rows  # only the fresh pick after clearing
    print(f"reset-pool OK: seeded={sorted([first, second])}, cleared={cleared}, reselect={reselect}")

    # Leveling curve sanity.
    assert leveling.level_for_exp(0) == 1
    assert leveling.level_for_exp(50) == 2
    assert leveling.level_for_exp(150) == 3
    assert leveling.streak_bonus(100) == leveling.EXP_STREAK_CAP
    lvl, into, need = leveling.exp_into_level(75)
    assert lvl == 2 and into == 25 and need == 100, (lvl, into, need)
    print(f"leveling OK: lvl(75)={lvl}, into={into}, need={need}")

    # Global EXP: award to the same user across two guilds and check global sums
    # while per-guild rows stay independent.
    from pokesketch.cogs.submissions import _award_exp

    uid = 999
    async with db.session() as s:
        await _award_exp(s, guild_id=1, user_id=uid, kind="submit", amount=10)
        await _award_exp(s, guild_id=2, user_id=uid, kind="submit", amount=15)
        await s.commit()

    async with db.session() as s:
        from sqlalchemy import select

        user_g1 = (
            await s.execute(
                select(db.User).where(db.User.guild_id == 1, db.User.user_id == uid)
            )
        ).scalar_one()
        user_g2 = (
            await s.execute(
                select(db.User).where(db.User.guild_id == 2, db.User.user_id == uid)
            )
        ).scalar_one()
        global_user = (
            await s.execute(select(db.GlobalUser).where(db.GlobalUser.user_id == uid))
        ).scalar_one()

    assert user_g1.exp == 10, user_g1.exp
    assert user_g2.exp == 15, user_g2.exp
    assert global_user.exp == 25, global_user.exp
    assert global_user.exp == user_g1.exp + user_g2.exp
    print(
        f"global exp OK: guild1={user_g1.exp}, guild2={user_g2.exp}, "
        f"global={global_user.exp}"
    )

    await db.dispose()

    # /help: EXP explainer should always reflect the live leveling constants
    # (no hardcoded numbers to drift out of sync with the award logic).
    from pokesketch.cogs.help import _exp_field_value

    exp_text = _exp_field_value()
    assert f"+{leveling.EXP_SUBMIT} EXP" in exp_text, exp_text
    assert f"+{leveling.EXP_PER_STREAK_DAY} EXP" in exp_text, exp_text
    assert f"capped at +{leveling.EXP_STREAK_CAP}" in exp_text, exp_text
    assert f"+{leveling.EXP_PER_UPVOTE} EXP" in exp_text, exp_text
    assert f"capped at +{leveling.EXP_UPVOTE_DAILY_CAP}/day" in exp_text, exp_text
    print("help EXP explainer OK: matches live leveling constants")

    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
