"""Reusable Discord UI components (buttons, views)."""

from __future__ import annotations

import logging

import discord
from sqlalchemy import select

from . import db
from .formatting import species_display_name
from .pokeapi import SpeciesForms

log = logging.getLogger(__name__)


class ConfirmView(discord.ui.View):
    """Generic Confirm/Cancel button pair for destructive admin actions.

    Usage:
        view = ConfirmView(author_id=interaction.user.id)
        await interaction.response.send_message("Are you sure?", view=view, ephemeral=True)
        await view.wait()
        if view.confirmed:
            ...

    Only `author_id` may press the buttons. On timeout (default 60s) or
    Cancel, `confirmed` is left False/None and the caller should treat that
    as a no-op; this view does not perform any action itself.
    """

    def __init__(self, author_id: int, *, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed: bool | None = None
        self.interaction: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.interaction is not None:
            try:
                await self.interaction.edit_original_response(
                    content="⌛ Confirmation expired — nothing was changed.", view=self
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.edit_message(content="❌ Cancelled — nothing was changed.", view=self)


class PaginatorView(discord.ui.View):
    """Prev/Next pager over a list of pre-built embeds.

    Usage:
        view = PaginatorView(pages, author_id=interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view)
    """

    def __init__(self, pages: list[discord.Embed], *, author_id: int, timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author_id = author_id
        self.index = 0
        self.message: discord.Message | None = None
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can page through this.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = max(0, self.index - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


# Static custom_id for the persistent "View Alt Forms" button — see
# FormsButtonView's docstring for why this must never vary per-message.
FORMS_BUTTON_CUSTOM_ID = "view_alt_forms"


def _build_form_pages(dex_no: int, species_name: str, forms: SpeciesForms, shiny: bool) -> list[discord.Embed]:
    """Build the full list of ephemeral gallery pages for a species' forms,
    default variety first (see `SpeciesForms`/`SpeciesForm` ordering), all
    reflecting the same day's shiny roll. Built once up front, per
    `PaginatorView(pages: list[Embed], ...)`'s existing shape, so paging
    afterwards is a pure `edit_message` call with no further PokeAPI I/O.

    When the species-forms cache reports more eligible forms exist than were
    sampled/shown (`SpeciesForms.more_forms_exist`), a closing note page is
    appended so a user paging to the end sees why the count looks capped.
    Returns an empty list only in the degenerate case where `forms.forms` is
    itself empty (a lookup failure) — callers must handle that themselves.
    """
    total = len(forms.forms)
    pages: list[discord.Embed] = []
    for i, form in enumerate(forms.forms, start=1):
        images = form.reference_images(shiny=shiny)
        shiny_tag = " ✨(Shiny!)" if shiny else ""
        embed = discord.Embed(
            title=f"#{dex_no:04d} {form.display_name()}{shiny_tag}",
            description=f"Form {i}/{total}",
            color=0x5865F2,
        )
        if images:
            embed.set_image(url=images[0])
        if len(images) > 1:
            embed.set_thumbnail(url=images[1])
        embed.set_footer(text="PokeSketchDex • alt forms")
        pages.append(embed)
    if forms.more_forms_exist:
        pages.append(
            discord.Embed(
                title=f"#{dex_no:04d} {species_display_name(species_name)} has more forms",
                description=(
                    f"Showing {total} of {forms.eligible_total} eligible forms — "
                    "more exist beyond what's sampled here."
                ),
                color=0x5865F2,
            )
        )
    return pages


async def _resolve_species_for_message(message_id: int | None) -> tuple[int, str, bool] | None:
    """Resolve a clicked "View Alt Forms" button's message to the species it
    belongs to, by checking `DailyPokemon.announce_message_id` then
    `WildEncounter.message_id` (whichever matches) — see the design doc's
    button-persistence decision. Neither column is indexed beyond its
    table's primary key, so this is a full-column scan; not a performance
    concern at this bot's data volumes.

    Returns `(dex_no, name, is_shiny)`, or None if the message doesn't match
    either table (e.g. a post older than data retention). Wild encounters
    have no shiny mechanic (`WildEncounter` carries no `is_shiny` column —
    see `WildEncounterSubmission.is_shiny`), so a wild-encounter match always
    reports `is_shiny=False`.
    """
    if message_id is None:
        return None
    async with db.session() as s:
        daily = (
            await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.announce_message_id == message_id))
        ).scalar_one_or_none()
        if daily is not None:
            return daily.dex_no, daily.name, daily.is_shiny
        wild = (
            await s.execute(select(db.WildEncounter).where(db.WildEncounter.message_id == message_id))
        ).scalar_one_or_none()
        if wild is not None:
            return wild.dex_no, wild.name, False
    return None


class _FormsPaginatorView(discord.ui.View):
    """Short-lived Prev/Next pager over a species' alt-forms pages, attached
    to one user's ephemeral response from `FormsButtonView`. Unlike
    `PaginatorView`, this carries no `interaction_check`/`author_id` —
    Discord already scopes an ephemeral message's components to the single
    user it was sent to, so no additional restriction is needed. A fresh
    instance is built per click (see `FormsButtonView.view_alt_forms`), so
    two users clicking the same post concurrently each get their own
    `index`; paging one never touches the other's.
    """

    def __init__(self, pages: list[discord.Embed], *, timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages
        self.index = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.pages) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = max(0, self.index - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


class FormsButtonView(discord.ui.View):
    """Persistent "View Alt Forms" button attached to daily/encounter posts.

    Must be constructed exactly once and registered via `bot.add_view(...)`
    in `PokeSketchDexBot.setup_hook` (see `bot.py`) — NOT built per-message,
    which would silently defeat persistence. Its button's `custom_id` is the
    static `FORMS_BUTTON_CUSTOM_ID`, so Discord can route a click on *any*
    message carrying this view to this one registered instance, for the
    full life of the post (days, per `grace_period_days`) across bot
    restarts/deploys — the click resolves which species it belongs to at
    click-time via `interaction.message.id`, not from any state stored on
    this view or the button itself.

    Any guild member may click (no `interaction_check`/author-restriction,
    unlike `ConfirmView`/`PaginatorView`) — this sits on a public post, not a
    single user's command result. The resulting gallery is always ephemeral,
    regardless of who clicked, and multiple concurrent clicks are safe: each
    builds its own `_FormsPaginatorView` from the same cached forms data,
    with no shared mutable state.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="View Alt Forms", style=discord.ButtonStyle.secondary, custom_id=FORMS_BUTTON_CUSTOM_ID)
    async def view_alt_forms(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Defer immediately: everything below is DB I/O plus a possible
        # live PokeAPI fetch on cache miss (see pokeapi.get_species_forms),
        # either of which can blow past Discord's 3-second interaction
        # response window and surface as "The application didn't respond
        # in time" even though the bot is still working. Deferring buys the
        # full 15-minute followup window instead.
        message_id = interaction.message.id if interaction.message is not None else None
        log.info("view_alt_forms: click received, message_id=%s", message_id)
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            log.exception("view_alt_forms: defer() failed for message_id=%s", message_id)
            raise

        try:
            resolved = await _resolve_species_for_message(message_id)
            if resolved is None:
                log.warning("view_alt_forms: no DailyPokemon/WildEncounter row matches message_id=%s", message_id)
                await interaction.followup.send(
                    "Couldn't find this post's Pokemon data (it may be too old).", ephemeral=True
                )
                return

            dex_no, name, shiny = resolved
            forms = await interaction.client.api.get_species_forms(dex_no)  # type: ignore[attr-defined]
            pages = _build_form_pages(dex_no, name, forms, shiny)
            if not pages:
                await interaction.followup.send("No alternate forms available for this Pokemon.", ephemeral=True)
                return

            await interaction.followup.send(embed=pages[0], view=_FormsPaginatorView(pages), ephemeral=True)
            log.info("view_alt_forms: followup sent ok for message_id=%s dex_no=%s", message_id, dex_no)
        except Exception:
            log.exception("view_alt_forms: failed after defer for message_id=%s", message_id)
            raise
