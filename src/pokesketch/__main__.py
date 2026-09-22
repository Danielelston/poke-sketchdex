"""Entry point: python -m pokesketch."""

from __future__ import annotations

import logging

from .bot import PokeSketchDexBot
from .config import Config


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    bot = PokeSketchDexBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
