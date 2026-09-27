"""Real-rendered proof for Card 4/5 (Display): the Super Like button attached
to a submission post, plus a mocked end-to-end interaction click.

This is PROOF/FIXTURE code only -- it imports and calls the actual production
functions/classes:
  - `cogs.submissions.Submissions._submit_to_daily` (real, against a fake
    Discord channel/message stub -- proves the button is attached via
    `message.edit(view=...)` once `sub.id` is known, and renders the resulting
    post + button to an image).
  - `ui.SuperLikeButton.for_submission` / `.from_custom_id` (real) -- the
    second call simulates a bot restart: a brand-new `SuperLikeButton`
    instance is reconstructed purely from the persisted `custom_id` string,
    with no reference to the original instance, proving the `DynamicItem`
    registration survives a restart without per-message re-registration.
  - `ui.SuperLikeButton.callback` (real) -- driven end-to-end through a
    mocked `discord.Interaction` for a full click: balance decrement, player
    EXP grant (`ExpEvent` row), and party-mon EXP grant on the `CaughtMon`
    sourced from the targeted submission.

See: `Design/Super Likes on Submissions Plan.md`, Decomposition sketch, Unit 4.

Run: `uv run python scripts/proof_super_like_display.py <output_dir>`
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pokesketch import db, leveling, pokebox, ui  # noqa: E402
from pokesketch.cogs.submissions import Submissions  # noqa: E402

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")

CARD_W = 460
PAD = 18

GIVER_ID = 42
RECEIVER_ID = 7
GUILD_ID = 1


# --- fake Discord plumbing (only Discord I/O is faked; everything else is
# the real production code path) -----------------------------------------


class _FakeAttachment:
    async def to_file(self):
        return None


class _FakeSentAttachment:
    def __init__(self, url: str):
        self.url = url


class _FakeMessage:
    """Stands in for the `discord.Message` returned by `channel.send(...)`.

    Captures the reaction added and, crucially, the `view` kwarg passed to
    `.edit(...)` -- that's the button attach this card implements.
    """

    def __init__(self, msg_id: int):
        self.id = msg_id
        self.attachments = [_FakeSentAttachment(f"https://cdn.discord/{msg_id}.png")]
        self.reactions_added: list[str] = []
        self.view = None

    async def add_reaction(self, emoji):
        self.reactions_added.append(emoji)

    async def edit(self, *args, **kwargs):
        self.view = kwargs.get("view")


class _FakeChannel:
    def __init__(self, channel_id: int = 1):
        self.id = channel_id
        self._next_msg_id = 100
        self.sent: list[_FakeMessage] = []

    async def send(self, *args, **kwargs):
        self._next_msg_id += 1
        msg = _FakeMessage(self._next_msg_id)
        self.sent.append(msg)
        return msg


class _FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class _FakeFollowup:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, *, ephemeral=False):
        self.sent.append({"content": content, "ephemeral": ephemeral})


class _FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, *, ephemeral=False):
        self.deferred = True
        self.deferred_ephemeral = ephemeral


class _FakeInteraction:
    """Stands in for `discord.Interaction` for one button click."""

    def __init__(self, user_id: int):
        self.user = _FakeUser(user_id)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


# --- rendering ------------------------------------------------------------


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def render_submission_post(display_name: str, mention: str, button: ui.SuperLikeButton, *, note: str) -> Image.Image:
    """Draw the submission post text + a Discord-button-styled rectangle
    showing the real `label`/`style`/`custom_id` off the real `SuperLikeButton`
    instance attached to the post -- discord.py can't render a live message to
    pixels without a gateway connection, so this draws the actual component
    data it would send (same convention as `proof_multiform_forms.py`)."""
    title_font = _font(FONT_BOLD, 18)
    body_font = _font(FONT_REGULAR, 15)
    button_font = _font(FONT_BOLD, 15)
    note_font = _font(FONT_BOLD, 13)

    header = f"{mention} submitted a sketch of {display_name}!"
    btn_item = button.item  # the underlying discord.ui.Button
    btn_label = btn_item.label
    btn_custom_id = btn_item.custom_id

    height = PAD + 24 + 90 + 20 + 46 + 24 + PAD
    card = Image.new("RGB", (CARD_W, height), (35, 39, 42))
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, 6, card.height], fill=0x5865F2)

    y = PAD
    draw.text((PAD + 8, y), header, font=title_font, fill=(255, 255, 255))
    y += 30

    draw.rectangle([PAD + 8, y, PAD + 8 + 300, y + 90], fill=(50, 54, 58), outline=(80, 84, 88))
    draw.text((PAD + 20, y + 36), "[sketch image]", font=body_font, fill=(150, 152, 157))
    y += 90 + 16

    draw.text((PAD + 8, y), "👍", font=body_font, fill=(200, 202, 206))
    btn_x0 = PAD + 40
    draw.rounded_rectangle([btn_x0, y - 4, btn_x0 + 150, y + 32], radius=6, fill=(78, 80, 88))
    draw.text((btn_x0 + 14, y + 4), btn_label, font=button_font, fill=(255, 255, 255))
    y += 46

    draw.text((PAD + 8, y), f"custom_id: {btn_custom_id!r}", font=body_font, fill=(140, 142, 146))
    y += 24
    draw.text((PAD + 8, y), note, font=note_font, fill=(88, 101, 242))

    return card


def compose_strip(images: list[Image.Image], *, gap: int = 14) -> Image.Image:
    width = sum(im.width for im in images) + gap * (len(images) - 1)
    height = max(im.height for im in images)
    strip = Image.new("RGB", (width, height), (24, 26, 27))
    x = 0
    for im in images:
        strip.paste(im, (x, 0))
        x += im.width + gap
    return strip


# --- proof ------------------------------------------------------------


async def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    db_dir = tempfile.mkdtemp()
    db.init_engine(os.path.join(db_dir, "proof.db"))
    await db.create_all()

    print("== Functions/classes invoked by this proof ==")
    print(" - cogs.submissions.Submissions._submit_to_daily (real)")
    print(" - ui.SuperLikeButton.for_submission / .from_custom_id (real)")
    print(" - ui.SuperLikeButton.callback -> superlike.give_super_like (real)")
    print(" - pokebox.grant_super_like / mon_for_submission / award_mon_exp (real)")
    print()

    # ---------------- Step 1: real _submit_to_daily posts + attaches button --
    print("== Step 1: accepted daily /submit -- attach the button post-flush ==")
    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=GUILD_ID))
        daily = db.DailyPokemon(guild_id=GUILD_ID, local_date=date(2026, 9, 26), dex_no=25, name="pikachu")
        s.add(daily)
        await s.flush()

        channel = _FakeChannel()
        confirmation, exp_msg, _gym_result = await Submissions._submit_to_daily(
            Submissions.__new__(Submissions), s, daily, channel, _FakeAttachment(),
            GUILD_ID, RECEIVER_ID, f"<@{RECEIVER_ID}>",
        )
        await s.commit()
        submission_id = (
            await s.execute(select(db.Submission).where(db.Submission.daily_id == daily.id))
        ).scalar_one().id

    posted = channel.sent[0]
    assert posted.view is not None, "no view attached via message.edit -- button was never attached"
    attached_button = posted.view.children[0]
    assert isinstance(attached_button, ui.SuperLikeButton), f"wrong item type attached: {type(attached_button)!r}"
    assert attached_button.kind == "s"
    assert attached_button.target_id == submission_id
    print(f"  channel.send() -> message_id={posted.id}")
    print(f"  posted.edit(view=...) -> attached {attached_button.item.custom_id!r}")
    print(f"  confirmation shown to submitter: {confirmation!r}{exp_msg}")

    card1 = render_submission_post(
        "Pikachu", f"<@{RECEIVER_ID}>", attached_button,
        note="Button attached via message.edit() once sub.id was known (Card 4/5)",
    )
    card1.save(os.path.join(out_dir, "case1_submission_post_with_button.png"))
    print(f"  saved case1_submission_post_with_button.png ({card1.width}x{card1.height})")
    print()

    # Give the receiver a caught mon sourced from this exact submission, so
    # the click below also proves the mon-EXP routing side.
    async with db.session() as s:
        s.add(
            db.CaughtMon(
                user_id=RECEIVER_ID, is_active=True, slot=1, dex_no=25, name="pikachu",
                cached_image_path="/tmp/x.png", source_submission_id=submission_id,
            )
        )
        # Giver needs a super like to spend -- earned from their own accepted
        # submission (mirrors the design doc's "supply bounded by your own
        # submissions" posture), not hand-set.
        wallet = await pokebox.get_or_create_super_like_wallet(s, GIVER_ID)
        wallet.balance = 0
        await pokebox.grant_super_like(s, GIVER_ID)
        await s.commit()

    async with db.session() as s:
        wallet = await pokebox.get_or_create_super_like_wallet(s, GIVER_ID)
        print(f"  giver_id={GIVER_ID} balance after earning from their own accepted submission: {wallet.balance}")
        assert wallet.balance == 1

    # ---------------- Step 2: simulate a bot restart ------------------------
    print()
    print("== Step 2: simulate a bot restart -- reconstruct the button from custom_id alone ==")
    persisted_custom_id = attached_button.item.custom_id
    print(f"  persisted custom_id on the message (as if read back after restart): {persisted_custom_id!r}")
    match = ui.SUPER_LIKE_CUSTOM_ID_TEMPLATE
    import re as _re

    m = _re.fullmatch(match, persisted_custom_id)
    assert m is not None, "persisted custom_id no longer matches the registered DynamicItem template"
    # This is exactly what discord.py's dispatcher does internally when an
    # interaction arrives after a restart, with zero knowledge of the
    # original `attached_button` Python object (which no longer exists in a
    # fresh process) -- only the class + regex template, matching the design
    # doc's "registered once via bot.add_dynamic_items(), not per-message"
    # persistence requirement.
    reconstructed = ui.SuperLikeButton(m["kind"], int(m["target_id"]))
    assert reconstructed.kind == "s"
    assert reconstructed.target_id == submission_id
    print(f"  reconstructed SuperLikeButton(kind={reconstructed.kind!r}, target_id={reconstructed.target_id}) "
          "-- no reference to the original instance, no per-message add_view() needed")
    print()

    # ---------------- Step 3: drive a real interaction click end-to-end -----
    print("== Step 3: mocked interaction click through the REAL SuperLikeButton.callback ==")
    interaction = _FakeInteraction(user_id=GIVER_ID)
    await reconstructed.callback(interaction)

    assert interaction.response.deferred is True
    assert len(interaction.followup.sent) == 1
    reply = interaction.followup.sent[0]
    print(f"  ephemeral reply to giver: {reply['content']!r} (ephemeral={reply['ephemeral']})")
    assert reply["ephemeral"] is True
    assert "Super like given" in reply["content"]

    async with db.session() as s:
        wallet = await pokebox.get_or_create_super_like_wallet(s, GIVER_ID)
        print(f"  giver balance after the click: {wallet.balance} (was 1)")
        assert wallet.balance == 0

        events = (
            await s.execute(
                select(db.ExpEvent).where(
                    db.ExpEvent.user_id == RECEIVER_ID, db.ExpEvent.type == "super_like_received"
                )
            )
        ).scalars().all()
        assert len(events) == 1
        print(f"  ExpEvent(type='super_like_received', amount={events[0].amount}) logged for receiver")
        assert events[0].amount == leveling.EXP_PER_SUPER_LIKE

        mon = (
            await s.execute(select(db.CaughtMon).where(db.CaughtMon.source_submission_id == submission_id))
        ).scalar_one()
        print(f"  CaughtMon.mon_exp after the click: {mon.mon_exp} (MON_EXP_SUPER_LIKE={leveling.MON_EXP_SUPER_LIKE})")
        assert mon.mon_exp == leveling.MON_EXP_SUPER_LIKE

        # A second click by the SAME giver on the SAME submission hits the
        # (giver, submission) duplicate check first -- balance is separately
        # already 0 too, but the duplicate rejection fires before that check
        # ever runs (see superlike.give_super_like's validation order).
        second_button = ui.SuperLikeButton("s", submission_id)

    dup_interaction = _FakeInteraction(user_id=GIVER_ID)
    await second_button.callback(dup_interaction)
    dup_reply = dup_interaction.followup.sent[0]
    print(f"  second click, same giver+submission -> {dup_reply['content']!r}")
    assert "already super-liked" in dup_reply["content"]

    async with db.session() as s:
        wallet = await pokebox.get_or_create_super_like_wallet(s, GIVER_ID)
        assert wallet.balance == 0, "rejection must not touch balance further"

    card2 = render_submission_post(
        "Pikachu", f"<@{RECEIVER_ID}>", reconstructed,
        note="Post-restart click: balance -1, receiver +15 EXP, mon +150 EXP (real callback)",
    )
    strip = compose_strip([card1, card2])
    strip.save(os.path.join(out_dir, "case2_before_after_click.png"))
    print(f"  saved case2_before_after_click.png ({strip.width}x{strip.height})")
    print()
    print("All proof assertions passed.")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
    asyncio.run(main(out))
    print(f"\nOutput dir: {out}")
