"""Centralized logging: readable console output + rotating-ish file logs."""
from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    detailed = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s:%(lineno)d | %(message)s"
    )
    concise = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    file_handler = logging.FileHandler(log_dir / "bot.log", encoding="utf-8")
    file_handler.setFormatter(detailed)
    file_handler.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(concise)
    console.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)

    # Discord library is chatty at INFO; keep it at WARNING.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return logging.getLogger("bb")
