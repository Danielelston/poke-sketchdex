"""Shared display-name formatting, used by daily.py (via pokeapi.py), embeds.py
(via pokeapi.py), cogs/submissions.py, and cogs/collection.py."""

from __future__ import annotations


def species_display_name(name: str) -> str:
    """Title-cased, hyphens to spaces, e.g. 'mr-mime' -> 'Mr Mime'."""
    return name.replace("-", " ").title()
