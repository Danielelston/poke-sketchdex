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
    from pokesketch.cogs.submissions import (
        _is_outside_grace_window,
        _submission_confirmation,
        _submission_header,
    )
    from pokesketch.formatting import species_display_name

    assert species_display_name("mr-mime") == "Mr Mime", species_display_name("mr-mime")
    header = _submission_header(species_display_name("pikachu"), "<@123>")
    assert header == "🖼️ Pikachu by <@123> — react 👍 to upvote!", header
    assert _submission_confirmation("Pikachu", True, " (+15 EXP)", 24) == (
        "✅ Pikachu submitted (+15 EXP)! 🎯 " + "[" + "█" * 10 + "]" + " Catchable for 1d."
    )
    assert _submission_confirmation("Pikachu", False, "", 24) == (
        "✅ Updated your Pikachu sketch! 🎯 " + "[" + "█" * 10 + "]" + " Catchable for 1d."
    )
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
        confirmation, exp_msg = await subs_cog._submit_to_wild_encounter(
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
        confirmation2, exp_msg2 = await subs_cog._submit_to_daily(
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

    # Submit-vs-catch window visual: format_duration_label / duration_bar / the
    # /submit confirmation's live catch-window label+bar.
    from pokesketch.cogs.submissions import _submission_confirmation
    from pokesketch.daily import _window_summary_line
    from pokesketch.pokebox import duration_bar, format_duration_label

    assert format_duration_label(4) == "4h", format_duration_label(4)
    assert format_duration_label(24) == "1d", format_duration_label(24)
    assert format_duration_label(36) == "2d", format_duration_label(36)
    assert format_duration_label(168) == "7d", format_duration_label(168)
    print("format_duration_label OK: 4h/1d/2d/7d")

    # duration_bar: 0 -> empty, full -> fully filled, small-but-nonzero -> at
    # least 1 filled cell (never renders a real window as fully empty).
    assert duration_bar(0, 24) == "[" + "░" * 10 + "]", duration_bar(0, 24)
    assert duration_bar(24, 24) == "[" + "█" * 10 + "]", duration_bar(24, 24)
    small_bar = duration_bar(1, 168)
    assert small_bar.count("█") == 1, small_bar
    print(f"duration_bar OK: empty={duration_bar(0, 24)}, full={duration_bar(24, 24)}, small={small_bar}")

    # Equal-windows degenerate case: catch_window_hours == grace_period_days*24
    # must render as a fully-filled bar and matching-unit labels (no implied gap).
    equal_line = _window_summary_line(grace_period_days=7, catch_window_hours=168)
    assert "Submit window: 7d" in equal_line, equal_line
    assert "Catch window: 7d" in equal_line, equal_line
    assert "█" * 10 in equal_line, equal_line
    print("window summary OK: equal submit/catch windows render as a fully-filled bar")

    default_line = _window_summary_line(grace_period_days=7, catch_window_hours=23)
    assert "Submit window: 7d" in default_line, default_line
    assert "Catch window: 23h" in default_line, default_line
    print(f"window summary OK (default config): {default_line.splitlines()[0]}")

    confirmation = _submission_confirmation("Pikachu", True, " (+15 EXP)", 23)
    assert "Pikachu submitted (+15 EXP)!" in confirmation, confirmation
    assert "Catchable for 23h" in confirmation, confirmation
    assert "█" * 10 in confirmation, confirmation  # just-submitted: full bar
    print(f"submission confirmation OK: {confirmation}")

    update_confirmation = _submission_confirmation("Bulbasaur", False, "", 168)
    assert "Updated your Bulbasaur sketch!" in update_confirmation, update_confirmation
    assert "Catchable for 7d" in update_confirmation, update_confirmation
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
    from pokesketch.cogs.help import _admin_field_value

    admin_text = _admin_field_value()
    assert "/set-grace-period" in admin_text, admin_text
    assert "/set-catch-window" in admin_text, admin_text
    print("help-admin OK: admin command list includes the grace-period and catch-window settings")

    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
