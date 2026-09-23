"""Offline smoke test — no Discord token needed.

Exercises DB init, model creation, no-repeat selection, and leveling math.
"""

import asyncio
import io
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

    # --- PokeBox, Party & Pokeballs ---
    from datetime import UTC
    from datetime import date as _date
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    from pokesketch import pokebox

    # PokeBox: free tracker, insert-if-not-exists is idempotent.
    async with db.session() as s:
        s.add(db.DailyPokemon(guild_id=1, local_date=_date.today(), dex_no=7, name="squirtle"))
        await s.commit()
        daily = (
            await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.dex_no == 7))
        ).scalar_one()
        pb_uid = 5001
        sub = db.Submission(
            guild_id=1, user_id=pb_uid, daily_id=daily.id, image_url="https://example.invalid/x.png"
        )
        s.add(sub)
        await s.flush()
        await pokebox.record_pokebox_scan(s, pb_uid, daily.dex_no, sub.id)
        await pokebox.record_pokebox_scan(s, pb_uid, daily.dex_no, sub.id)  # idempotent
        wallet = await pokebox.get_or_create_wallet(s, pb_uid)
        wallet.balance = 25
        await s.commit()

    async with db.session() as s:
        scanned, total = await pokebox.pokebox_progress(s, pb_uid)
    assert scanned == 1, scanned
    assert total == pokebox.TOTAL_DEX
    print(f"pokebox OK: scanned={scanned}/{total} (double-insert stayed idempotent)")

    # /catch offline: stub the network image download so the smoke test stays offline.
    party_dir = os.path.join(tmp, "party_cache")

    async def _fake_cache_image(url, user_id, caughtmon_id, cache_dir):
        user_dir = os.path.join(cache_dir, str(user_id))
        os.makedirs(user_dir, exist_ok=True)
        path = os.path.join(user_dir, f"{caughtmon_id}.png")
        with open(path, "wb") as fh:
            fh.write(b"fake-image-bytes")
        return path

    pokebox._cache_image = _fake_cache_image

    # 20 extra distinct submissions for pb_uid (21 total with the original), so
    # the double-catch fix (a caught submission drops out of eligibility)
    # doesn't starve this loop, and one submission is still eligible-but-uncaught
    # when the 21st catch attempt below needs to hit the storage-full branch
    # specifically (not "no eligible submissions").
    async with db.session() as s:
        for i in range(20):
            s.add(
                db.Submission(
                    guild_id=1, user_id=pb_uid, daily_id=daily.id,
                    image_url=f"https://example.invalid/x{i}.png",
                )
            )
        await s.commit()

    # Catch 20 mons: first 6 land active, the rest fall back to storage,
    # exactly matching the 6/20 cap.
    caught_mons = []
    for _ in range(20):
        async with db.session() as s:
            mon = await pokebox.catch_submission(s, pb_uid, party_dir)
            await s.commit()
            caught_mons.append(mon)

    actives = [m for m in caught_mons if m.is_active]
    boxed = [m for m in caught_mons if not m.is_active]
    assert len(actives) == pokebox.MAX_ACTIVE, len(actives)
    assert len(boxed) == pokebox.MAX_TOTAL - pokebox.MAX_ACTIVE, len(boxed)
    assert sorted(m.slot for m in actives) == list(range(1, pokebox.MAX_ACTIVE + 1))
    assert sorted(m.slot for m in boxed) == list(range(1, pokebox.MAX_TOTAL - pokebox.MAX_ACTIVE + 1))
    print(f"catch placement OK: {len(actives)} active, {len(boxed)} boxed (auto-active then storage fallback)")

    # 21st catch: storage is full (20/20) -> clean CatchError, not a crash.
    try:
        async with db.session() as s:
            await pokebox.catch_submission(s, pb_uid, party_dir)
            await s.commit()
        raise AssertionError("expected CatchError for full storage")
    except pokebox.CatchError as exc:
        assert "Storage full" in str(exc), exc
    print("catch cap OK: 21st catch rejected cleanly")

    # No pokeballs left -> clean CatchError.
    empty_uid = 5002
    async with db.session() as s:
        s.add(db.DailyPokemon(guild_id=2, local_date=_date.today(), dex_no=8, name="wartortle"))
        await s.commit()
        daily2 = (
            await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.dex_no == 8))
        ).scalar_one()
        s.add(
            db.Submission(
                guild_id=2, user_id=empty_uid, daily_id=daily2.id, image_url="https://example.invalid/y.png"
            )
        )
        await s.commit()
    try:
        async with db.session() as s:
            # New wallets now start with STARTER_POKEBALLS; drain it first so this
            # exercises the actual zero-balance error path.
            wallet = await pokebox.get_or_create_wallet(s, empty_uid)
            wallet.balance = 0
            await pokebox.catch_submission(s, empty_uid, party_dir)
            await s.commit()
        raise AssertionError("expected CatchError for no pokeballs")
    except pokebox.CatchError as exc:
        assert "No pokeballs" in str(exc), exc
    print("catch OK: no-pokeballs error is clean")

    # /swap: promote a boxed mon into an active slot, demoting the incumbent to storage.
    async with db.session() as s:
        promoted, demoted = await pokebox.swap_mon(s, pb_uid, box_slot=1, active_slot=1)
        promoted_id, demoted_id = promoted.id, (demoted.id if demoted else None)
        await s.commit()
    async with db.session() as s:
        party = await pokebox.party_listing(s, pb_uid)
        box = await pokebox.box_listing(s, pb_uid)
    assert any(m.id == promoted_id and m.slot == 1 for m in party), party
    assert demoted_id is not None and any(m.id == demoted_id for m in box), box
    print("swap OK: promoted mon active in slot 1, demoted mon back in storage")

    # /release: deletes the CaughtMon row AND its cached file from disk (no relying on later GC).
    async with db.session() as s:
        box = await pokebox.box_listing(s, pb_uid)
    target = box[0]
    assert os.path.exists(target.cached_image_path), target.cached_image_path
    async with db.session() as s:
        released_path = await pokebox.release_mon(s, pb_uid, target.slot, is_active=False)
        await s.commit()
    pokebox.delete_cached_file(released_path)
    assert not os.path.exists(released_path), released_path
    async with db.session() as s:
        remaining_total = await pokebox.total_caught_count(s, pb_uid)
    assert remaining_total == 19, remaining_total
    print("release OK: DB row and cached file both removed")

    # Image normalization: an oversized image gets downsized to fit within
    # MAX_IMAGE_DIMENSION (aspect preserved), a smaller one is left alone.
    from PIL import Image as PILImage

    big = PILImage.new("RGB", (3000, 1500), (255, 0, 0))
    big_buf = io.BytesIO()
    big.save(big_buf, format="JPEG")
    big_path = os.path.join(tmp, "norm_big.png")
    pokebox._normalize_and_save(big_buf.getvalue(), big_path)
    with PILImage.open(big_path) as out:
        assert max(out.size) == pokebox.MAX_IMAGE_DIMENSION, out.size
        assert out.size[0] / out.size[1] == 2.0, out.size  # aspect preserved

    small = PILImage.new("RGBA", (200, 300), (0, 255, 0, 128))
    small_buf = io.BytesIO()
    small.save(small_buf, format="PNG")
    small_path = os.path.join(tmp, "norm_small.png")
    pokebox._normalize_and_save(small_buf.getvalue(), small_path)
    with PILImage.open(small_path) as out:
        assert out.size == (200, 300), out.size  # not upscaled
    print(f"image normalization OK: oversized capped at {pokebox.MAX_IMAGE_DIMENSION}px, small left alone")

    # --- Submission UX Fixes: header naming, grace/catch windows, nicknames, help split ---

    # Issue 1/5: shared display-name helper + header/confirmation copy name the Pokemon.
    from pokesketch.cogs.submissions import (
        _is_outside_grace_window,
        _submission_confirmation,
        _submission_header,
    )
    from pokesketch.formatting import species_display_name

    assert species_display_name("mr-mime") == "Mr Mime", species_display_name("mr-mime")
    header = _submission_header(species_display_name("pikachu"), "<@123>")
    assert header == "🖼️ Pikachu by <@123> — react 👍 to upvote!", header
    assert _submission_confirmation("Pikachu", True, " (+15 EXP)") == "✅ Pikachu submitted (+15 EXP)!"
    assert _submission_confirmation("Pikachu", False, "") == "✅ Updated your Pikachu sketch!"
    print("header/confirmation OK: both name the Pokemon via the shared display-name helper")

    # Issue 2: grace-window cutoff is a pure guild-local date comparison.
    _today = _date(2026, 9, 22)
    assert not _is_outside_grace_window(_today - _timedelta(days=7), _today, 7)  # exactly at boundary: inside
    assert _is_outside_grace_window(_today - _timedelta(days=8), _today, 7)  # one day past: outside
    print("grace window OK: boundary day still accepted, day after boundary rejected")

    # Issue 2: auto_archive_duration snaps to Discord's actual tiers (60/1440/4320/10080).
    from pokesketch.daily import _archive_duration_for_grace

    assert _archive_duration_for_grace(0) == 60
    assert _archive_duration_for_grace(1) == 1440
    assert _archive_duration_for_grace(3) == 4320
    assert _archive_duration_for_grace(4) == 10080
    assert _archive_duration_for_grace(7) == 10080  # default grace period: unchanged from today's hardcoded value
    assert _archive_duration_for_grace(30) == 10080  # capped at the largest tier
    print("archive duration OK: snaps to nearest tier, default 7 days -> 10080 (unchanged from before)")

    # Issue 2/3: /set-grace-period and /set-catch-window range validation + clamp-on-lower.
    from pokesketch.cogs.admin import _clamp_catch_window, _validate_catch_window, _validate_grace_period

    assert _validate_grace_period(1) and _validate_grace_period(30)
    assert not _validate_grace_period(0) and not _validate_grace_period(31)
    assert _validate_catch_window(24, 7) and _validate_catch_window(168, 7)
    assert not _validate_catch_window(169, 7) and not _validate_catch_window(0, 7)
    assert _clamp_catch_window(48, 1) == 24  # grace lowered to 1 day clamps a 48h window down to 24h
    assert _clamp_catch_window(12, 7) == 12  # already within range: unchanged
    print("grace/catch-window validators OK: ranges enforced, clamp only lowers when needed")

    # Issue 3: catch window eligibility is per-submission-guild and excludes already-caught rows.
    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=3, catch_window_hours=10))
        s.add(db.DailyPokemon(guild_id=3, local_date=_date.today(), dex_no=25, name="pikachu"))
        await s.commit()
        daily3 = (
            await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.guild_id == 3))
        ).scalar_one()
        window_uid = 5003
        now = _datetime.now(UTC)
        inside_sub = db.Submission(
            guild_id=3, user_id=window_uid, daily_id=daily3.id,
            image_url="https://example.invalid/inside.png", created_at=now - _timedelta(hours=9),
        )
        outside_sub = db.Submission(
            guild_id=3, user_id=window_uid, daily_id=daily3.id,
            image_url="https://example.invalid/outside.png", created_at=now - _timedelta(hours=11),
        )
        s.add_all([inside_sub, outside_sub])
        await s.commit()
        inside_id, outside_id = inside_sub.id, outside_sub.id

    async with db.session() as s:
        eligible = await pokebox.catchable_submissions(s, window_uid)
    eligible_ids = {sub.id for sub, _ in eligible}
    assert inside_id in eligible_ids, eligible_ids
    assert outside_id not in eligible_ids, eligible_ids
    print("catch window OK: just-inside submission eligible, just-outside excluded")

    # Issue 3 (latent double-catch bug fix): once caught, a submission drops out of
    # eligibility, and re-targeting it explicitly is rejected with a clean CatchError.
    async with db.session() as s:
        caught_mon = await pokebox.catch_submission(s, window_uid, party_dir, target_submission_id=inside_id)
        await s.commit()
    assert caught_mon.source_submission_id == inside_id

    async with db.session() as s:
        eligible_after = await pokebox.catchable_submissions(s, window_uid)
    assert inside_id not in {sub.id for sub, _ in eligible_after}
    print("double-catch fix OK: caught submission no longer eligible")

    try:
        async with db.session() as s:
            await pokebox.catch_submission(s, window_uid, party_dir, target_submission_id=inside_id)
            await s.commit()
        raise AssertionError("expected CatchError for a stale/already-caught target")
    except pokebox.CatchError as exc:
        assert "no longer catchable" in str(exc), exc
    print("catch targeting OK: stale/already-caught target rejected cleanly, not a crash")

    # Issue 3: /catch targeting precedence — no target picks the latest eligible;
    # an explicit target overrides that default.
    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=4))
        s.add(db.DailyPokemon(guild_id=4, local_date=_date.today(), dex_no=1, name="bulbasaur"))
        await s.commit()
        daily4 = (
            await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.guild_id == 4))
        ).scalar_one()
        precedence_uid = 5005
        older_sub = db.Submission(
            guild_id=4, user_id=precedence_uid, daily_id=daily4.id,
            image_url="https://example.invalid/older.png",
        )
        s.add(older_sub)
        await s.flush()
        newer_sub = db.Submission(
            guild_id=4, user_id=precedence_uid, daily_id=daily4.id,
            image_url="https://example.invalid/newer.png",
        )
        s.add(newer_sub)
        await s.commit()
        older_id, newer_id = older_sub.id, newer_sub.id

    async with db.session() as s:
        default_mon = await pokebox.catch_submission(s, precedence_uid, party_dir)
        await s.commit()
    assert default_mon.source_submission_id == newer_id, (default_mon.source_submission_id, newer_id)

    async with db.session() as s:
        targeted_mon = await pokebox.catch_submission(
            s, precedence_uid, party_dir, target_submission_id=older_id
        )
        await s.commit()
    assert targeted_mon.source_submission_id == older_id
    print("catch precedence OK: no target -> latest eligible; explicit target -> that one")

    # Issue 4: sanitize_nickname — valid nicknames pass through trimmed, each rejected-char
    # class raises CatchError, over-length rejects, empty-after-trim means "no nickname".
    assert pokebox.sanitize_nickname(" Sparky ") == "Sparky"
    assert pokebox.sanitize_nickname("T-Rex") == "T-Rex"
    assert pokebox.sanitize_nickname("Sparky Jr.") == "Sparky Jr."
    assert pokebox.sanitize_nickname("   ") is None
    assert pokebox.sanitize_nickname("") is None

    try:
        pokebox.sanitize_nickname("ThisIsWayTooLong")
        raise AssertionError("expected CatchError for an over-length nickname")
    except pokebox.CatchError as exc:
        assert "12 characters" in str(exc), exc

    for bad in ["Sp@rky", "Sp#rky", "<Tag>", "back`tick", "na:me", "we,ird", "a​b"]:
        try:
            pokebox.sanitize_nickname(bad)
            raise AssertionError(f"expected CatchError for {bad!r}")
        except pokebox.CatchError:
            pass
    print("sanitize_nickname OK: valid trimmed through, bad chars/over-length/blank all handled")

    # Issue 4: _display_name shows "{nickname} ({species})" when set, plain species otherwise.
    from pokesketch.cogs.collection import _display_name as _collection_display_name

    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=6))
        s.add(db.DailyPokemon(guild_id=6, local_date=_date.today(), dex_no=25, name="pikachu"))
        await s.commit()
        daily6 = (
            await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.guild_id == 6))
        ).scalar_one()
        nick_uid = 5006
        s.add(
            db.Submission(
                guild_id=6, user_id=nick_uid, daily_id=daily6.id,
                image_url="https://example.invalid/nick.png",
            )
        )
        await s.commit()

    async with db.session() as s:
        nick_mon = await pokebox.catch_submission(s, nick_uid, party_dir, nickname="Sparky")
        await s.commit()
    assert nick_mon.nickname == "Sparky", nick_mon.nickname
    assert _collection_display_name(nick_mon) == "Sparky (#0025 Pikachu)", _collection_display_name(nick_mon)
    assert _collection_display_name(caught_mons[0]) == "#0007 Squirtle", _collection_display_name(caught_mons[0])
    print("display name OK: nickname shown as 'Sparky (#0025 Pikachu)', unset stays plain species")

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

    # Issue 6: /help-admin's content mentions the two new grace/catch-window settings.
    from pokesketch.cogs.help import _admin_field_value

    admin_text = _admin_field_value()
    assert "/set-grace-period" in admin_text, admin_text
    assert "/set-catch-window" in admin_text, admin_text
    print("help-admin OK: admin command list includes the grace-period and catch-window settings")

    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
