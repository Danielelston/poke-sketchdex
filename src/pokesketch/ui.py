"""Reusable Discord UI components (buttons, views)."""

from __future__ import annotations

import discord


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
