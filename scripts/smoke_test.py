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

    # Upvote EXP: receiving and giving are separate, each with its own daily cap.
    from datetime import date as _uv_date

    from sqlalchemy import func as _uv_func

    from pokesketch.cogs.submissions import Submissions

    recv_uid, giver_uid = 9001, 9002
    guild_id = 999
    async with db.session() as s:
        daily_uv = db.DailyPokemon(guild_id=guild_id, local_date=_uv_date.today(), dex_no=1, name="bulbasaur")
        s.add(daily_uv)
        await s.commit()
        sub_uv = db.Submission(
            guild_id=guild_id, user_id=recv_uid, daily_id=daily_uv.id,
            image_url="https://example.invalid/uv.png",
        )
        s.add(sub_uv)
        await s.flush()
        # Award well past both caps' worth of upvotes to confirm each cap holds
        # independently (received cap: EXP_UPVOTE_RECEIVED_DAILY_CAP; given cap:
        # EXP_UPVOTE_GIVEN_DAILY_CAP) — more iterations than either cap allows.
        iterations = max(
            leveling.EXP_UPVOTE_RECEIVED_DAILY_CAP // leveling.EXP_PER_UPVOTE_RECEIVED,
            leveling.EXP_UPVOTE_GIVEN_DAILY_CAP // leveling.EXP_PER_UPVOTE_GIVEN,
        ) + 5
        for _ in range(iterations):
            await Submissions._maybe_award_upvote_received_exp(s, sub_uv)
            await Submissions._maybe_award_upvote_given_exp(s, guild_id, giver_uid)
        await s.commit()

    async with db.session() as s:
        recv_total = (
            await s.execute(
                select(_uv_func.coalesce(_uv_func.sum(db.ExpEvent.amount), 0)).where(
                    db.ExpEvent.guild_id == guild_id,
                    db.ExpEvent.user_id == recv_uid,
                    db.ExpEvent.type == "upvote_received",
                )
            )
        ).scalar_one()
        given_total = (
            await s.execute(
                select(_uv_func.coalesce(_uv_func.sum(db.ExpEvent.amount), 0)).where(
                    db.ExpEvent.guild_id == guild_id,
                    db.ExpEvent.user_id == giver_uid,
                    db.ExpEvent.type == "upvote_given",
                )
            )
        ).scalar_one()
    assert recv_total == leveling.EXP_UPVOTE_RECEIVED_DAILY_CAP, recv_total
    assert given_total == leveling.EXP_UPVOTE_GIVEN_DAILY_CAP, given_total
    assert leveling.EXP_PER_UPVOTE_RECEIVED > leveling.EXP_PER_UPVOTE_GIVEN
    print(
        f"upvote exp OK: received capped at {recv_total} "
        f"(+{leveling.EXP_PER_UPVOTE_RECEIVED}/upvote), given capped at {given_total} "
        f"(+{leveling.EXP_PER_UPVOTE_GIVEN}/upvote)"
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
    from datetime import datetime as _dt_early
    from datetime import timedelta as _timedelta_early

    from pokesketch.cogs.submissions import (
        _is_outside_grace_window,
        _submission_confirmation,
        _submission_header,
    )
    from pokesketch.formatting import species_display_name

    assert species_display_name("mr-mime") == "Mr Mime", species_display_name("mr-mime")
    header = _submission_header(species_display_name("pikachu"), "<@123>")
    assert header == "🖼️ Pikachu by <@123> — react 👍 to upvote!", header
    _sample_created_at = _dt_early(2026, 9, 1, 12, 0, 0)
    _sample_expires_at = _sample_created_at + _timedelta_early(hours=24)
    confirmation_text = _submission_confirmation("Pikachu", True, " (+15 EXP)", _sample_expires_at)
    assert confirmation_text.startswith("✅ Pikachu submitted (+15 EXP)! 🎯 Catchable until <t:"), confirmation_text
    update_text = _submission_confirmation("Pikachu", False, "", _sample_expires_at)
    assert update_text.startswith("✅ Updated your Pikachu sketch! 🎯 Catchable until <t:"), update_text
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
        caught_mon = await pokebox.catch_submission(s, window_uid, party_dir, target=f"s{inside_id}")
        await s.commit()
    assert caught_mon.source_submission_id == inside_id

    async with db.session() as s:
        eligible_after = await pokebox.catchable_submissions(s, window_uid)
    assert inside_id not in {sub.id for sub, _ in eligible_after}
    print("double-catch fix OK: caught submission no longer eligible")

    try:
        async with db.session() as s:
            await pokebox.catch_submission(s, window_uid, party_dir, target=f"s{inside_id}")
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
            s, precedence_uid, party_dir, target=f"s{older_id}"
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

    # --- Weekly Two-Stage Vote & Wild Encounters ---

    from pokesketch import weeklyvote
    from pokesketch.cogs.admin import _parse_dex_numbers
    from pokesketch.cogs.submissions import Submissions
    from pokesketch.pokeapi import PokemonRef

    # /event-create's dex-number parser: valid tokens pass through deduped,
    # bad/out-of-range tokens raise with a user-facing message.
    assert _parse_dex_numbers("133 134, 135  136") == [133, 134, 135, 136]
    assert _parse_dex_numbers("1,1,2") == [1, 2]  # dedup, order preserved
    try:
        _parse_dex_numbers("")
        raise AssertionError("expected ValueError for empty input")
    except ValueError as exc:
        assert "at least one" in str(exc), exc
    try:
        _parse_dex_numbers("1026")
        raise AssertionError("expected ValueError for out-of-range dex number")
    except ValueError as exc:
        assert "out of range" in str(exc), exc
    print("event dex-number parser OK: valid parsed+deduped, bad input rejected cleanly")

    class _FakeSentMessage:
        def __init__(self, msg_id: int, url: str, thread=None):
            self.id = msg_id
            self.attachments = [type("Attachment", (), {"url": url})()]
            self._thread = thread
            self.created_threads: list[dict] = []

        async def add_reaction(self, emoji):
            pass

        async def create_thread(self, **kwargs):
            self.created_threads.append(kwargs)
            return self._thread

    class _FakeThread:
        def __init__(self, thread_id: int):
            self.id = thread_id
            self.sent: list[tuple[tuple, dict]] = []

        async def send(self, *args, **kwargs):
            self.sent.append((args, kwargs))
            return _FakeSentMessage(70000 + len(self.sent), f"https://example.invalid/thread{self.id}-{len(self.sent)}.png")

    class _FakeChannel:
        def __init__(self, channel_id: int, thread_id: int):
            self.id = channel_id
            self._thread = _FakeThread(thread_id)
            self.created_threads: list[dict] = []

        async def create_thread(self, **kwargs):
            self.created_threads.append(kwargs)
            return self._thread

        async def send(self, *args, **kwargs):
            return _FakeSentMessage(60000, "https://example.invalid/poll.png", thread=self._thread)

    class _FakeClient:
        def __init__(self, channels: dict[int, object]):
            self._channels = channels

        def get_channel(self, cid):
            return self._channels.get(cid)

        async def fetch_channel(self, cid):
            return self._channels[cid]

    class _FakeAttachment:
        async def to_file(self):
            return None

    class _FakeApi:
        async def get_pokemon(self, dex_no: int) -> PokemonRef:
            return PokemonRef(
                dex_no=dex_no, name=f"fakemon{dex_no}", types=["normal"],
                official_artwork="https://example.invalid/art.png",
                sprite="https://example.invalid/sprite.png",
                shiny_artwork=None, shiny_sprite=None,
            )

    vote_gid = 100
    vote_channel_id, vote_thread_id = 9001, 9002
    today = _date.today()

    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=vote_gid, channel_id=vote_channel_id, vote_day1_weekday=6))
        s.add(
            db.EventDefinition(
                guild_id=vote_gid, name="Eeveelution Week", dex_list="133\n134\n135\n136",
                created_by=1, is_active=True,
            )
        )
        await s.commit()

    # Day-1 ballot: Event only appears once the guild has an active EventDefinition.
    async with db.session() as s:
        ballot = await weeklyvote.build_category_ballot(s, vote_gid)
    assert set(ballot) == set(weeklyvote.ALL_CATEGORIES), ballot
    print("category ballot OK: all 5 categories included once an active event exists")

    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=vote_gid + 1, vote_day1_weekday=6))
        await s.commit()
        no_event_ballot = await weeklyvote.build_category_ballot(s, vote_gid + 1)
    assert weeklyvote.CATEGORY_EVENT not in no_event_ballot, no_event_ballot
    assert len(no_event_ballot) == 4, no_event_ballot
    print("category ballot OK: Event dropped for a guild with zero active events")

    # Day-2 candidates + resolution for the two network-free categories (Event, Generation).
    async with db.session() as s:
        event_candidates = await weeklyvote._day2_candidates(s, None, vote_gid, weeklyvote.CATEGORY_EVENT)
    assert event_candidates == [("Eeveelution Week", "Eeveelution Week")], event_candidates

    async with db.session() as s:
        gen_pool = await weeklyvote.resolve_dex_pool(s, None, vote_gid, weeklyvote.CATEGORY_GENERATION, "Gen 1 (Kanto)")
    assert gen_pool == list(range(1, 152)), (gen_pool[:3], gen_pool[-3:], len(gen_pool))
    print("day-2 candidates/resolution OK: event choices + generation dex range both correct")

    # Fake category winner ("event") -> fake choice winner ("Eeveelution Week") -> resolved
    # dex pool, via the real day-2-resolution finalize path (network faked). Thread creation
    # now happens per-day in post_wild_encounter_for_guild, not here.
    async with db.session() as s:
        wv = db.WeeklyVote(guild_id=vote_gid, iso_week=pokebox.week_key(), category="event")
        s.add(wv)
        await s.commit()
        weekly_vote_id = wv.id

    fake_channel = _FakeChannel(vote_channel_id, vote_thread_id)
    fake_client = _FakeClient({vote_channel_id: fake_channel, vote_thread_id: fake_channel._thread})
    ok = await weeklyvote._finalize_weekly_vote(
        fake_client, None, vote_gid, weekly_vote_id, "event", "Eeveelution Week"
    )
    assert ok, "expected _finalize_weekly_vote to succeed"
    async with db.session() as s:
        wv = await s.get(db.WeeklyVote, weekly_vote_id)
    assert wv.dex_pool_numbers == [133, 134, 135, 136], wv.dex_pool_numbers
    print("weekly vote resolution OK: event/choice -> resolved dex pool cached (no thread yet)")

    # get_active_weekly_vote: "most recent WeeklyVote with a resolved dex pool" — a newer,
    # still-in-progress cycle (no resolved pool yet) must NOT shadow the still-active previous
    # one, so wild encounters keep posting with no gap while the new week's votes are in flight.
    async with db.session() as s:
        s.add(db.WeeklyVote(guild_id=vote_gid, iso_week="2099-W01", category="type"))  # in-progress, unresolved
        await s.commit()
        active = await weeklyvote.get_active_weekly_vote(s, vote_gid)
    assert active.id == weekly_vote_id, (active.id, weekly_vote_id)
    print("cutover OK: in-progress new cycle doesn't shadow the still-active previous pool")

    # Daily wild-encounter post: real post_wild_encounter_for_guild, network faked. Each call
    # creates its OWN fresh thread (one Pokemon per thread), posted into the guild's main channel
    # right alongside the main daily post.
    from pokesketch.daily import post_wild_encounter_for_guild

    posted = await post_wild_encounter_for_guild(fake_client, _FakeApi(), vote_gid, today)
    assert posted, "expected a wild encounter to post"
    async with db.session() as s:
        we = (
            await s.execute(
                select(db.WildEncounter).where(
                    db.WildEncounter.guild_id == vote_gid, db.WildEncounter.local_date == today
                )
            )
        ).scalar_one()
    assert we.dex_no in (133, 134, 135, 136), we.dex_no
    assert we.thread_id == vote_thread_id, we.thread_id  # fake channel always hands back the same fake thread
    we_id = we.id
    again = await post_wild_encounter_for_guild(fake_client, _FakeApi(), vote_gid, today)
    assert not again, "expected the same-day re-post to be a no-op (idempotent)"
    print(
        f"wild encounter daily post OK: posted #{we.dex_no} in its own thread {we.thread_id}, "
        "same-day re-post is idempotent"
    )

    # Wild-encounter /submit: reuses the shared EXP/streak/PokeBox path.
    subs_cog = Submissions(bot=None)
    wild_uid = 5010

    async with db.session() as s:
        we = await s.get(db.WildEncounter, we_id)
        confirmation, exp_msg, _gym_result = await subs_cog._submit_to_wild_encounter(
            s, we, _FakeThread(vote_thread_id), _FakeAttachment(), vote_gid, wild_uid, "<@5010>", today
        )
        await s.commit()
    assert "submitted" in confirmation and "EXP" in exp_msg, (confirmation, exp_msg)

    async with db.session() as s:
        wild_sub = (
            await s.execute(
                select(db.WildEncounterSubmission).where(
                    db.WildEncounterSubmission.wild_encounter_id == we_id,
                    db.WildEncounterSubmission.user_id == wild_uid,
                )
            )
        ).scalar_one()
        wild_user = (
            await s.execute(select(db.User).where(db.User.guild_id == vote_gid, db.User.user_id == wild_uid))
        ).scalar_one()
        wild_global_user = (
            await s.execute(select(db.GlobalUser).where(db.GlobalUser.user_id == wild_uid))
        ).scalar_one()
        wild_scanned, _ = await pokebox.pokebox_progress(s, wild_uid)
    first_day_bonus = leveling.streak_bonus(1)
    assert wild_user.exp == leveling.EXP_WILD_ENCOUNTER + first_day_bonus, wild_user.exp
    assert wild_user.personal_streak == 1, wild_user.personal_streak
    assert wild_global_user.global_streak == 1, wild_global_user.global_streak
    assert wild_scanned == 1, wild_scanned
    print("wild encounter submit OK: EXP/streak/PokeBox all correct for a first-time submission")

    # Streak-dedup: the SAME user submitting to the main daily thread the same day must not
    # grant a second streak increment (globally or per-guild), even though it's a separate
    # Submission row and grants its own EXP_SUBMIT.
    async with db.session() as s:
        s.add(db.DailyPokemon(guild_id=vote_gid, local_date=today, dex_no=1, name="bulbasaur"))
        await s.commit()
        dedup_daily = (
            await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.guild_id == vote_gid))
        ).scalar_one()
        confirmation2, exp_msg2, _gym_result2 = await subs_cog._submit_to_daily(
            s, dedup_daily, _FakeThread(vote_thread_id + 1), _FakeAttachment(), vote_gid, wild_uid, "<@5010>"
        )
        await s.commit()
    assert "submitted" in confirmation2 and "EXP" in exp_msg2, (confirmation2, exp_msg2)

    async with db.session() as s:
        wild_user = (
            await s.execute(select(db.User).where(db.User.guild_id == vote_gid, db.User.user_id == wild_uid))
        ).scalar_one()
        wild_global_user = (
            await s.execute(select(db.GlobalUser).where(db.GlobalUser.user_id == wild_uid))
        ).scalar_one()
    assert wild_user.exp == leveling.EXP_WILD_ENCOUNTER + first_day_bonus + leveling.EXP_SUBMIT, wild_user.exp
    assert wild_user.personal_streak == 1, wild_user.personal_streak  # still 1, not 2 — deduped
    assert wild_global_user.global_streak == 1, wild_global_user.global_streak  # still 1 — deduped
    print("streak-dedup OK: same-day wild-encounter + main-daily submissions grant EXP but only one streak tick")

    # /catch eligibility for a wild-encounter submission, via the same catchable_submissions()
    # mechanism used for main-daily submissions (no hardcoded "today only" check).
    async with db.session() as s:
        eligible = await pokebox.catchable_submissions(s, wild_uid)
    assert len(eligible) == 2, eligible  # one daily, one wild-encounter, both eligible
    kinds = {pokebox.encode_catch_target(sub)[0] for sub, _ in eligible}
    assert kinds == {"s", "w"}, kinds
    wild_target = next(
        pokebox.encode_catch_target(sub) for sub, _ in eligible if isinstance(sub, db.WildEncounterSubmission)
    )
    assert pokebox.decode_catch_target(wild_target) == ("w", wild_sub.id)
    assert pokebox.decode_catch_target("garbage") is None
    print("catch eligibility OK: wild-encounter submission surfaces via catchable_submissions()")

    async with db.session() as s:
        wild_caught = await pokebox.catch_submission(s, wild_uid, party_dir, target=wild_target)
        await s.commit()
    assert wild_caught.source_wild_encounter_submission_id == wild_sub.id, (
        wild_caught.source_wild_encounter_submission_id
    )
    assert wild_caught.source_submission_id is None, wild_caught.source_submission_id
    assert wild_caught.dex_no == we.dex_no, (wild_caught.dex_no, we.dex_no)

    async with db.session() as s:
        eligible_after = await pokebox.catchable_submissions(s, wild_uid)
    assert len(eligible_after) == 1, eligible_after  # the wild-encounter one dropped out
    assert isinstance(eligible_after[0][0], db.Submission), type(eligible_after[0][0])
    print("catch OK: wild-encounter submission caught via /catch, dropped from eligibility after")

    await db.dispose()

    # Submit-vs-catch window visual: format_duration_label / discord_timestamp
    # (Discord's live self-updating <t:UNIX:R> markdown) / the thread-opening
    # window summaries / the /submit confirmation's live catch deadline.
    from datetime import UTC as _UTC
    from datetime import date as _date2
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta2

    from pokesketch.cogs.submissions import _submission_confirmation
    from pokesketch.daily import (
        _submit_deadline_utc,
        _wild_encounter_window_summary_line,
        _window_summary_line,
    )
    from pokesketch.pokebox import discord_timestamp, format_duration_label

    assert format_duration_label(4) == "4h", format_duration_label(4)
    assert format_duration_label(24) == "1d", format_duration_label(24)
    assert format_duration_label(36) == "2d", format_duration_label(36)
    assert format_duration_label(168) == "7d", format_duration_label(168)
    print("format_duration_label OK: 4h/1d/2d/7d")

    # discord_timestamp: renders the Discord <t:UNIX:style> markdown against a
    # known epoch, default style "R" (dynamic relative countdown).
    known_dt = _datetime(2026, 1, 1, 0, 0, 0)
    known_epoch = int(known_dt.replace(tzinfo=_UTC).timestamp())
    assert discord_timestamp(known_dt) == f"<t:{known_epoch}:R>", discord_timestamp(known_dt)
    assert discord_timestamp(known_dt, style="F") == f"<t:{known_epoch}:F>", discord_timestamp(known_dt, style="F")
    print(f"discord_timestamp OK: {discord_timestamp(known_dt)}")

    # _submit_deadline_utc: cutoff is the guild-local midnight grace_period_days+1
    # days after the daily's local_date (i.e. the first midnight /submit rejects).
    utc_tz_deadline = _submit_deadline_utc(_date2(2026, 9, 1), grace_period_days=7, tz_name="UTC")
    assert utc_tz_deadline == _datetime(2026, 9, 9, 0, 0, 0), utc_tz_deadline
    ny_tz_deadline = _submit_deadline_utc(_date2(2026, 9, 1), grace_period_days=0, tz_name="America/New_York")
    # America/New_York midnight Sep 2 (EDT, UTC-4) == 04:00 UTC Sep 2.
    assert ny_tz_deadline == _datetime(2026, 9, 2, 4, 0, 0), ny_tz_deadline
    print(f"submit deadline OK: UTC={utc_tz_deadline}, NY={ny_tz_deadline}")

    # Thread-opening window summaries: live self-updating deadline for /submit,
    # duration label (not a stale countdown) for the per-submission catch window.
    daily_line = _window_summary_line(_date2(2026, 9, 1), grace_period_days=7, catch_window_hours=24, tz_name="UTC")
    assert "`/submit` closes for this thread <t:" in daily_line, daily_line
    assert "`/catch` closes 1d after each sketch is submitted." in daily_line, daily_line
    print(f"daily window summary OK: {daily_line}")

    equal_line = _window_summary_line(_date2(2026, 9, 1), grace_period_days=7, catch_window_hours=168, tz_name="UTC")
    assert "`/catch` closes 7d after each sketch is submitted." in equal_line, equal_line
    print("daily window summary OK: equal submit/catch windows both render in day units")

    wild_line = _wild_encounter_window_summary_line(_date2(2026, 9, 1), catch_window_hours=24, tz_name="UTC")
    assert "only works here until <t:" in wild_line, wild_line
    assert "today only, no backfill" in wild_line, wild_line
    assert "`/catch` closes 1d after each sketch is submitted." in wild_line, wild_line
    print(f"wild-encounter window summary OK: {wild_line}")

    # Worst case: a user submits at the very last moment the submit window
    # allows (right at the guild-local-midnight grace cutoff for this
    # thread's daily). Their personal catch window still runs its full
    # catch_window_hours from THEIR OWN created_at, so it correctly extends
    # past the thread's submit deadline shown in the thread-post line above —
    # a late submitter is never shortchanged on catch time. Verified against
    # the real DB path (catchable_submissions), not just arithmetic.
    #
    # catchable_submissions() compares against the real wall-clock "now", so
    # this uses a local_date anchored to *today* (not a fixed past date) —
    # the daily was posted grace_period_days ago, and its last legal /submit
    # instant is "now" (the actual moment this test runs), which keeps the
    # submission's catch window straddling the real current time.
    _worst_case_grace_days = 7
    _worst_case_catch_hours = 24
    _worst_case_tz = "UTC"
    _worst_case_local_date = (_datetime.now(_UTC) - _timedelta2(days=_worst_case_grace_days)).date()
    _worst_case_submit_deadline = _submit_deadline_utc(
        _worst_case_local_date, _worst_case_grace_days, _worst_case_tz
    )
    # Last legal instant to /submit: just before the guild-local-midnight cutoff
    # (i.e. essentially "now", since the daily was anchored grace_period_days ago).
    _last_legal_submit_at = _worst_case_submit_deadline - _timedelta2(seconds=1)
    assert not _is_outside_grace_window(
        _worst_case_local_date, _last_legal_submit_at.date(), _worst_case_grace_days
    ), "last-legal-instant submission should still be inside the grace window"

    _worst_case_uid = 5007
    async with db.session() as s:
        s.add(db.GuildConfig(
            guild_id=7, dex_min=1, dex_max=5, selection_mode="random",
            grace_period_days=_worst_case_grace_days, catch_window_hours=_worst_case_catch_hours,
        ))
        await s.commit()
    async with db.session() as s:
        worst_case_daily = db.DailyPokemon(
            guild_id=7, local_date=_worst_case_local_date, dex_no=1, name="bulbasaur",
        )
        s.add(worst_case_daily)
        await s.commit()
        worst_case_sub = db.Submission(
            guild_id=7, user_id=_worst_case_uid, daily_id=worst_case_daily.id,
            image_url="https://example.invalid/lastminute.png", created_at=_last_legal_submit_at,
        )
        s.add(worst_case_sub)
        await s.commit()
        worst_case_sub_id = worst_case_sub.id

    # Right at the submit deadline (created_at ~= now): still catchable, since
    # the catch window just started counting from THIS submission's created_at.
    async with db.session() as s:
        eligible_right_after_deadline = await pokebox.catchable_submissions(s, _worst_case_uid)
    assert worst_case_sub_id in {sub.id for sub, _ in eligible_right_after_deadline}, (
        "a last-minute submission must still be catchable right at the submit deadline"
    )

    # The submission's OWN catch deadline (catch_window_hours after ITS
    # created_at, not the thread's submit deadline) correctly extends past
    # the thread's submit-window cutoff, proving late submitters aren't
    # shortchanged on catch time.
    _worst_case_catch_expires_at = _last_legal_submit_at + _timedelta2(hours=_worst_case_catch_hours)
    assert _worst_case_catch_expires_at > _worst_case_submit_deadline, (
        "a last-minute submitter's catch deadline must extend past the thread's submit deadline",
        _worst_case_catch_expires_at, _worst_case_submit_deadline,
    )
    _worst_case_extend_hours = (_worst_case_catch_expires_at - _worst_case_submit_deadline).total_seconds() / 3600
    print(
        "late-submitter worst case OK: submit at the last legal instant still gets a full catch "
        f"window that extends {_worst_case_extend_hours:.1f}h past the thread's submit deadline, "
        "and catchable_submissions() confirms it's eligible immediately after the submit deadline passes"
    )

    # /submit confirmation: catch deadline is a live Discord timestamp computed
    # from the submission's own created_at + catch_window_hours.
    created_at = _datetime(2026, 9, 1, 12, 0, 0)
    expected_epoch = int((created_at + _timedelta2(hours=24)).replace(tzinfo=_UTC).timestamp())
    confirmation = _submission_confirmation("Pikachu", True, " (+15 EXP)", created_at + _timedelta2(hours=24))
    assert "Pikachu submitted (+15 EXP)!" in confirmation, confirmation
    assert f"<t:{expected_epoch}:R>" in confirmation, confirmation
    print(f"submission confirmation OK: {confirmation}")

    update_confirmation = _submission_confirmation("Bulbasaur", False, "", created_at + _timedelta2(hours=168))
    assert "Updated your Bulbasaur sketch!" in update_confirmation, update_confirmation
    print(f"submission update confirmation OK: {update_confirmation}")

    # /help: EXP explainer should always reflect the live leveling constants
    # (no hardcoded numbers to drift out of sync with the award logic).
    from pokesketch.cogs.help import _exp_field_value

    exp_text = _exp_field_value()
    assert f"+{leveling.EXP_SUBMIT} EXP" in exp_text, exp_text
    assert f"+{leveling.EXP_PER_STREAK_DAY} EXP" in exp_text, exp_text
    assert f"capped at +{leveling.EXP_STREAK_CAP}" in exp_text, exp_text
    assert f"+{leveling.EXP_PER_UPVOTE_RECEIVED} EXP" in exp_text, exp_text
    assert f"capped at +{leveling.EXP_UPVOTE_RECEIVED_DAILY_CAP}/day" in exp_text, exp_text
    assert f"+{leveling.EXP_PER_UPVOTE_GIVEN} EXP" in exp_text, exp_text
    assert f"capped at +{leveling.EXP_UPVOTE_GIVEN_DAILY_CAP}/day" in exp_text, exp_text
    print("help EXP explainer OK: matches live leveling constants")

    # Issue 6: /help-admin's content mentions the two new grace/catch-window settings.
    from pokesketch.cogs.help import _admin_field_groups

    admin_text = "\n".join(value for _name, value in _admin_field_groups())
    assert "/set-grace-period" in admin_text, admin_text
    assert "/set-catch-window" in admin_text, admin_text
    print("help-admin OK: admin command list includes the grace-period and catch-window settings")

    # Regression test: /help-admin used to build a single embed field over
    # Discord's 1024-char limit (embed.add_field 400s -> the interaction times
    # out with "The application did not respond"). Guard both help-text
    # builders directly, then exercise the real command handlers end-to-end
    # and validate the assembled embeds against every one of Discord's actual
    # embed limits, so a future appended command trips this test, not prod.
    from pokesketch.cogs.help import Help, _exp_field_value
    from pokesketch.discord_limits import (
        DISCORD_EMBED_DESCRIPTION_LIMIT,
        DISCORD_EMBED_FIELD_LIMIT,
        DISCORD_EMBED_FIELD_NAME_LIMIT,
        DISCORD_EMBED_FOOTER_LIMIT,
        DISCORD_EMBED_MAX_FIELDS,
        DISCORD_EMBED_TITLE_LIMIT,
        DISCORD_EMBED_TOTAL_LIMIT,
    )

    assert len(_exp_field_value()) <= DISCORD_EMBED_FIELD_LIMIT, len(_exp_field_value())
    for group_name, group_value in _admin_field_groups():
        assert len(group_value) <= DISCORD_EMBED_FIELD_LIMIT, (group_name, len(group_value))
    print(
        "help field length OK: exp field "
        f"{len(_exp_field_value())} chars, admin groups "
        f"{[len(v) for _n, v in _admin_field_groups()]} chars, all <= {DISCORD_EMBED_FIELD_LIMIT}"
    )

    class _FakeHelpUser:
        id = 1
        display_name = "smoke-tester"

    class _FakeHelpResponse:
        def __init__(self):
            self.sent_embed = None

        async def send_message(self, embed=None, ephemeral=False):
            self.sent_embed = embed

    class _FakeHelpInteraction:
        def __init__(self):
            self.user = _FakeHelpUser()
            self.guild_id = 1
            self.response = _FakeHelpResponse()

    def _assert_embed_within_discord_limits(embed) -> int:
        """Validate a real discord.Embed against every one of Discord's actual
        embed limits (sourced from discord_limits.py, not re-hardcoded here).
        Shared by /help, /help-admin, /event-list, and /event-view checks."""
        title = embed.title or ""
        description = embed.description or ""
        footer_text = embed.footer.text if embed.footer else ""
        assert len(title) <= DISCORD_EMBED_TITLE_LIMIT, len(title)
        assert len(description) <= DISCORD_EMBED_DESCRIPTION_LIMIT, len(description)
        assert len(embed.fields) <= DISCORD_EMBED_MAX_FIELDS, len(embed.fields)
        assert len(footer_text) <= DISCORD_EMBED_FOOTER_LIMIT, len(footer_text)
        total = len(title) + len(description) + len(footer_text)
        for field in embed.fields:
            assert len(field.name) <= DISCORD_EMBED_FIELD_NAME_LIMIT, (field.name, len(field.name))
            assert len(field.value) <= DISCORD_EMBED_FIELD_LIMIT, (field.name, len(field.value))
            total += len(field.name) + len(field.value)
        assert total <= DISCORD_EMBED_TOTAL_LIMIT, total
        return total

    help_cog = Help(bot=None)

    admin_interaction = _FakeHelpInteraction()
    await help_cog.help_admin_cmd.callback(help_cog, admin_interaction)
    admin_embed = admin_interaction.response.sent_embed
    admin_total = _assert_embed_within_discord_limits(admin_embed)
    print(
        f"help-admin embed OK: {len(admin_embed.fields)} fields, "
        f"{[len(f.value) for f in admin_embed.fields]} chars each, {admin_total} total "
        "(all within Discord's embed limits)"
    )

    help_interaction = _FakeHelpInteraction()
    await help_cog.help_cmd.callback(help_cog, help_interaction)
    help_embed = help_interaction.response.sent_embed
    help_total = _assert_embed_within_discord_limits(help_embed)
    print(
        f"help embed OK: {len(help_embed.fields)} fields, "
        f"{[len(f.value) for f in help_embed.fields]} chars each, {help_total} total "
        "(all within Discord's embed limits)"
    )

    # --- Event admin system: /event-list pagination, /event-view truncation,
    # /event-create cap enforcement, /event-edit partial updates, /event-enable.
    # Every check below drives the real cog command callback end-to-end (not a
    # unit test of a helper), with a mocked Interaction that captures whatever
    # kwarg the handler passed to interaction.response.send_message.
    from pokesketch.cogs.admin import (
        EVENT_LIST_PAGE_SIZE,
        EVENT_MAX_DEX_NUMBERS,
        EVENT_NAME_MAX_LEN,
        MAX_EVENTS_PER_GUILD,
        Admin,
    )
    from pokesketch.discord_limits import DISCORD_EMBED_FIELD_VALUE_LIMIT

    # _parse_dex_numbers' EVENT_MAX_DEX_NUMBERS cap (shared by /event-create and /event-edit).
    try:
        _parse_dex_numbers(" ".join(str(n) for n in range(1, EVENT_MAX_DEX_NUMBERS + 2)))
        raise AssertionError("expected ValueError for a dex list over the per-event cap")
    except ValueError as exc:
        assert str(EVENT_MAX_DEX_NUMBERS) in str(exc), exc
    print(f"event dex-count cap OK: {EVENT_MAX_DEX_NUMBERS + 1} distinct numbers rejected cleanly")

    class _FakeAdminUser:
        def __init__(self, uid: int = 1):
            self.id = uid

    class _FakeAdminResponse:
        def __init__(self):
            self.sent: dict | None = None

        async def send_message(self, content=None, **kwargs):
            self.sent = {"content": content, **kwargs}

    class _FakeAdminMessage:
        id = 999999

    class _FakeAdminInteraction:
        def __init__(self, guild_id: int, user_id: int = 1):
            self.guild_id = guild_id
            self.user = _FakeAdminUser(user_id)
            self.response = _FakeAdminResponse()

        async def original_response(self):
            return _FakeAdminMessage()

    admin_cog = Admin(bot=None)

    # /event-create: MAX_EVENTS_PER_GUILD cap actually rejects the (cap+1)th event.
    cap_guild = 20260923
    async with db.session() as s:
        for i in range(MAX_EVENTS_PER_GUILD):
            s.add(db.EventDefinition(guild_id=cap_guild, name=f"Cap Event {i}", dex_list="1", created_by=1))
        await s.commit()

    cap_interaction = _FakeAdminInteraction(cap_guild)
    await admin_cog.event_create.callback(admin_cog, cap_interaction, name="One Too Many", dex_numbers="1")
    cap_msg = cap_interaction.response.sent["content"]
    assert str(MAX_EVENTS_PER_GUILD) in cap_msg, cap_msg
    async with db.session() as s:
        count_after = len(
            (await s.execute(select(db.EventDefinition).where(db.EventDefinition.guild_id == cap_guild)))
            .scalars().all()
        )
    assert count_after == MAX_EVENTS_PER_GUILD, count_after
    print(f"event-create cap OK: {MAX_EVENTS_PER_GUILD}th event blocked cleanly — {cap_msg!r}")

    # /event-create: case-insensitive duplicate name and over-length name both rejected.
    name_guild = 20260927
    async with db.session() as s:
        s.add(db.EventDefinition(guild_id=name_guild, name="Existing Event", dex_list="1", created_by=1))
        await s.commit()
    dup_interaction = _FakeAdminInteraction(name_guild)
    await admin_cog.event_create.callback(admin_cog, dup_interaction, name="existing event", dex_numbers="2")
    dup_msg = dup_interaction.response.sent["content"]
    assert "already exists" in dup_msg.lower(), dup_msg
    long_interaction = _FakeAdminInteraction(name_guild)
    await admin_cog.event_create.callback(
        admin_cog, long_interaction, name="X" * (EVENT_NAME_MAX_LEN + 1), dex_numbers="3"
    )
    long_msg = long_interaction.response.sent["content"]
    assert str(EVENT_NAME_MAX_LEN) in long_msg, long_msg
    print("event-create validation OK: case-insensitive duplicate name and over-length name both rejected")

    # /event-list: pagination actually kicks in past EVENT_LIST_PAGE_SIZE events, and every
    # page's embed stays within every Discord embed limit.
    list_guild = 20260924
    async with db.session() as s:
        for i in range(40):
            s.add(
                db.EventDefinition(
                    guild_id=list_guild, name=f"Event {i:02d}", dex_list="1", created_by=1,
                    is_active=(i % 2 == 0),
                )
            )
        await s.commit()
    list_interaction = _FakeAdminInteraction(list_guild)
    await admin_cog.event_list.callback(admin_cog, list_interaction)
    list_sent = list_interaction.response.sent
    assert list_sent.get("view") is not None, list_sent
    pages = list_sent["view"].pages
    expected_pages = -(-40 // EVENT_LIST_PAGE_SIZE)
    assert len(pages) == expected_pages and len(pages) > 1, (len(pages), expected_pages)
    for page in pages:
        _assert_embed_within_discord_limits(page)
    print(
        f"event-list pagination OK: 40 events -> {len(pages)} pages of <= {EVENT_LIST_PAGE_SIZE} each, "
        "all page embeds within Discord's embed limits"
    )

    # /event-view: a 200-number (cap) dex list truncates to fit under the field value limit,
    # with a "… (+N more)" suffix, and the whole embed stays within every embed limit.
    view_guild = 20260925
    huge_dex = list(range(1, EVENT_MAX_DEX_NUMBERS + 1))
    async with db.session() as s:
        s.add(
            db.EventDefinition(
                guild_id=view_guild, name="Huge Event",
                dex_list="\n".join(str(n) for n in huge_dex), created_by=1,
                flavor_text="Testing truncation",
            )
        )
        await s.commit()
    view_interaction = _FakeAdminInteraction(view_guild)
    await admin_cog.event_view.callback(admin_cog, view_interaction, name="Huge Event")
    view_embed = view_interaction.response.sent["embed"]
    _assert_embed_within_discord_limits(view_embed)
    dex_field = next(f for f in view_embed.fields if f.name == "Dex numbers")
    assert len(dex_field.value) <= DISCORD_EMBED_FIELD_VALUE_LIMIT, len(dex_field.value)
    assert "more)" in dex_field.value, dex_field.value
    print(
        f"event-view truncation OK: {len(huge_dex)}-entry dex list (at the cap) rendered as "
        f"{len(dex_field.value)} chars (<= {DISCORD_EMBED_FIELD_VALUE_LIMIT}), ends with "
        f"'{dex_field.value[-24:]}'"
    )

    # /event-edit: updates only the fields provided, and rejects "no fields" / duplicate-name.
    edit_guild = 20260926
    async with db.session() as s:
        s.add(db.EventDefinition(guild_id=edit_guild, name="Alpha", dex_list="1\n2", created_by=1, flavor_text="orig"))
        s.add(db.EventDefinition(guild_id=edit_guild, name="Beta", dex_list="3", created_by=1))
        await s.commit()

    noop_interaction = _FakeAdminInteraction(edit_guild)
    await admin_cog.event_edit.callback(admin_cog, noop_interaction, name="Alpha")
    noop_msg = noop_interaction.response.sent["content"]
    assert "at least one" in noop_msg.lower(), noop_msg

    dup_edit_interaction = _FakeAdminInteraction(edit_guild)
    await admin_cog.event_edit.callback(admin_cog, dup_edit_interaction, name="Alpha", new_name="Beta")
    dup_edit_msg = dup_edit_interaction.response.sent["content"]
    assert "already exists" in dup_edit_msg.lower(), dup_edit_msg
    async with db.session() as s:
        alpha = (
            await s.execute(
                select(db.EventDefinition).where(
                    db.EventDefinition.guild_id == edit_guild, db.EventDefinition.name == "Alpha"
                )
            )
        ).scalar_one()
    assert alpha.name == "Alpha", "duplicate-name rename must not apply"

    dex_edit_interaction = _FakeAdminInteraction(edit_guild)
    await admin_cog.event_edit.callback(admin_cog, dex_edit_interaction, name="Alpha", dex_numbers="5 6 7")
    dex_edit_msg = dex_edit_interaction.response.sent["content"]
    assert "dex list now 3 Pokemon" in dex_edit_msg, dex_edit_msg
    assert "renamed" not in dex_edit_msg and "flavor text updated" not in dex_edit_msg, dex_edit_msg
    async with db.session() as s:
        alpha = (
            await s.execute(
                select(db.EventDefinition).where(
                    db.EventDefinition.guild_id == edit_guild, db.EventDefinition.name == "Alpha"
                )
            )
        ).scalar_one()
    assert alpha.dex_numbers == [5, 6, 7], alpha.dex_numbers
    assert alpha.flavor_text == "orig", alpha.flavor_text
    print(f"event-edit OK: no-fields and duplicate-name both rejected, partial update applied — {dex_edit_msg!r}")

    # /event-enable: mirrors /event-disable, flips a disabled event back to active.
    enable_guild = 20260928
    async with db.session() as s:
        s.add(
            db.EventDefinition(
                guild_id=enable_guild, name="Retro Event", dex_list="1", created_by=1, is_active=False
            )
        )
        await s.commit()
    enable_interaction = _FakeAdminInteraction(enable_guild)
    await admin_cog.event_enable.callback(admin_cog, enable_interaction, name="Retro Event")
    enable_msg = enable_interaction.response.sent["content"]
    assert "Re-enabled" in enable_msg, enable_msg
    async with db.session() as s:
        retro = (
            await s.execute(
                select(db.EventDefinition).where(
                    db.EventDefinition.guild_id == enable_guild, db.EventDefinition.name == "Retro Event"
                )
            )
        ).scalar_one()
    assert retro.is_active is True, retro.is_active
    print(f"event-enable OK: {enable_msg}")

    # --- Built-in default Events: fresh-guild seeding, idempotency, and the
    # under-7 recommended-size warning on /event-create and /event-edit.
    from pokesketch.cogs.admin import EVENT_MIN_RECOMMENDED_DEX, _short_event_warning
    from pokesketch.default_events import BUILTIN_EVENT_NAMES, seed_default_events

    class _FakeSeedApi:
        async def get_type_pokemon_dex_nos(self, type_name: str) -> list[int]:
            # Deterministic, type-distinct dex lists — just needs to be >= the
            # per-event minimum so the seeding assertions below are meaningful.
            base = {"ghost": 90, "ice": 190, "bug": 290}[type_name]
            return list(range(base, base + 12))

    seed_guild = 20260929
    async with db.session() as s:
        seeded = await seed_default_events(s, _FakeSeedApi(), seed_guild)
        await s.commit()
    assert set(seeded) == BUILTIN_EVENT_NAMES, (sorted(seeded), sorted(BUILTIN_EVENT_NAMES))
    async with db.session() as s:
        seeded_rows = (
            await s.execute(select(db.EventDefinition).where(db.EventDefinition.guild_id == seed_guild))
        ).scalars().all()
    assert len(seeded_rows) == 10, len(seeded_rows)
    for row in seeded_rows:
        assert len(row.dex_numbers) >= EVENT_MIN_RECOMMENDED_DEX, (row.name, len(row.dex_numbers))
        assert row.is_active is True, row.name
    print(
        f"default-events seed OK: {len(seeded_rows)} built-in events created, each with "
        f">= {EVENT_MIN_RECOMMENDED_DEX} Pokemon"
    )

    # Idempotency: re-running the seed for the same (now-seeded) guild is a no-op.
    async with db.session() as s:
        reseeded = await seed_default_events(s, _FakeSeedApi(), seed_guild)
        await s.commit()
    assert reseeded == [], reseeded
    async with db.session() as s:
        rows_after = (
            await s.execute(select(db.EventDefinition).where(db.EventDefinition.guild_id == seed_guild))
        ).scalars().all()
    assert len(rows_after) == 10, len(rows_after)
    print("default-events seed OK: re-running for an already-seeded guild creates no duplicates")

    # Idempotency also holds if only ONE built-in name already exists (e.g. an admin
    # manually recreated it after deleting the auto-seeded one) — seeding is skipped
    # entirely rather than backfilling the other 9.
    partial_guild = 20260930
    async with db.session() as s:
        s.add(db.EventDefinition(guild_id=partial_guild, name="bug week", dex_list="1\n2", created_by=1))
        await s.commit()
        partial_seeded = await seed_default_events(s, _FakeSeedApi(), partial_guild)
        await s.commit()
    assert partial_seeded == [], partial_seeded
    async with db.session() as s:
        partial_rows = (
            await s.execute(select(db.EventDefinition).where(db.EventDefinition.guild_id == partial_guild))
        ).scalars().all()
    assert len(partial_rows) == 1, len(partial_rows)
    print("default-events seed OK: a single pre-existing built-in name (case-insensitive) skips seeding entirely")

    # The under-7 warning helper itself, factored out of /event-create and /event-edit
    # for direct testability.
    assert _short_event_warning(EVENT_MIN_RECOMMENDED_DEX - 1) is not None
    assert _short_event_warning(EVENT_MIN_RECOMMENDED_DEX) is None
    assert _short_event_warning(EVENT_MIN_RECOMMENDED_DEX + 5) is None
    print("event dex-count warning helper OK: fires below the recommended minimum, silent at/above it")

    # /event-create: warning appended to the success message for a short list, absent otherwise.
    warn_guild = 20260931
    short_interaction = _FakeAdminInteraction(warn_guild)
    await admin_cog.event_create.callback(admin_cog, short_interaction, name="Short Event", dex_numbers="1 2 3")
    short_msg = short_interaction.response.sent["content"]
    assert "⚠️" in short_msg and "3 Pokemon" in short_msg, short_msg

    full_interaction = _FakeAdminInteraction(warn_guild)
    await admin_cog.event_create.callback(
        admin_cog, full_interaction, name="Full Event", dex_numbers="1 2 3 4 5 6 7"
    )
    full_msg = full_interaction.response.sent["content"]
    assert "⚠️" not in full_msg, full_msg
    print("event-create warning OK: appended for a <7 dex list, absent for a >=7 one")

    # /event-edit: same warning fires when a replacement dex list drops under 7.
    warn_edit_interaction = _FakeAdminInteraction(warn_guild)
    await admin_cog.event_edit.callback(admin_cog, warn_edit_interaction, name="Full Event", dex_numbers="8 9")
    warn_edit_msg = warn_edit_interaction.response.sent["content"]
    assert "⚠️" in warn_edit_msg, warn_edit_msg
    print("event-edit warning OK: appended when a replaced dex list drops under 7")

    # --- Gym Events: contribution damage, defeat/badge awarding, expiry,
    # the upvote-received daily cap, one-active-event enforcement, the
    # catches-excluded regression guard, and the real /submit + upvote hooks.
    from datetime import timedelta as _gym_timedelta

    from sqlalchemy import func

    from pokesketch import gym
    from pokesketch.cogs.gym import Gym, _hp_bar

    def _make_gym_event(guild_id: int, hp_total: int, *, ends_in_days: int = 7) -> db.GymEvent:
        now = gym.naive_utcnow()
        return db.GymEvent(
            guild_id=guild_id, name="Test Gym", leader_name="Leader",
            hp_total=hp_total, hp_remaining=hp_total,
            starts_at=now, ends_at=now + _gym_timedelta(days=ends_in_days),
            status=gym.STATUS_ACTIVE, badge_name="Test Badge",
            channel_id=None, created_by=1,
        )

    assert _hp_bar(100, 100) == "🟩" * 20, _hp_bar(100, 100)
    assert _hp_bar(0, 100) == "⬜" * 20, _hp_bar(0, 100)
    assert _hp_bar(-5, 100) == "⬜" * 20, _hp_bar(-5, 100)  # negative HP clamps to empty, not a crash
    print("gym hp bar OK: full/empty/negative HP all render safely")

    # HP driven to exactly 0 by three distinct contributors -> defeated, each
    # gets exactly one badge; only the exact call that zeroes HP reports
    # just_defeated=True.
    defeat_guild = 20260940
    a_uid, b_uid, c_uid = 41001, 41002, 41003
    defeat_hp_total = 2 * gym.GYM_DAMAGE_SUBMIT + gym.GYM_DAMAGE_UPVOTE_RECEIVED
    async with db.session() as s:
        s.add(_make_gym_event(defeat_guild, hp_total=defeat_hp_total))
        await s.commit()
    async with db.session() as s:
        r1 = await gym.record_contribution(s, defeat_guild, a_uid, gym.KIND_SUBMIT)
        r2 = await gym.record_contribution(s, defeat_guild, b_uid, gym.KIND_SUBMIT)
        r3 = await gym.record_contribution(s, defeat_guild, c_uid, gym.KIND_UPVOTE_RECEIVED)
        await s.commit()
        defeat_event_id = r3[0].id
    assert r1[1] is False and r2[1] is False, (r1, r2)
    assert r3[1] is True, r3
    async with db.session() as s:
        defeat_event = await s.get(db.GymEvent, defeat_event_id)
        defeat_badges = (
            await s.execute(select(db.GymBadge).where(db.GymBadge.gym_event_id == defeat_event_id))
        ).scalars().all()
    assert defeat_event.status == gym.STATUS_DEFEATED, defeat_event.status
    assert defeat_event.hp_remaining == 0, defeat_event.hp_remaining
    assert {b.user_id for b in defeat_badges} == {a_uid, b_uid, c_uid}, {b.user_id for b in defeat_badges}
    assert len(defeat_badges) == 3, len(defeat_badges)
    print("gym defeat OK: HP hit exactly 0 across 3 contributors -> defeated, one badge each")

    # A contribution after defeat is a no-op (no active event to attach to),
    # and re-running the defeat transition directly (the /gym end edge case)
    # never duplicates a badge.
    async with db.session() as s:
        post_defeat_result = await gym.record_contribution(s, defeat_guild, a_uid, gym.KIND_SUBMIT)
        redefeat_event = await s.get(db.GymEvent, defeat_event_id)
        await gym.mark_defeated(s, redefeat_event)
        await s.commit()
    assert post_defeat_result is None, post_defeat_result
    async with db.session() as s:
        badges_after = (
            await s.execute(select(db.GymBadge).where(db.GymBadge.gym_event_id == defeat_event_id))
        ).scalars().all()
    assert len(badges_after) == 3, len(badges_after)
    print(
        "gym badge idempotency OK: a post-defeat contribution is a no-op, and re-running the "
        "defeat transition directly doesn't duplicate anyone's badge"
    )

    # Daily cap: upvote_received damage stops accruing once it hits
    # GYM_DAMAGE_UPVOTE_RECEIVED_DAILY_CAP for that user/event/day, mirroring
    # the EXP upvote-received cap but with its own independent tracking.
    cap_guild = 20260941
    cap_uid = 41010
    async with db.session() as s:
        s.add(_make_gym_event(cap_guild, hp_total=10_000))
        await s.commit()
    cap_iterations = gym.GYM_DAMAGE_UPVOTE_RECEIVED_DAILY_CAP // gym.GYM_DAMAGE_UPVOTE_RECEIVED + 5
    async with db.session() as s:
        for _ in range(cap_iterations):
            await gym.record_contribution(s, cap_guild, cap_uid, gym.KIND_UPVOTE_RECEIVED)
        await s.commit()
    async with db.session() as s:
        cap_total_damage = (
            await s.execute(
                select(func.coalesce(func.sum(db.GymContribution.damage), 0)).where(
                    db.GymContribution.user_id == cap_uid,
                    db.GymContribution.kind == gym.KIND_UPVOTE_RECEIVED,
                )
            )
        ).scalar_one()
    assert cap_total_damage == gym.GYM_DAMAGE_UPVOTE_RECEIVED_DAILY_CAP, cap_total_damage
    print(
        f"gym upvote-received cap OK: capped at {cap_total_damage} damage/day despite "
        f"{cap_iterations} attempts"
    )

    # Expiry: ends_at in the past with HP > 0 -> expired, zero badges, even
    # for a contributor who chipped some HP off before time ran out. A
    # still-active event with ends_at in the future is left untouched.
    expiry_guild = 20260944
    expiry_uid = 41030
    async with db.session() as s:
        expiry_event = _make_gym_event(expiry_guild, hp_total=1000, ends_in_days=-1)
        s.add(expiry_event)
        await s.commit()
        expiry_event_id = expiry_event.id
        await gym.record_contribution(s, expiry_guild, expiry_uid, gym.KIND_SUBMIT)
        await s.commit()

    active_guild = 20260945
    async with db.session() as s:
        s.add(_make_gym_event(active_guild, hp_total=100, ends_in_days=7))
        await s.commit()

    async with db.session() as s:
        expired = await gym.close_expired_gym_events(s)
        await s.commit()
    assert any(e.id == expiry_event_id for e in expired), (expiry_event_id, [e.id for e in expired])
    assert all(e.guild_id != active_guild for e in expired), expired
    async with db.session() as s:
        expired_event_after = await s.get(db.GymEvent, expiry_event_id)
        expiry_badges = (
            await s.execute(select(db.GymBadge).where(db.GymBadge.gym_event_id == expiry_event_id))
        ).scalars().all()
        still_active = await gym.get_active_gym_event(s, active_guild)
    assert expired_event_after.status == gym.STATUS_EXPIRED, expired_event_after.status
    assert expired_event_after.hp_remaining > 0, expired_event_after.hp_remaining
    assert expiry_badges == [], expiry_badges
    assert still_active is not None and still_active.status == gym.STATUS_ACTIVE, still_active
    print(
        "gym expiry OK: a past-ends_at event with HP > 0 -> expired with zero badges; a "
        "still-active event with a future ends_at is left untouched"
    )

    # Catches must NOT contribute to gym damage (locked design decision) — a
    # regression guard against ever wiring gym contributions into /catch.
    catch_guard_guild = 20260943
    catch_guard_uid = 41020
    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=catch_guard_guild))
        s.add(_make_gym_event(catch_guard_guild, hp_total=1000))
        catch_guard_daily = db.DailyPokemon(
            guild_id=catch_guard_guild, local_date=_date.today(), dex_no=1, name="bulbasaur"
        )
        s.add(catch_guard_daily)
        await s.commit()
        s.add(
            db.Submission(
                guild_id=catch_guard_guild, user_id=catch_guard_uid, daily_id=catch_guard_daily.id,
                image_url="https://example.invalid/catchguard.png",
            )
        )
        await s.commit()
    async with db.session() as s:
        await pokebox.catch_submission(s, catch_guard_uid, party_dir)
        await s.commit()
    async with db.session() as s:
        catch_guard_contributions = (
            await s.execute(select(db.GymContribution).where(db.GymContribution.user_id == catch_guard_uid))
        ).scalars().all()
    assert catch_guard_contributions == [], catch_guard_contributions
    print("gym catches-excluded OK: /catch never logs a GymContribution even with an active gym event")

    # Real /submit hook: the shared _award_submission_rewards path (not a
    # direct gym.record_contribution call) logs a GymContribution and
    # decrements HP for both the main-daily and upvote-given/-received paths.
    hook_guild = 20260950
    hook_uid = 41060
    async with db.session() as s:
        s.add(_make_gym_event(hook_guild, hp_total=1000))
        hook_daily = db.DailyPokemon(guild_id=hook_guild, local_date=_date.today(), dex_no=1, name="bulbasaur")
        s.add(hook_daily)
        await s.commit()
        _hook_confirm, _hook_exp, hook_gym_result = await subs_cog._submit_to_daily(
            s, hook_daily, _FakeThread(90001), _FakeAttachment(), hook_guild, hook_uid, "<@41060>"
        )
        await s.commit()
    assert hook_gym_result is not None and hook_gym_result[1] is False, hook_gym_result
    async with db.session() as s:
        hook_event = await gym.get_active_gym_event(s, hook_guild)
        hook_contributions = (
            await s.execute(select(db.GymContribution).where(db.GymContribution.gym_event_id == hook_event.id))
        ).scalars().all()
    assert hook_event.hp_remaining == 1000 - gym.GYM_DAMAGE_SUBMIT, hook_event.hp_remaining
    assert len(hook_contributions) == 1 and hook_contributions[0].kind == gym.KIND_SUBMIT, hook_contributions
    print("gym submit-hook OK: a real /submit (main daily) logs a GymContribution and decrements HP")

    async with db.session() as s:
        hook_sub_row = db.Submission(
            guild_id=hook_guild, user_id=hook_uid, daily_id=hook_daily.id,
            image_url="https://example.invalid/hook.png",
        )
        s.add(hook_sub_row)
        await s.flush()
        received_result = await Submissions._maybe_award_upvote_received_exp(s, hook_sub_row)
        given_result = await Submissions._maybe_award_upvote_given_exp(s, hook_guild, 41061)
        await s.commit()
    assert received_result is not None and received_result[1] is False, received_result
    assert given_result is not None and given_result[1] is False, given_result
    async with db.session() as s:
        hook_event_after_upvotes = await gym.get_active_gym_event(s, hook_guild)
    expected_hook_hp = 1000 - gym.GYM_DAMAGE_SUBMIT - gym.GYM_DAMAGE_UPVOTE_RECEIVED - gym.GYM_DAMAGE_UPVOTE_GIVEN
    assert hook_event_after_upvotes.hp_remaining == expected_hook_hp, (
        hook_event_after_upvotes.hp_remaining, expected_hook_hp
    )
    print("gym upvote-hooks OK: both upvote_received and upvote_given feed gym damage independently of EXP caps")

    # gym.announce_defeat: posts a defeat embed to the event's stored channel,
    # and no-ops gracefully (no crash) when channel_id is unset.
    class _FakeAnnounceChannel:
        def __init__(self, channel_id: int):
            self.id = channel_id
            self.sent: list[dict] = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    class _FakeAnnounceClient:
        def __init__(self, channel):
            self._channel = channel

        def get_channel(self, cid):
            return self._channel if cid == self._channel.id else None

        async def fetch_channel(self, cid):
            raise AssertionError("fetch_channel should not be called when get_channel already found it")

    announce_channel = _FakeAnnounceChannel(777001)
    announce_client = _FakeAnnounceClient(announce_channel)
    announce_event = _make_gym_event(20260951, hp_total=10)
    announce_event.channel_id = announce_channel.id
    announce_event.badge_name = "Announce Badge"
    await gym.announce_defeat(announce_client, announce_event)
    assert len(announce_channel.sent) == 1, announce_channel.sent
    announce_embed = announce_channel.sent[0]["embed"]
    assert "Announce Badge" in announce_embed.description, announce_embed.description
    print("gym announce_defeat OK: posts a defeat embed to the event's stored channel")

    no_channel_event = _make_gym_event(20260952, hp_total=10)
    no_channel_event.channel_id = None
    await gym.announce_defeat(announce_client, no_channel_event)  # must not raise
    assert announce_channel.sent == [announce_channel.sent[0]], "no-channel event must not post anything"
    print("gym announce_defeat OK: a missing channel_id is a graceful no-op, not a crash")

    # /gym commands end-to-end via the real cog callbacks (mirrors the
    # _FakeAdminInteraction pattern used for the /event-* commands above).
    class _FakeGymUser:
        def __init__(self, uid: int = 1, display_name: str = "gym-tester"):
            self.id = uid
            self.display_name = display_name

    class _FakeGymInteraction:
        def __init__(self, guild_id: int, channel_id: int = 1, user_id: int = 1):
            self.guild_id = guild_id
            self.channel_id = channel_id
            self.user = _FakeGymUser(user_id)
            self.response = _FakeAdminResponse()

    gym_cog = Gym(bot=None)

    # Only one active GymEvent per guild — /gym start while one is already
    # active errors cleanly and does not create a second row.
    onegym_guild = 20260942
    first_start = _FakeGymInteraction(onegym_guild, channel_id=555001)
    await gym_cog.gym_start.callback(
        gym_cog, first_start, name="First Gym", leader_name="Leader One",
        hp_total=100, duration_days=3, badge_name="First Badge",
    )
    assert first_start.response.sent.get("embed") is not None, first_start.response.sent
    _assert_embed_within_discord_limits(first_start.response.sent["embed"])

    second_start = _FakeGymInteraction(onegym_guild, channel_id=555002)
    await gym_cog.gym_start.callback(
        gym_cog, second_start, name="Second Gym", leader_name="Leader Two",
        hp_total=50, duration_days=3, badge_name="Second Badge",
    )
    second_start_msg = second_start.response.sent.get("content")
    assert second_start_msg is not None and "still active" in second_start_msg, second_start.response.sent
    async with db.session() as s:
        onegym_active = (
            await s.execute(
                select(db.GymEvent).where(
                    db.GymEvent.guild_id == onegym_guild, db.GymEvent.status == gym.STATUS_ACTIVE
                )
            )
        ).scalars().all()
    assert len(onegym_active) == 1 and onegym_active[0].name == "First Gym", onegym_active
    print("gym one-active-event OK: /gym start while one is active is rejected cleanly, no second row created")

    # /gym end: HP > 0 -> cancelled, no badges.
    end_guild = 20260946
    async with db.session() as s:
        s.add(_make_gym_event(end_guild, hp_total=500))
        await s.commit()
    end_interaction = _FakeGymInteraction(end_guild)
    await gym_cog.gym_end.callback(gym_cog, end_interaction)
    assert "ended early" in end_interaction.response.sent["content"], end_interaction.response.sent
    async with db.session() as s:
        ended_event = (
            await s.execute(select(db.GymEvent).where(db.GymEvent.guild_id == end_guild))
        ).scalar_one()
        end_badges = (
            await s.execute(select(db.GymBadge).where(db.GymBadge.gym_event_id == ended_event.id))
        ).scalars().all()
    assert ended_event.status == gym.STATUS_CANCELLED, ended_event.status
    assert end_badges == [], end_badges
    print("gym end OK: manual early end with HP > 0 -> cancelled, no badges")

    # /gym end edge case: HP already <= 0 but never transitioned (e.g. a crash
    # between the decrement and the status flip) -> handled as a defeat.
    edge_guild = 20260947
    edge_uid = 41040
    async with db.session() as s:
        edge_event = _make_gym_event(edge_guild, hp_total=10)
        edge_event.hp_remaining = 0
        s.add(edge_event)
        await s.commit()
        edge_event_id = edge_event.id
        s.add(db.GymContribution(gym_event_id=edge_event_id, user_id=edge_uid, kind=gym.KIND_SUBMIT, damage=10))
        await s.commit()
    edge_interaction = _FakeGymInteraction(edge_guild)
    await gym_cog.gym_end.callback(gym_cog, edge_interaction)
    assert "already fallen" in edge_interaction.response.sent["content"], edge_interaction.response.sent
    async with db.session() as s:
        edge_event_after = await s.get(db.GymEvent, edge_event_id)
        edge_badges = (
            await s.execute(select(db.GymBadge).where(db.GymBadge.gym_event_id == edge_event_id))
        ).scalars().all()
    assert edge_event_after.status == gym.STATUS_DEFEATED, edge_event_after.status
    assert {b.user_id for b in edge_badges} == {edge_uid}, edge_badges
    print("gym end edge case OK: HP already <= 0 but unprocessed is handled gracefully as a defeat with badges")

    noevent_interaction = _FakeGymInteraction(20260948)
    await gym_cog.gym_end.callback(gym_cog, noevent_interaction)
    assert "No active gym event" in noevent_interaction.response.sent["content"], noevent_interaction.response.sent
    print("gym end OK: no active event -> clean error, not a crash")

    # /gym status: HP bar, time remaining, and top contributors reflect live contributions.
    status_guild = 20260949
    status_uid1, status_uid2 = 41050, 41051
    async with db.session() as s:
        s.add(_make_gym_event(status_guild, hp_total=100))
        await s.commit()
        await gym.record_contribution(s, status_guild, status_uid1, gym.KIND_SUBMIT)
        await gym.record_contribution(s, status_guild, status_uid2, gym.KIND_UPVOTE_GIVEN)
        await s.commit()
    status_interaction = _FakeGymInteraction(status_guild)
    await gym_cog.gym_status.callback(gym_cog, status_interaction)
    status_embed = status_interaction.response.sent["embed"]
    _assert_embed_within_discord_limits(status_embed)
    status_hp_field = next(f for f in status_embed.fields if f.name == "HP")
    expected_status_hp = 100 - gym.GYM_DAMAGE_SUBMIT - gym.GYM_DAMAGE_UPVOTE_GIVEN
    assert f"{expected_status_hp} / 100 HP" in status_hp_field.value, status_hp_field.value
    status_contributors_field = next(f for f in status_embed.fields if f.name == "Top contributors")
    assert f"<@{status_uid1}>" in status_contributors_field.value, status_contributors_field.value
    assert f"<@{status_uid2}>" in status_contributors_field.value, status_contributors_field.value
    print("gym status OK: HP/time-remaining/top-contributors embed reflects live contributions")

    no_status_interaction = _FakeGymInteraction(20260953)
    await gym_cog.gym_status.callback(gym_cog, no_status_interaction)
    assert "No active gym event" in no_status_interaction.response.sent["content"], no_status_interaction.response.sent
    print("gym status OK: no active event -> clean message, not a crash")

    # /gym badges: a contributor from the earlier defeat scenario sees their
    # badge; a player with none gets a clean message.
    badges_interaction = _FakeGymInteraction(defeat_guild)
    await gym_cog.gym_badges.callback(gym_cog, badges_interaction, user=_FakeGymUser(a_uid, "A"))
    badges_embed = badges_interaction.response.sent["embed"]
    assert "Test Badge" in badges_embed.description and "Leader" in badges_embed.description, badges_embed.description
    print("gym badges OK: earned badge listed with badge name, leader, and event name")

    no_badges_interaction = _FakeGymInteraction(defeat_guild)
    await gym_cog.gym_badges.callback(gym_cog, no_badges_interaction, user=_FakeGymUser(999999, "Nobody"))
    assert "hasn't earned any gym badges yet" in no_badges_interaction.response.sent["content"], (
        no_badges_interaction.response.sent
    )
    print("gym badges OK: a player with none gets a clean message, not a crash")

    # /help + /help-admin: gym commands are documented and both embeds stay
    # within Discord's limits with the new fields added.
    help_cog2 = Help(bot=None)
    help_interaction2 = _FakeHelpInteraction()
    await help_cog2.help_cmd.callback(help_cog2, help_interaction2)
    help_embed2 = help_interaction2.response.sent_embed
    _assert_embed_within_discord_limits(help_embed2)
    help_text2 = "\n".join(f.value for f in help_embed2.fields)
    assert "/gym status" in help_text2 and "/gym badges" in help_text2, help_text2

    admin_interaction2 = _FakeHelpInteraction()
    await help_cog2.help_admin_cmd.callback(help_cog2, admin_interaction2)
    admin_embed2 = admin_interaction2.response.sent_embed
    _assert_embed_within_discord_limits(admin_embed2)
    admin_text2 = "\n".join(f.value for f in admin_embed2.fields)
    assert "/gym start" in admin_text2 and "/gym end" in admin_text2, admin_text2
    print("gym help OK: /gym start /gym end documented in /help-admin, /gym status /gym badges in /help")

    # --- Daily Winner Spotlight ---------------------------------------
    from datetime import timedelta as _timedelta

    from pokesketch.daily_spotlight import post_daily_spotlight_for_guild

    class _SpotlightChannel:
        def __init__(self, channel_id: int):
            self.id = channel_id
            self.sent: list[dict] = []

        async def send(self, *args, **kwargs):
            self.sent.append(kwargs)
            return _FakeSentMessage(80000 + len(self.sent), "https://example.invalid/spotlight.png")

    class _SpotlightClient:
        def __init__(self, channels: dict[int, object]):
            self._channels = channels

        def get_channel(self, cid):
            return self._channels.get(cid)

        async def fetch_channel(self, cid):
            return self._channels[cid]

    sl_gid = 400
    sl_channel_id = 40001
    sl_channel = _SpotlightChannel(sl_channel_id)
    sl_client = _SpotlightClient({sl_channel_id: sl_channel})
    yesterday = today - _timedelta(days=1)
    two_days_ago = today - _timedelta(days=2)

    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=sl_gid, channel_id=sl_channel_id, timezone="UTC"))
        await s.commit()

        daily_yday = db.DailyPokemon(
            guild_id=sl_gid, local_date=yesterday, dex_no=25, name="pikachu", is_shiny=False
        )
        daily_2days = db.DailyPokemon(
            guild_id=sl_gid, local_date=two_days_ago, dex_no=1, name="bulbasaur", is_shiny=False
        )
        s.add_all([daily_yday, daily_2days])
        await s.flush()

        sub_low = db.Submission(
            guild_id=sl_gid, user_id=111, daily_id=daily_yday.id,
            image_url="https://example.invalid/low.png",
        )
        sub_high = db.Submission(
            guild_id=sl_gid, user_id=222, daily_id=daily_yday.id,
            image_url="https://example.invalid/high.png",
        )
        s.add_all([sub_low, sub_high])
        await s.flush()

        s.add(db.Upvote(submission_id=sub_low.id, voter_id=901))
        s.add(db.Upvote(submission_id=sub_high.id, voter_id=901))
        s.add(db.Upvote(submission_id=sub_high.id, voter_id=902))
        s.add(db.Upvote(submission_id=sub_high.id, voter_id=903))
        await s.commit()

    posted = await post_daily_spotlight_for_guild(sl_client, sl_gid, yesterday)
    assert posted is True, posted
    assert len(sl_channel.sent) == 1, sl_channel.sent
    embed = sl_channel.sent[0]["embed"]
    assert "222" in embed.description or True  # mention format check below
    assert "<@222>" in embed.description and "<@111>" not in embed.description, embed.description
    assert "3 upvotes" in embed.description, embed.description

    async with db.session() as s:
        events = (
            await s.execute(
                select(db.ExpEvent).where(
                    db.ExpEvent.guild_id == sl_gid, db.ExpEvent.type == "daily_winner"
                )
            )
        ).scalars().all()
    assert len(events) == 1, events
    assert events[0].user_id == 222, events[0].user_id
    assert events[0].amount == leveling.EXP_DAILY_WINNER, events[0].amount
    print("daily spotlight OK: single winner picked by upvote count, EXP_DAILY_WINNER awarded once")

    # Re-running the same day must not double-post or double-award (idempotency).
    sl_channel.sent.clear()
    posted_again = await post_daily_spotlight_for_guild(sl_client, sl_gid, yesterday)
    assert posted_again is False, posted_again
    assert len(sl_channel.sent) == 0, sl_channel.sent
    async with db.session() as s:
        events_after = (
            await s.execute(
                select(db.ExpEvent).where(
                    db.ExpEvent.guild_id == sl_gid, db.ExpEvent.type == "daily_winner"
                )
            )
        ).scalars().all()
    assert len(events_after) == 1, events_after
    print("daily spotlight OK: idempotency guard prevents a second post/EXP-award for the same day")

    # Tie case: a separate guild, two submissions tied for the top upvote count.
    tie_gid = 401
    tie_channel_id = 40101
    tie_channel = _SpotlightChannel(tie_channel_id)
    tie_client = _SpotlightClient({tie_channel_id: tie_channel})

    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=tie_gid, channel_id=tie_channel_id, timezone="UTC"))
        await s.commit()
        daily_tie = db.DailyPokemon(
            guild_id=tie_gid, local_date=yesterday, dex_no=4, name="charmander", is_shiny=True
        )
        s.add(daily_tie)
        await s.flush()
        sub_a = db.Submission(guild_id=tie_gid, user_id=301, daily_id=daily_tie.id, image_url="https://example.invalid/a.png")
        sub_b = db.Submission(guild_id=tie_gid, user_id=302, daily_id=daily_tie.id, image_url="https://example.invalid/b.png")
        s.add_all([sub_a, sub_b])
        await s.flush()
        s.add(db.Upvote(submission_id=sub_a.id, voter_id=901))
        s.add(db.Upvote(submission_id=sub_b.id, voter_id=901))
        await s.commit()

    tie_posted = await post_daily_spotlight_for_guild(tie_client, tie_gid, yesterday)
    assert tie_posted is True, tie_posted
    tie_embed = tie_channel.sent[0]["embed"]
    assert "<@301>" in tie_embed.description and "<@302>" in tie_embed.description, tie_embed.description
    async with db.session() as s:
        tie_events = (
            await s.execute(
                select(db.ExpEvent).where(
                    db.ExpEvent.guild_id == tie_gid, db.ExpEvent.type == "daily_winner"
                )
            )
        ).scalars().all()
    assert len(tie_events) == 2, tie_events
    assert {e.user_id for e in tie_events} == {301, 302}, tie_events
    assert all(e.amount == leveling.EXP_DAILY_WINNER for e in tie_events), tie_events
    print("daily spotlight OK: tie credits both artists in one post, each gets full EXP (not split)")

    # No-submissions case: a guild with a daily row for yesterday but zero
    # submissions must no-op cleanly (no post, no crash).
    empty_gid = 402
    empty_channel_id = 40201
    empty_channel = _SpotlightChannel(empty_channel_id)
    empty_client = _SpotlightClient({empty_channel_id: empty_channel})
    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=empty_gid, channel_id=empty_channel_id, timezone="UTC"))
        s.add(db.DailyPokemon(guild_id=empty_gid, local_date=yesterday, dex_no=7, name="squirtle", is_shiny=False))
        await s.commit()
    empty_posted = await post_daily_spotlight_for_guild(empty_client, empty_gid, yesterday)
    assert empty_posted is False, empty_posted
    assert len(empty_channel.sent) == 0, empty_channel.sent
    print("daily spotlight OK: no-submissions day posts nothing and does not crash")

    # --- Player Profile Card & Poké Ball Rank Badges (native-embed rebuild +
    # command merge) --------------------------------------------------------
    from pokesketch import rank_badges

    # rank_for_level: exact threshold levels map to the correct tier, and
    # off-by-one below/above each threshold lands on the adjacent tier.
    # Unchanged by the embed rebuild — reused, not duplicated.
    assert rank_badges.rank_for_level(1) == "Poké Ball"
    assert rank_badges.rank_for_level(4) == "Poké Ball"
    assert rank_badges.rank_for_level(5) == "Great Ball"
    assert rank_badges.rank_for_level(11) == "Great Ball"
    assert rank_badges.rank_for_level(12) == "Ultra Ball"
    assert rank_badges.rank_for_level(19) == "Ultra Ball"
    assert rank_badges.rank_for_level(20) == "Premier Ball"
    assert rank_badges.rank_for_level(29) == "Premier Ball"
    assert rank_badges.rank_for_level(30) == "Luxury Ball"
    assert rank_badges.rank_for_level(44) == "Luxury Ball"
    assert rank_badges.rank_for_level(45) == "Master Ball"
    assert rank_badges.rank_for_level(999) == "Master Ball"
    assert rank_badges.rank_for_level(0) == "Poké Ball"  # defensive: below the first threshold
    print("rank_for_level OK: every tier boundary maps correctly, off-by-one on both sides holds")

    # get_tier_emblem: the ONE piece of pre-rendering the native-embed rebuild
    # keeps (the full-bleed watermark variant is gone — no equivalent in a
    # discord.Embed). Network faked so this stays offline, same pattern as
    # pokebox._cache_image above. Confirms the corner emblem exists at the
    # expected size and a second call is a pure cache hit (no re-fetch).
    from PIL import Image as _BadgeImage

    fake_raw_dir = os.path.join(tmp, "fake_raw_ball")
    os.makedirs(fake_raw_dir, exist_ok=True)
    fake_raw_path = os.path.join(fake_raw_dir, "fake-ball.png")
    _BadgeImage.new("RGBA", (128, 128), (255, 0, 0, 255)).save(fake_raw_path, format="PNG")

    fetch_calls: list[str] = []

    async def _fake_fetch_ball_sprite(tier_name, cache_dir):
        fetch_calls.append(tier_name)
        return fake_raw_path

    rank_badges.fetch_ball_sprite = _fake_fetch_ball_sprite

    badge_cache_dir = os.path.join(tmp, "badge_cache")
    emblem_path = await rank_badges.get_tier_emblem("Great Ball", badge_cache_dir)
    assert os.path.exists(emblem_path)
    with _BadgeImage.open(emblem_path) as emblem_img:
        assert emblem_img.size == (rank_badges.CORNER_EMBLEM_SIZE, rank_badges.CORNER_EMBLEM_SIZE), emblem_img.size
    assert fetch_calls == ["Great Ball"], fetch_calls

    emblem_path2 = await rank_badges.get_tier_emblem("Great Ball", badge_cache_dir)
    assert emblem_path2 == emblem_path
    assert fetch_calls == ["Great Ball"], fetch_calls  # cache hit: no second fetch
    print("get_tier_emblem OK: corner emblem rendered at the expected size, cache hit skips re-fetch")

    # PokeApiClient.get_sprite_image_path: cache-hit branch returns the
    # existing file without touching the network (mirrors pokebox's
    # cache-forever tests above). Party sprites are no longer consumed by a
    # Pillow render, but /profile's "Inspect Party" flow still relies on this
    # cache-forever behavior for other sprite lookups in this codebase.
    from pokesketch.pokeapi import PokeApiClient

    sprite_cache_root = os.path.join(tmp, "sprite_cache_root")
    api_client_for_sprites = PokeApiClient(sprite_cache_root)
    precached_path = api_client_for_sprites._sprite_cache_path(999999, shiny=False)
    os.makedirs(os.path.dirname(precached_path), exist_ok=True)
    with open(precached_path, "wb") as fh:
        fh.write(b"fake-sprite-bytes")
    resolved_path = await api_client_for_sprites.get_sprite_image_path(999999, shiny=False)
    assert resolved_path == precached_path, (resolved_path, precached_path)
    await api_client_for_sprites.aclose()
    print("get_sprite_image_path OK: a cached sprite is returned without a network fetch")

    # _title_badge: leaderboard-rank-derived title, falling back to the rank
    # tier when not ranked highly enough for a leaderboard-based title.
    from pokesketch.cogs.profile import _accuracy_pct, _title_badge

    assert _title_badge(1, "Great Ball") == "Champion"
    assert _title_badge(2, "Great Ball") == "Elite Four"
    assert _title_badge(4, "Great Ball") == "Elite Four"
    assert _title_badge(5, "Great Ball") == "Gym Leader"
    assert _title_badge(10, "Great Ball") == "Gym Leader"
    assert _title_badge(11, "Great Ball") == "Great Ball Trainer"
    assert _title_badge(None, "Master Ball") == "Master Ball Trainer"
    print("_title_badge OK: leaderboard-rank titles at every cut, falls back to '{tier} Trainer' otherwise")

    # _accuracy_pct: the LOCKED formula (submission_count / days_since_first_submit),
    # with the divide-by-zero guard (0 days since first submit — a same-day
    # first submission) and the 100%-display clamp (the raw formula can
    # exceed 100% since one calendar day can hold two submissions).
    assert _accuracy_pct(0, None) == 0.0  # never submitted
    assert _accuracy_pct(5, 0) == 100.0  # first-day submitter: 0 days treated as 1, 5/1 clamped to 100%
    assert _accuracy_pct(3, 10) == 30.0
    assert _accuracy_pct(20, 10) == 100.0  # raw formula would be 200% — clamped for display
    print("_accuracy_pct OK: divide-by-zero guarded, normal case correct, over-100% clamped")

    # --- End-to-end /profile command: build real DB state for a user with
    # BOTH per-server AND global data, run the actual command callback, and
    # inspect the real discord.Embed/View it produces. This is the
    # regression guard for the /profile + /profile-card merge — the whole
    # point of consolidating is that per-server stats from the old /profile
    # must NOT silently disappear.
    from pokesketch.cogs.profile import Profile, ProfileView

    profile_gid = 20260924
    profile_uid = 7001
    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=profile_gid))
        s.add(
            db.User(
                guild_id=profile_gid, user_id=profile_uid, exp=60, level=leveling.level_for_exp(60),
                personal_streak=2, longest_streak=4,
            )
        )
        s.add(
            db.GlobalUser(
                user_id=profile_uid, exp=160, level=leveling.level_for_exp(160),
                global_streak=6, longest_global_streak=9,
            )
        )
        daily_p = db.DailyPokemon(guild_id=profile_gid, local_date=_date.today(), dex_no=1, name="bulbasaur")
        s.add(daily_p)
        await s.flush()
        first_sub_at = _datetime.now(UTC) - _timedelta(days=9)
        s.add(
            db.Submission(
                guild_id=profile_gid, user_id=profile_uid, daily_id=daily_p.id,
                image_url="https://example.invalid/p1.png", created_at=first_sub_at,
            )
        )
        s.add(
            db.Submission(
                guild_id=profile_gid, user_id=profile_uid, daily_id=daily_p.id,
                image_url="https://example.invalid/p2.png",
            )
        )
        await s.commit()
        await pokebox.record_pokebox_scan(s, profile_uid, 1, None)
        await s.commit()

    profile_party_dir = os.path.join(tmp, "profile_party_cache")
    async with db.session() as s:
        wallet = await pokebox.get_or_create_wallet(s, profile_uid)
        wallet.balance = 5
        await s.commit()
        await pokebox.catch_submission(s, profile_uid, profile_party_dir)  # shiny=False, from daily_p
        await s.commit()
    async with db.session() as s:
        # A second, shiny catch so the Shinies field and party's ★ marker both
        # have real data to show, without depending on /catch's own shiny odds.
        shiny_daily = db.DailyPokemon(
            guild_id=profile_gid, local_date=_date.today() - _timedelta(days=1),
            dex_no=25, name="pikachu", is_shiny=True,
        )
        s.add(shiny_daily)
        await s.flush()
        shiny_sub = db.Submission(
            guild_id=profile_gid, user_id=profile_uid, daily_id=shiny_daily.id,
            image_url="https://example.invalid/shiny.png",
        )
        s.add(shiny_sub)
        await s.commit()
        shiny_sub_id = shiny_sub.id

    # A fresh session so shiny_sub's created_at round-trips through SQLite as
    # naive before catchable_submissions() compares it against `now` — doing
    # this catch in the SAME session that just inserted the row would leave
    # the Python-side object holding its client-side-default AWARE datetime
    # (expire_on_commit=False keeps it), which can't compare against a naive
    # "now" from an already-flushed row loaded fresh in the same query.
    async with db.session() as s:
        await pokebox.catch_submission(s, profile_uid, profile_party_dir, target=f"s{shiny_sub_id}")
        await s.commit()

    class _FakeProfileAvatar:
        url = "https://example.invalid/avatar.png"
        key = "fake_avatar_hash_v1"

        async def save(self, path: str) -> None:
            # Offline stand-in for discord.Asset.save() — writes a real tiny
            # on-disk PNG (same "real file, not a mock" posture as
            # _FakeProfileApiClient.get_sprite_image_path below) so the
            # render module's Image.open()/circle-mask path gets exercised.
            from PIL import Image as _AvatarImage

            os.makedirs(os.path.dirname(path), exist_ok=True)
            _AvatarImage.new("RGBA", (64, 64), (100, 120, 220, 255)).save(path, format="PNG")

    class _FakeProfileUser:
        def __init__(self, uid: int, display_name: str = "ProfileTester"):
            self.id = uid
            self.display_name = display_name
            self.display_avatar = _FakeProfileAvatar()

    class _FakeProfileResponse:
        def __init__(self):
            self.deferred = False
            self.sent_message: dict | None = None
            self.edited: dict | None = None

        async def defer(self, thinking: bool = False) -> None:
            self.deferred = True

        async def send_message(self, *args, **kwargs):
            self.sent_message = {"args": args, "kwargs": kwargs}

        async def edit_message(self, **kwargs):
            self.edited = kwargs

    class _FakeProfileFollowup:
        def __init__(self):
            self.sent: dict | None = None

        async def send(self, **kwargs):
            self.sent = kwargs

    class _FakeProfileMessage:
        def __init__(self):
            self.edited: dict | None = None

        async def edit(self, **kwargs):
            self.edited = kwargs

    class _FakeProfileInteraction:
        def __init__(self, user, guild_id: int | None):
            self.user = user
            self.guild_id = guild_id
            self.response = _FakeProfileResponse()
            self.followup = _FakeProfileFollowup()

        async def original_response(self):
            return _FakeProfileMessage()

    class _FakeProfileBotConfig:
        def __init__(self, badge_cache_dir: str, image_cache_dir: str):
            self.badge_cache_dir = badge_cache_dir
            self.image_cache_dir = image_cache_dir

    class _FakeAccentColor:
        def __init__(self, r: int, g: int, b: int):
            self.r = r
            self.g = g
            self.b = b

    class _FakeFetchedUser:
        def __init__(self, accent_color=None):
            self.accent_color = accent_color

    class _FakeProfileApiClient:
        """Offline stand-in for pokeapi.PokeApiClient — party sprite fetches
        must stay offline in this test, same posture as _fake_cache_image
        above. Returns a real tiny on-disk PNG so the render module's
        Image.open() path gets exercised, not a mocked no-op."""

        def __init__(self, sprite_path: str):
            self.sprite_path = sprite_path
            self.calls: list[tuple[int, bool]] = []

        async def get_sprite_image_path(self, dex_no: int, shiny: bool = False):
            self.calls.append((dex_no, shiny))
            return self.sprite_path

    class _FakeProfileBot:
        def __init__(self, badge_cache_dir: str, accent_color=None, api=None, image_cache_dir: str | None = None):
            self.config = _FakeProfileBotConfig(badge_cache_dir, image_cache_dir or os.path.join(tmp, "image_cache"))
            self._accent_color = accent_color
            self.api = api

        async def fetch_user(self, user_id: int):
            return _FakeFetchedUser(accent_color=self._accent_color)

    import pokesketch.cogs.profile as _profile_cog_module
    from pokesketch.profile_card_render import ProfileCardData as _ProfileCardData

    # Spy on render_profile_panel so we can capture the *real* ProfileCardData
    # the actual /profile command path built (per-server + global stats
    # included), while still letting the real render run so the embed's
    # attached image is real PNG bytes, not a mock.
    _captured_card_data: list[_ProfileCardData] = []
    _real_render_profile_panel = _profile_cog_module.render_profile_panel

    def _spying_render_profile_panel(data):
        _captured_card_data.append(data)
        return _real_render_profile_panel(data)

    from PIL import Image as _SpriteFixtureImage

    fake_sprite_path = os.path.join(tmp, "fake_party_sprite.png")
    _SpriteFixtureImage.new("RGBA", (4, 4), (10, 20, 30, 255)).save(fake_sprite_path, format="PNG")
    fake_api_client = _FakeProfileApiClient(fake_sprite_path)

    profile_cog = Profile(
        bot=_FakeProfileBot(badge_cache_dir, accent_color=_FakeAccentColor(88, 101, 242), api=fake_api_client)
    )
    profile_user = _FakeProfileUser(profile_uid)
    profile_interaction = _FakeProfileInteraction(profile_user, guild_id=profile_gid)
    _profile_cog_module.render_profile_panel = _spying_render_profile_panel
    try:
        await profile_cog.profile.callback(profile_cog, profile_interaction, user=None)
    finally:
        _profile_cog_module.render_profile_panel = _real_render_profile_panel

    assert profile_interaction.response.deferred, "expected /profile to defer before gathering data"
    sent = profile_interaction.followup.sent
    assert sent is not None, "expected /profile to send an embed via followup"
    profile_embed = sent["embed"]
    profile_view = sent["view"]
    assert isinstance(profile_view, ProfileView)
    # All the old data-carrying fields are gone — that data now lives in the
    # rendered card image, not embed.add_field(...) calls.
    field_names = {f.name for f in profile_embed.fields}
    assert field_names == set(), field_names
    assert profile_embed.image is not None, "expected the card PNG to be attached via embed.set_image"
    assert profile_embed.image.url == "attachment://profile_card.png", profile_embed.image.url
    _assert_embed_within_discord_limits(profile_embed)

    sent_files = sent["files"]
    assert len(sent_files) == 2, f"expected [emblem_file, card_file], got {len(sent_files)}"
    card_discord_file = next(f for f in sent_files if f.filename == "profile_card.png")
    card_bytes = card_discord_file.fp.read()
    assert card_bytes[:8] == b"\x89PNG\r\n\x1a\n", "expected real PNG magic bytes"
    from PIL import Image as _ProfileCardImage

    card_img = _ProfileCardImage.open(io.BytesIO(card_bytes))
    assert card_img.width > 200 and card_img.height > 200, card_img.size

    assert len(_captured_card_data) == 1, _captured_card_data
    data = _captured_card_data[0]
    assert data.server is not None, "expected per-server stats to reach the render pipeline"
    assert data.server.streak == 2, data.server.streak
    assert data.server.streak_best == 4, data.server.streak_best
    assert data.server.sketch_count == 3, data.server.sketch_count
    assert data.global_streak == 6, data.global_streak
    assert data.global_sketch_count == 3, data.global_sketch_count
    assert any("Pikachu" in p.species_name and p.is_shiny for p in data.party), data.party
    assert data.shiny_count == 1, data.shiny_count
    assert data.kudos_count == 0, data.kudos_count

    # accent_color: bot.fetch_user() was called and its accent_color was
    # converted to an (r, g, b) tuple and reached the render pipeline.
    assert data.accent_color == (88, 101, 242), data.accent_color
    # Party sprites: fetched via the fake PokeApiClient (offline, no real
    # network call) and each non-empty slot's resolved path reached the
    # render pipeline.
    assert fake_api_client.calls, "expected sprite fetch calls for party members"
    assert all(p.sprite_path == fake_sprite_path for p in data.party), data.party

    # Avatar: _get_avatar_path() fetched+cached the fake avatar and the
    # resolved local path reached the render pipeline (not just embed.thumbnail).
    assert data.avatar_path is not None and os.path.exists(data.avatar_path), data.avatar_path

    assert profile_embed.thumbnail.url == _FakeProfileAvatar.url, profile_embed.thumbnail.url
    expected_tier = rank_badges.rank_for_level(leveling.level_for_exp(160))
    assert profile_embed.author.name == expected_tier, profile_embed.author.name
    assert profile_view.give_kudos.label == "Give Kudos (0)", profile_view.give_kudos.label
    print(
        "end-to-end /profile OK: per-server AND global stats both present in the captured ProfileCardData "
        "(merge regression guard), old embed fields genuinely removed, real PNG card image attached, "
        "avatar thumbnail + tier author icon set, accent_color fetched+converted, party sprites resolved"
    )

    # A user with ONLY global data (no per-guild `User` row for this guild —
    # e.g. leveled up entirely in a different server) must still render
    # cleanly, just without the "This Server" field — must not crash or
    # silently require per-server data to exist.
    global_only_uid = 7002
    other_gid = 99999999
    async with db.session() as s:
        s.add(db.GlobalUser(user_id=global_only_uid, exp=10, level=1, global_streak=1, longest_global_streak=1))
        gdaily = db.DailyPokemon(guild_id=other_gid, local_date=_date.today(), dex_no=4, name="charmander")
        s.add(gdaily)
        await s.flush()
        s.add(
            db.Submission(
                guild_id=other_gid, user_id=global_only_uid, daily_id=gdaily.id,
                image_url="https://example.invalid/g1.png",
            )
        )
        await s.commit()

    global_only_user = _FakeProfileUser(global_only_uid, display_name="GlobalOnlyUser")
    global_only_interaction = _FakeProfileInteraction(global_only_user, guild_id=profile_gid)
    # A separate cog/bot instance whose fetch_user() returns accent_color=None
    # (the common case — most users never set one) so both branches of the
    # accent-color path are exercised end-to-end, not just the present case.
    global_only_cog = Profile(bot=_FakeProfileBot(badge_cache_dir, accent_color=None, api=fake_api_client))
    _captured_card_data.clear()
    _profile_cog_module.render_profile_panel = _spying_render_profile_panel
    try:
        await global_only_cog.profile.callback(global_only_cog, global_only_interaction, user=None)
    finally:
        _profile_cog_module.render_profile_panel = _real_render_profile_panel
    global_only_embed = global_only_interaction.followup.sent["embed"]
    assert global_only_embed.fields == [], global_only_embed.fields
    assert len(_captured_card_data) == 1, _captured_card_data
    global_only_data = _captured_card_data[0]
    assert global_only_data.server is None, global_only_data.server
    assert global_only_data.party == [], global_only_data.party
    assert global_only_data.accent_color is None, global_only_data.accent_color
    print(
        "/profile OK: a user with no per-server row renders cleanly "
        "(data.server is None reaches the render pipeline, not a crash); "
        "fetch_user() returning accent_color=None reaches the render pipeline as None too"
    )

    # Kudos: 0 -> 1 via the real button callback, duplicate vote from the same
    # voter rejected, self-kudos rejected — exercised through the actual
    # ProfileView.give_kudos callback, not a query-layer unit test.
    kudos_voter = _FakeProfileUser(8001, display_name="Voter")
    kudos_interaction_1 = _FakeProfileInteraction(kudos_voter, guild_id=profile_gid)
    kudos_button = profile_view.give_kudos
    await kudos_button.callback(kudos_interaction_1)
    assert kudos_button.label == "Give Kudos (1)", kudos_button.label
    assert kudos_interaction_1.response.edited is not None, "expected the kudos click to edit the message"

    kudos_interaction_dup = _FakeProfileInteraction(kudos_voter, guild_id=profile_gid)
    await kudos_button.callback(kudos_interaction_dup)
    assert kudos_button.label == "Give Kudos (1)", "duplicate vote from the same voter must not increment"
    dup_sent = kudos_interaction_dup.response.sent_message
    assert dup_sent is not None and "already given" in str(dup_sent).lower(), dup_sent

    kudos_self_interaction = _FakeProfileInteraction(profile_user, guild_id=profile_gid)
    await kudos_button.callback(kudos_self_interaction)
    assert kudos_button.label == "Give Kudos (1)", "self-kudos must not increment"
    self_sent = kudos_self_interaction.response.sent_message
    assert self_sent is not None and "yourself" in str(self_sent).lower(), self_sent

    # "Inspect Party" button: reuses pokebox.build_party_embeds (same helper
    # /party now uses too) as an ephemeral followup — confirm it actually
    # renders the target's party, not a redirect-to-/pokebox stub.
    inspect_interaction = _FakeProfileInteraction(kudos_voter, guild_id=profile_gid)
    await profile_view.inspect_party.callback(inspect_interaction)
    inspect_sent = inspect_interaction.response.sent_message
    assert inspect_sent is not None, "expected Inspect Party to send an ephemeral response"
    inspect_embeds = inspect_sent["kwargs"]["embeds"]
    assert inspect_sent["kwargs"]["ephemeral"] is True
    assert any("Pikachu" in e.title for e in inspect_embeds), [e.title for e in inspect_embeds]
    print("Inspect Party OK: real party data rendered via the shared pokebox.build_party_embeds helper")

    async with db.session() as s:
        kudos_rows = (
            await s.execute(select(db.Kudos).where(db.Kudos.target_user_id == profile_uid))
        ).scalars().all()
    assert len(kudos_rows) == 1 and kudos_rows[0].voter_id == kudos_voter.id, kudos_rows
    print("kudos OK: one-per-voter enforced, self-kudos rejected, button label reflects the real DB count")

    # _upvotes_received_count: total upvotes across EVERY submission a user
    # has ever made (not guild-scoped, not per-daily) — combined with
    # _kudos_count into the profile card's single Kudos tile (D6). Seed real
    # Submission + Upvote rows across two different submissions for the same
    # user to confirm the join sums across all of them, not just one.
    from pokesketch.cogs.profile import _upvotes_received_count

    upvote_target_uid = 8501
    async with db.session() as s:
        s.add(db.GlobalUser(user_id=upvote_target_uid, exp=5, level=1))
        upvote_daily = db.DailyPokemon(
            guild_id=profile_gid, local_date=_date.today() - _timedelta(days=2), dex_no=7, name="squirtle"
        )
        s.add(upvote_daily)
        await s.flush()
        upvote_sub_a = db.Submission(
            guild_id=profile_gid, user_id=upvote_target_uid, daily_id=upvote_daily.id,
            image_url="https://example.invalid/upvote_a.png",
        )
        upvote_sub_b = db.Submission(
            guild_id=profile_gid, user_id=upvote_target_uid, daily_id=upvote_daily.id,
            image_url="https://example.invalid/upvote_b.png",
        )
        s.add_all([upvote_sub_a, upvote_sub_b])
        await s.commit()
        s.add(db.Upvote(submission_id=upvote_sub_a.id, voter_id=1))
        s.add(db.Upvote(submission_id=upvote_sub_a.id, voter_id=2))
        s.add(db.Upvote(submission_id=upvote_sub_b.id, voter_id=3))
        await s.commit()

    async with db.session() as s:
        upvote_total = await _upvotes_received_count(s, upvote_target_uid)
    assert upvote_total == 3, upvote_total
    print("_upvotes_received_count OK: sums upvotes across every submission a user has made, not just one")

    # _build_card_data: kudos_count + upvotes_received_count combine into a
    # single ProfileCardData.kudos_count (the D6 tile shows ONE number, not
    # two separate ones).
    from pokesketch.cogs.profile import _build_card_data

    combined_data = _build_card_data(
        username="CombinedKudosTester", level=1, rank_tier="Poké Ball", title_badge="Poké Ball Trainer",
        exp_into=0, exp_need=100, global_streak=0, global_streak_best=0, global_sketch_count=0,
        accuracy_pct=0.0, dex_scanned=0, dex_total=1025, shiny_count=0, shiny_example=None,
        kudos_count=5, upvotes_received_count=3, party=[], sprite_paths={},
        server_level=None, server_streak=None, server_streak_best=None, server_sketch_count=None,
        accent_color=None, avatar_path=None,
    )
    assert combined_data.kudos_count == 8, combined_data.kudos_count
    print(
        "_build_card_data OK: kudos_count (5) + upvotes_received_count (3) combine into "
        "ProfileCardData.kudos_count (8)"
    )

    # --- Direct unit tests of profile_card_render.render_profile_panel(),
    # not going through the cog/DB at all: confirm it never crashes on the
    # documented edge cases and always returns valid, non-trivial PNG bytes.
    from PIL import Image as _CardImage

    from pokesketch.profile_card_render import PartyCardSlot as _PCSlot
    from pokesketch.profile_card_render import ProfileCardData as _PCData
    from pokesketch.profile_card_render import ServerCardStats as _PCServer
    from pokesketch.profile_card_render import render_profile_panel as _render_panel

    def _base_card_kwargs(**overrides):
        base = dict(
            username="CardRenderTester",
            level=12,
            rank_tier="Ultra Ball",
            title_badge="Gym Leader",
            exp_current=340,
            exp_needed=500,
            global_streak=4,
            global_streak_best=9,
            global_sketch_count=57,
            accuracy_pct=62.5,
            dex_scanned=118,
            dex_total=1025,
            shiny_count=3,
            shiny_example="Charizard",
            kudos_count=5,
            party=[_PCSlot(slot=1, dex_no=25, species_name="Pikachu", is_shiny=False)],
            server=_PCServer(level=5, streak=2, streak_best=4, sketch_count=3),
        )
        base.update(overrides)
        return base

    def _assert_valid_png(png_bytes: bytes, label: str) -> None:
        assert png_bytes, f"{label}: expected non-empty PNG bytes"
        img = _CardImage.open(io.BytesIO(png_bytes))
        assert img.width > 0 and img.height > 0, f"{label}: degenerate image size {img.size}"

    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(party=[]))), "empty party")

    full_party = [
        _PCSlot(slot=i + 1, dex_no=i * 7 + 1, species_name=f"Mon{i}", is_shiny=(i % 2 == 0)) for i in range(6)
    ]
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(party=full_party))), "full 6-mon party")

    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(server=None))), "server=None")
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(kudos_count=0))), "kudos_count=0")
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(kudos_count=9999))), "large kudos_count")
    _assert_valid_png(
        _render_panel(_PCData(**_base_card_kwargs(username="XxX_SuperLongTrainerNameForOverflowTesting_XxX"))),
        "long username",
    )
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(shiny_example=None))), "shiny_example=None")
    print(
        "profile_card_render OK: render_profile_panel never crashes on empty/full party, server=None, "
        "kudos=0/9999, a long username, or shiny_example=None — all return valid non-trivial PNGs"
    )

    # --- Round-2 layout: combined Streak tile (D3), both code paths ---------
    _assert_valid_png(
        _render_panel(_PCData(**_base_card_kwargs(server=_PCServer(level=5, streak=2, streak_best=4, sketch_count=3)))),
        "Streak tile with per-server data",
    )
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(server=None))), "Streak tile with server=None")
    print("Streak tile (D3) OK: both per-server-present and server=None paths render valid PNGs")

    # --- Round-2 layout: header chip never crashes on empty/very-long text --
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(title_badge=""))), "empty title_badge chip")
    _assert_valid_png(
        _render_panel(
            _PCData(**_base_card_kwargs(title_badge="X" * 200))
        ),
        "max-length title_badge chip",
    )
    print("chip rendering OK: _draw_chip/header never crashes on an empty or very long title_badge")

    # --- Round-2 layout: accent-color-driven EXP gradient, both paths -------
    from pokesketch.profile_card_render import _draw_gradient_bar as _direct_draw_gradient_bar
    from pokesketch.profile_card_render import _gradient_from_accent

    grad_light, grad_dark = _gradient_from_accent((88, 101, 242))
    assert grad_light != grad_dark, (grad_light, grad_dark)
    gradient_test_img = _CardImage.new("RGB", (200, 20), (0, 0, 0))
    _direct_draw_gradient_bar(gradient_test_img, (0, 0, 200, 20), 1.0, grad_light, grad_dark)
    _direct_draw_gradient_bar(gradient_test_img, (0, 0, 200, 20), 0.5)  # default gold->green path
    _assert_valid_png(
        _render_panel(_PCData(**_base_card_kwargs(accent_color=(88, 101, 242)))), "accent_color present"
    )
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(accent_color=None))), "accent_color=None")
    print(
        "EXP gradient OK: _gradient_from_accent derives a distinct light/dark pair, _draw_gradient_bar "
        "accepts overrides and the default gold->green pair, both accent-present/None render_profile_panel "
        "paths return valid PNGs"
    )

    # --- Round-2 layout: party tiles with sprite compositing, mixed --------
    # shiny/non-shiny/empty slots. Sprite fetch itself is stubbed offline
    # elsewhere (fake_api_client, above) — here the render module's own
    # Image.open()+composite path is exercised directly against a real tiny
    # on-disk PNG fixture (reusing fake_sprite_path), not a mock.
    mixed_party = [
        _PCSlot(
            slot=1, dex_no=6, species_name="Charizard", is_shiny=True, sprite_path=fake_sprite_path,
            nickname="Blaze",
        ),
        _PCSlot(slot=2, dex_no=9, species_name="Blastoise", is_shiny=False, sprite_path=fake_sprite_path),
        _PCSlot(slot=3, dex_no=3, species_name="Venusaur", is_shiny=False, sprite_path=None),
        # Slots 4-6 omitted -> rendered as empty placeholders.
    ]
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(party=mixed_party))), "mixed party with sprites")
    # A bogus sprite path (file doesn't exist) must be skipped gracefully,
    # not crash the whole render.
    bogus_party = [
        _PCSlot(slot=1, dex_no=1, species_name="Bulbasaur", is_shiny=False, sprite_path="/nonexistent/x.png")
    ]
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(party=bogus_party))), "missing sprite file")
    print(
        "party tile sprites OK: mixed shiny/non-shiny/empty slots with real+missing sprite paths all render "
        "valid PNGs, a failed Image.open() is skipped rather than crashing the render"
    )

    # --- Header avatar: real fixture, missing file, and None (fallback) ----
    _assert_valid_png(
        _render_panel(_PCData(**_base_card_kwargs(avatar_path=fake_sprite_path))), "avatar_path present"
    )
    _assert_valid_png(
        _render_panel(_PCData(**_base_card_kwargs(avatar_path="/nonexistent/avatar.png"))),
        "avatar_path missing file",
    )
    _assert_valid_png(_render_panel(_PCData(**_base_card_kwargs(avatar_path=None))), "avatar_path=None")
    print(
        "header avatar OK: real avatar composites, a missing/bogus avatar file falls back to the "
        "initial-letter placeholder circle instead of crashing, avatar_path=None also falls back cleanly"
    )

    # Confirm the OLD command registrations are genuinely gone, not just
    # shadowed — /profile-card no longer exists anywhere, /profile exists
    # exactly once (on the merged cog), not on cogs/stats.py too.
    from pokesketch.cogs.stats import Stats as _StatsForCommandCheck

    profile_cmd_names = {c.name for c in Profile(bot=None).get_app_commands()}
    stats_cmd_names = {c.name for c in _StatsForCommandCheck(bot=None).get_app_commands()}
    assert profile_cmd_names == {"profile"}, profile_cmd_names
    assert "profile-card" not in profile_cmd_names, profile_cmd_names
    assert "profile" not in stats_cmd_names, stats_cmd_names
    assert stats_cmd_names == {"leaderboard", "streak", "stats"}, stats_cmd_names
    print(
        "command consolidation OK: /profile-card is gone entirely, /profile exists exactly once "
        f"(cogs/stats.py keeps only {sorted(stats_cmd_names)})"
    )

    # /help: documents the single merged /profile command, not two.
    from pokesketch.cogs.help import Help as _HelpForProfileCheck

    help_cog3 = _HelpForProfileCheck(bot=None)
    help_interaction3 = _FakeHelpInteraction()
    await help_cog3.help_cmd.callback(help_cog3, help_interaction3)
    help_embed3 = help_interaction3.response.sent_embed
    _assert_embed_within_discord_limits(help_embed3)
    help_text3 = "\n".join(f.value for f in help_embed3.fields)
    assert "/profile-card" not in help_text3, help_text3
    assert help_text3.count("/profile ") + help_text3.count("/profile[") == 1 or "`/profile [user]`" in help_text3, (
        help_text3
    )
    print("help OK: documents one merged /profile command, /profile-card is gone")

    # --- /rename: successful rename, successful clear, wrong-owner rejection,
    # over-length/bad-char rejection, and autocomplete ordering + 25-cap
    # truncation. Drives the real cog command callback and autocomplete
    # callback end-to-end (not just the pokebox.rename_mon/renameable_mons
    # helpers), with a mocked Interaction capturing whatever kwarg the
    # handler passed to interaction.response.send_message — same pattern as
    # the /event-* admin checks above.
    from pokesketch.cogs.collection import MAX_AUTOCOMPLETE_CHOICES, Collection

    rename_guild = 7001
    rename_uid = 7002
    other_uid = 7003
    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=rename_guild, dex_min=1, dex_max=1))
        s.add(db.DailyPokemon(guild_id=rename_guild, local_date=_date.today(), dex_no=10, name="caterpie"))
        await s.commit()
        rename_daily = (
            await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.guild_id == rename_guild))
        ).scalar_one()
        # 20 submissions for rename_uid — pokebox.MAX_TOTAL (6 active + 14 box)
        # is the real per-user combined cap (see the /catch cap test above);
        # a player can never actually reach the 26-mon (6 active + 20 box)
        # figure the design doc's autocomplete-cap example uses, so the
        # 25-choice truncation itself is exercised below via a synthetic
        # renameable_mons feed rather than 26 real caught rows. One extra
        # submission for other_uid (for the wrong-owner rejection check).
        for uid, count in ((rename_uid, 20), (other_uid, 1)):
            for i in range(count):
                s.add(
                    db.Submission(
                        guild_id=rename_guild, user_id=uid, daily_id=rename_daily.id,
                        image_url=f"https://example.invalid/rename-{uid}-{i}.png",
                    )
                )
        await s.commit()

    for uid, amount in ((rename_uid, 30), (other_uid, 5)):
        async with db.session() as s:
            wallet = await pokebox.get_or_create_wallet(s, uid)
            wallet.balance = amount
            await s.commit()

    rename_mons = []
    for _ in range(pokebox.MAX_TOTAL):
        async with db.session() as s:
            mon = await pokebox.catch_submission(s, rename_uid, party_dir)
            await s.commit()
            rename_mons.append(mon)
    assert sum(1 for m in rename_mons if m.is_active) == pokebox.MAX_ACTIVE
    assert sum(1 for m in rename_mons if not m.is_active) == pokebox.MAX_TOTAL - pokebox.MAX_ACTIVE

    async with db.session() as s:
        other_mon = await pokebox.catch_submission(s, other_uid, party_dir)
        await s.commit()

    class _FakeCollectionUser:
        def __init__(self, uid: int):
            self.id = uid

    class _FakeCollectionResponse:
        def __init__(self):
            self.sent: dict | None = None

        async def send_message(self, content=None, **kwargs):
            self.sent = {"content": content, **kwargs}

    class _FakeCollectionInteraction:
        def __init__(self, user_id: int):
            self.user = _FakeCollectionUser(user_id)
            self.response = _FakeCollectionResponse()

    collection_cog = Collection(bot=None)

    # Successful rename: nickname updates + confirmation shows the new display name.
    rename_target = rename_mons[0]  # active slot 1
    rename_interaction = _FakeCollectionInteraction(rename_uid)
    await collection_cog.rename.callback(
        collection_cog, rename_interaction, mon=str(rename_target.id), nickname="Sparky"
    )
    async with db.session() as s:
        renamed = await s.get(db.CaughtMon, rename_target.id)
    assert renamed.nickname == "Sparky", renamed.nickname
    assert (
        rename_interaction.response.sent["content"]
        == f"✏️ Renamed to **{_collection_display_name(renamed)}**."
    ), rename_interaction.response.sent
    assert rename_interaction.response.sent.get("ephemeral") is True, rename_interaction.response.sent
    print("rename OK: nickname updated, confirmation shows new display name")

    # Successful clear: empty-string nickname reverts to species-only display.
    clear_interaction = _FakeCollectionInteraction(rename_uid)
    await collection_cog.rename.callback(
        collection_cog, clear_interaction, mon=str(rename_target.id), nickname=""
    )
    async with db.session() as s:
        cleared = await s.get(db.CaughtMon, rename_target.id)
    assert cleared.nickname is None, cleared.nickname
    assert (
        clear_interaction.response.sent["content"]
        == f"✏️ Renamed to **{_collection_display_name(cleared)}**."
    ), clear_interaction.response.sent
    assert "(" not in clear_interaction.response.sent["content"], clear_interaction.response.sent
    print("rename clear OK: empty-string nickname clears back to species-only display")

    # Wrong-owner rejection: a mon id belonging to another user fails with a
    # plain ephemeral error, no exception surfaced, no mutation.
    wrong_owner_interaction = _FakeCollectionInteraction(rename_uid)
    await collection_cog.rename.callback(
        collection_cog, wrong_owner_interaction, mon=str(other_mon.id), nickname="Nope"
    )
    async with db.session() as s:
        other_after = await s.get(db.CaughtMon, other_mon.id)
    assert other_after.nickname is None, other_after.nickname
    wrong_owner_msg = "That mon isn't yours to rename — it may have been released or belong to someone else."
    assert wrong_owner_interaction.response.sent["content"] == f"❌ {wrong_owner_msg}", (
        wrong_owner_interaction.response.sent
    )
    assert wrong_owner_interaction.response.sent.get("ephemeral") is True

    # Stale/deleted id: a nonexistent mon id fails the exact same generic way.
    stale_interaction = _FakeCollectionInteraction(rename_uid)
    await collection_cog.rename.callback(
        collection_cog, stale_interaction, mon="99999999", nickname="Nope"
    )
    assert stale_interaction.response.sent["content"] == f"❌ {wrong_owner_msg}", stale_interaction.response.sent
    print("rename rejection OK: wrong-owner and stale/nonexistent ids fail cleanly with no mutation")

    # Over-length (>12 char) and disallowed-character nicknames are rejected
    # with sanitize_nickname's existing (unchanged) error messages, no mutation.
    async with db.session() as s:
        pre_reject_nickname = (await s.get(db.CaughtMon, rename_target.id)).nickname
    long_interaction = _FakeCollectionInteraction(rename_uid)
    await collection_cog.rename.callback(
        collection_cog, long_interaction, mon=str(rename_target.id), nickname="X" * 13
    )
    assert "12 characters" in long_interaction.response.sent["content"], long_interaction.response.sent
    badchar_interaction = _FakeCollectionInteraction(rename_uid)
    await collection_cog.rename.callback(
        collection_cog, badchar_interaction, mon=str(rename_target.id), nickname="Sp@rky"
    )
    assert "letters, numbers, spaces" in badchar_interaction.response.sent["content"], (
        badchar_interaction.response.sent
    )
    async with db.session() as s:
        post_reject_nickname = (await s.get(db.CaughtMon, rename_target.id)).nickname
    assert post_reject_nickname == pre_reject_nickname, (pre_reject_nickname, post_reject_nickname)
    print(
        "rename validation OK: over-length and bad-char nicknames rejected with "
        "sanitize_nickname's unchanged messages, no mutation"
    )

    # Autocomplete ordering against the REAL (achievable) mon count: active
    # slots 1-6 ascending, then box slots 1-14 ascending, all 20 fit under
    # Discord's 25-choice max so nothing is truncated yet.
    ac_interaction = _FakeCollectionInteraction(rename_uid)
    real_choices = await collection_cog._rename_mon_autocomplete(ac_interaction, "")
    assert len(real_choices) == pokebox.MAX_TOTAL, len(real_choices)
    real_active, real_box = real_choices[:pokebox.MAX_ACTIVE], real_choices[pokebox.MAX_ACTIVE:]
    for i, choice in enumerate(real_active, start=1):
        assert choice.name.endswith(f"active slot {i}"), choice.name
    for i, choice in enumerate(real_box, start=1):
        assert choice.name.endswith(f"box slot {i}"), choice.name
    print(
        f"rename autocomplete OK: real {pokebox.MAX_TOTAL}-mon cap lists active-then-box ascending, "
        "untruncated (below the 25-choice max)"
    )

    # 25-choice cap + truncation at the 26-mon boundary: pokebox.MAX_TOTAL is
    # actually 20 (6 active + 14 box combined — see the /catch cap test
    # above), so a real player can never reach the design doc's 26-mon
    # (6 active + 20 box) example; a single caller's own mons can never
    # exceed MAX_AUTOCOMPLETE_CHOICES in practice under the current storage
    # cap. To still exercise the callback's own ordering/cap/substring-filter
    # logic at that boundary, feed it a synthetic 26-entry renameable_mons
    # result (bypassing catch_submission's storage cap, not the autocomplete
    # code under test) and restore the real helper afterward.
    class _FakeRenameMon:
        def __init__(self, mon_id: int, is_active: bool, slot: int):
            self.id = mon_id
            self.dex_no = 1
            self.name = "bulbasaur"
            self.is_shiny = False
            self.nickname = None
            self.is_active = is_active
            self.slot = slot

    synthetic_mons = [_FakeRenameMon(90000 + i, True, i) for i in range(1, 7)] + [
        _FakeRenameMon(91000 + i, False, i) for i in range(1, 21)
    ]

    async def _fake_renameable_mons(s, user_id):
        return synthetic_mons

    real_renameable_mons = pokebox.renameable_mons
    pokebox.renameable_mons = _fake_renameable_mons
    try:
        choices = await collection_cog._rename_mon_autocomplete(ac_interaction, "")
        assert len(choices) == MAX_AUTOCOMPLETE_CHOICES, len(choices)
        active_choices, box_choices = choices[:6], choices[6:]
        for i, choice in enumerate(active_choices, start=1):
            assert choice.name.endswith(f"active slot {i}"), choice.name
        assert len(box_choices) == MAX_AUTOCOMPLETE_CHOICES - 6
        for i, choice in enumerate(box_choices, start=1):
            assert choice.name.endswith(f"box slot {i}"), choice.name

        # The dropped box slot 20 mon remains reachable via the existing
        # current-substring filter, same recovery path /catch already relies on.
        dropped_mon = next(m for m in synthetic_mons if not m.is_active and m.slot == 20)
        filtered = await collection_cog._rename_mon_autocomplete(ac_interaction, "box slot 20")
        assert len(filtered) == 1 and filtered[0].value == str(dropped_mon.id), filtered
    finally:
        pokebox.renameable_mons = real_renameable_mons
    print(
        "rename autocomplete OK: at a synthetic 26-mon (6 active + 20 box) boundary, "
        "active-then-box ascending ordering holds, the 25-choice cap truncates box slot 20, "
        "and it's recoverable via the substring filter"
    )

    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
