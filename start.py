from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from bot.bot import MusicBot
from web.app import WebApp


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and configure it.")
    if not os.getenv("WEB_PASSWORD", "").strip():
        raise RuntimeError("WEB_PASSWORD is missing.")

    music_dir = BASE_DIR / os.getenv("MUSIC_DIR", "library/music")
    lyrics_dir = BASE_DIR / os.getenv("LYRICS_DIR", "library/lyrics")
    data_dir = BASE_DIR / os.getenv("DATA_DIR", "data")
    bot = MusicBot(music_dir, lyrics_dir, data_dir)
    web = WebApp(bot, BASE_DIR)

    async def runner() -> None:
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "10000"))
        config = uvicorn.Config(web.app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        await asyncio.gather(bot.start(token), server.serve())

    asyncio.run(runner())


if __name__ == "__main__":
    main()
