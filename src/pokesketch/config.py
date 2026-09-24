"""Runtime configuration loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _split_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


@dataclass(frozen=True)
class Config:
    discord_token: str
    dev_guild_ids: list[int] = field(default_factory=list)
    db_path: str = "data/pokesketch.db"
    image_cache_dir: str = "data/images_cache"
    party_cache_dir: str = "data/party_cache"
    badge_cache_dir: str = "data/badge_cache"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Config:
        token = os.getenv("POKESKETCH_DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "POKESKETCH_DISCORD_TOKEN is not set. Copy .env.example to .env "
                "and fill in the bot token."
            )
        return cls(
            discord_token=token,
            dev_guild_ids=_split_ids(os.getenv("POKESKETCH_DEV_GUILD_IDS")),
            db_path=os.getenv("POKESKETCH_DB_PATH", "data/pokesketch.db").strip(),
            image_cache_dir=os.getenv(
                "POKESKETCH_IMAGE_CACHE_DIR", "data/images_cache"
            ).strip(),
            party_cache_dir=os.getenv(
                "POKESKETCH_PARTY_CACHE_DIR", "data/party_cache"
            ).strip(),
            badge_cache_dir=os.getenv(
                "POKESKETCH_BADGE_CACHE_DIR", "data/badge_cache"
            ).strip(),
            log_level=os.getenv("POKESKETCH_LOG_LEVEL", "INFO").strip().upper(),
        )
