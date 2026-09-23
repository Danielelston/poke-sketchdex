"""Built-in wild-encounter Events, auto-seeded for every guild.

Two source strategies, mirroring the weekly vote's own Event category:
three events resolve their dex pool by Pokemon type (reusing the same
PokeAPI-backed TypePool cache `weeklyvote.py` uses for the Type category),
the rest are hand-curated explicit dex-number lists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from . import weeklyvote
from .db import EventDefinition
from .pokeapi import PokeApiClient

log = logging.getLogger(__name__)

# Defined here (rather than cogs/admin.py, which used to own it) so this
# module's seeding logic can enforce the same per-guild cap without
# cogs/admin.py and default_events.py importing each other in a cycle —
# cogs/admin.py imports `seed_default_events` from here, and (via daily.py)
# already imports weeklyvote.py, which this module also imports.
MAX_EVENTS_PER_GUILD = 50


@dataclass(frozen=True)
class TypeLookupEvent:
    name: str
    flavor_text: str
    pokeapi_type: str


@dataclass(frozen=True)
class CuratedEvent:
    name: str
    flavor_text: str
    dex_numbers: list[int]


BUILTIN_TYPE_EVENTS: list[TypeLookupEvent] = [
    TypeLookupEvent("Spooky Season", "Ghost-type pool for the Halloween season.", "ghost"),
    TypeLookupEvent("Frost & Tinsel", "Ice-type pool for the winter season.", "ice"),
    TypeLookupEvent("Bug Week", "Bug-type pool celebrating the creepy-crawlies.", "bug"),
]

BUILTIN_CURATED_EVENTS: list[CuratedEvent] = [
    CuratedEvent(
        "Fossil Week",
        "Every fossil-revival Pokemon across all generations.",
        [
            138, 139, 140, 141, 142,  # Omanyte/Omastar, Kabuto/Kabutops, Aerodactyl
            345, 346, 347, 348,  # Lileep/Cradily, Anorith/Armaldo
            408, 409, 410, 411,  # Cranidos/Rampardos, Shieldon/Bastiodon
            564, 565, 566, 567,  # Tirtouga/Carracosta, Archen/Archeops
            696, 697, 698, 699,  # Tyrunt/Tyrantrum, Amaura/Aurorus
            880, 881, 882, 883,  # Dracozolt/Arctozolt/Dracovish/Arctovish
        ],
    ),
    CuratedEvent(
        "Eeveelution Week",
        "Eevee and all eight of its evolutions.",
        [133, 134, 135, 136, 196, 197, 470, 471, 700],
    ),
    CuratedEvent(
        "Flavor Town",
        "Food-shaped and food-themed Pokemon.",
        [
            213,  # Shuckle
            420, 421,  # Cherubi/Cherrim
            582, 583, 584,  # Vanillite/Vanillish/Vanilluxe
            684, 685,  # Swirlix/Slurpuff
            761, 762, 763,  # Bounsweet/Steenee/Tsareena
            840, 841, 842,  # Applin/Flapple/Appletun
            868, 869,  # Milcery/Alcremie
            1011, 1019,  # Dipplin/Hydrapple
        ],
    ),
    CuratedEvent(
        "Holiday Themes",
        "Broad winter-holiday flavor, distinct from the strictly ice-type Frost & Tinsel.",
        [
            124,  # Jynx
            220, 221, 473,  # Swinub/Piloswine/Mamoswine
            225,  # Delibird
            234,  # Stantler
            363, 364, 365,  # Spheal/Sealeo/Walrein
            459, 460,  # Snover/Abomasnow
            613, 614,  # Cubchoo/Beartic
        ],
    ),
    CuratedEvent(
        "Starter Showdown",
        "Every regional starter line across all nine generations.",
        [
            1, 2, 3, 4, 5, 6, 7, 8, 9,  # Kanto
            152, 153, 154, 155, 156, 157, 158, 159, 160,  # Johto
            252, 253, 254, 255, 256, 257, 258, 259, 260,  # Hoenn
            387, 388, 389, 390, 391, 392, 393, 394, 395,  # Sinnoh
            495, 496, 497, 498, 499, 500, 501, 502, 503,  # Unova
            650, 651, 652, 653, 654, 655, 656, 657, 658,  # Kalos
            722, 723, 724, 725, 726, 727, 728, 729, 730,  # Alola
            810, 811, 812, 813, 814, 815, 816, 817, 818,  # Galar
            906, 907, 908, 909, 910, 911, 912, 913, 914,  # Paldea
        ],
    ),
    CuratedEvent(
        "Legendary/Mythical Week",
        "A curated sample of legendaries and mythicals spanning every generation.",
        [
            150, 151,  # Mewtwo, Mew
            249, 250, 251,  # Lugia, Ho-Oh, Celebi
            382, 383, 384, 385,  # Kyogre, Groudon, Rayquaza, Jirachi
            483, 484, 487, 491, 493,  # Dialga, Palkia, Giratina, Darkrai, Arceus
            643, 644, 646,  # Reshiram, Zekrom, Kyurem
            716, 717, 718,  # Xerneas, Yveltal, Zygarde
            791, 792, 800,  # Solgaleo, Lunala, Necrozma
            888, 889, 890,  # Zacian, Zamazenta, Eternatus
            1007, 1008,  # Koraidon, Miraidon
        ],
    ),
    CuratedEvent(
        "Sweetheart Week",
        "Valentine's-coded, pink-and-love Pokemon.",
        [
            35, 36,  # Clefairy/Clefable
            39, 40,  # Jigglypuff/Wigglytuff
            113, 242,  # Chansey/Blissey
            241,  # Miltank
            300, 301,  # Skitty/Delcatty
            370,  # Luvdisc
            531,  # Audino
            594,  # Alomomola
            669, 670, 671,  # Flabebe/Floette/Florges
            730,  # Primarina
        ],
    ),
]

BUILTIN_EVENT_NAMES: set[str] = {e.name for e in BUILTIN_TYPE_EVENTS} | {e.name for e in BUILTIN_CURATED_EVENTS}


async def seed_default_events(session, api: PokeApiClient, guild_id: int) -> list[str]:
    """Create the built-in Event pool for a guild, once.

    Skips entirely if the guild already has an EventDefinition (active or
    disabled) matching any built-in name, case-insensitively — keeps repeated
    /setup runs and bot restarts idempotent, and doesn't fight an admin who
    intentionally deleted one. Returns the names actually created (empty if
    skipped). Caller is responsible for committing the session.
    """
    existing_names = (
        await session.execute(select(EventDefinition.name).where(EventDefinition.guild_id == guild_id))
    ).scalars().all()
    existing_lower = {n.lower() for n in existing_names}
    if existing_lower & {n.lower() for n in BUILTIN_EVENT_NAMES}:
        return []

    room = MAX_EVENTS_PER_GUILD - len(existing_names)
    if room <= 0:
        log.warning(
            "Guild %s: already at the %d-event cap; skipping built-in event seed entirely.",
            guild_id, MAX_EVENTS_PER_GUILD,
        )
        return []

    created: list[str] = []

    for type_event in BUILTIN_TYPE_EVENTS:
        if len(created) >= room:
            log.warning(
                "Guild %s: hit the %d-event cap while seeding built-ins; skipping %r.",
                guild_id, MAX_EVENTS_PER_GUILD, type_event.name,
            )
            continue
        dex_numbers = await weeklyvote._get_type_pool(session, api, type_event.pokeapi_type)
        session.add(
            EventDefinition(
                guild_id=guild_id,
                name=type_event.name,
                flavor_text=type_event.flavor_text,
                dex_list="\n".join(str(n) for n in dex_numbers),
                created_by=0,
            )
        )
        created.append(type_event.name)

    for curated_event in BUILTIN_CURATED_EVENTS:
        if len(created) >= room:
            log.warning(
                "Guild %s: hit the %d-event cap while seeding built-ins; skipping %r.",
                guild_id, MAX_EVENTS_PER_GUILD, curated_event.name,
            )
            continue
        session.add(
            EventDefinition(
                guild_id=guild_id,
                name=curated_event.name,
                flavor_text=curated_event.flavor_text,
                dex_list="\n".join(str(n) for n in curated_event.dex_numbers),
                created_by=0,
            )
        )
        created.append(curated_event.name)

    await session.flush()
    log.info("Guild %s: seeded %d built-in events.", guild_id, len(created))
    return created
