"""Entry point. Loads config, builds the bot, runs it."""
from __future__ import annotations

import logging

from bb.config import Season, Settings
from bb.discord_bot.client import BBBot
from bb.logging_setup import setup_logging


def main() -> None:
    log = setup_logging()
    try:
        settings = Settings.from_env()
        season = Season.load(settings.season_config_path)
    except Exception as e:
        logging.getLogger("bb").critical("Startup config error: %s", e)
        raise

    log.info("Starting bot for %s", season.name)
    bot = BBBot(settings, season)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
