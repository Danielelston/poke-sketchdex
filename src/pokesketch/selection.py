"""Daily Pokemon selection: no-repeat-until-exhausted within a dex range."""

from __future__ import annotations

import logging
import random

from sqlalchemy import delete, select

from .db import UsedPokemon
from .db import session as db_session

log = logging.getLogger(__name__)


async def pick_dex_no(guild_id: int, dex_min: int, dex_max: int, mode: str) -> int:
    """Pick the next dex number for a guild.

    mode="no_repeat": choose uniformly from unused numbers in range; when the
    pool is exhausted, reset and start a fresh cycle.
    mode="random": pure random with repeats allowed.
    """
    full_pool = set(range(dex_min, dex_max + 1))
    if mode == "random":
        return random.choice(tuple(full_pool))

    async with db_session() as s:
        used_rows = (
            await s.execute(select(UsedPokemon.dex_no).where(UsedPokemon.guild_id == guild_id))
        ).scalars().all()
        used = set(used_rows)
        remaining = full_pool - used
        if not remaining:
            # Pool exhausted for the current range -> reset the cycle.
            log.info("Guild %s exhausted dex pool; resetting no-repeat cycle.", guild_id)
            await s.execute(delete(UsedPokemon).where(UsedPokemon.guild_id == guild_id))
            await s.commit()
            remaining = full_pool
        choice = random.choice(tuple(remaining))
        s.add(UsedPokemon(guild_id=guild_id, dex_no=choice))
        await s.commit()
        return choice
