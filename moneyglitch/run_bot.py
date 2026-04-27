"""Entry point for the aiogram control bot."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from moneyglitch.bot import run_bot


def load_config() -> dict:
    path = Path(os.environ.get("MONEYGLITCH_CONFIG", "config.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MONEYGLITCH_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_bot(load_config()))


if __name__ == "__main__":
    main()
