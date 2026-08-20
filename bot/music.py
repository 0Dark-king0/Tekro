from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

import discord
from discord.ext import tasks

from .lyrics import LyricLine, LyricWindow, lyric_window, parse_lrc
from .naming import DisplayNameStore

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}

# Discord edit rate-limit safety: never edit the "now playing" message more
# often than this, no matter how fast the lyrics change underneath.
EMBED_UPDATE_INTERVAL = 1.1  # seconds

# How many upcoming lyric lines to show below the current one.
UPCOMING_LINES = 1

# If the bot ends up alone in a voice channel, leave after this long.
AUTO_DISCONNECT_SECONDS = 120


class LoopMode(Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


@dataclass
class Track:
    path: Path
    lyrics_path: Path | None
    display_name: str
    requested_by: int | None = None

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def name(self) -> str:
        # Kept for backwards compatibility with anything expecting `.name`;
        # prefer `display_name` for anything user-facing.
        return self.display_name


@dataclass
class PlayerState:
    """Everything the live embed needs to redraw itself."""
    current: Track | None = None
    lyrics: list[LyricLine] = field(default_factory=list)
    loop_mode: LoopMode = LoopMode.OFF
    volume: float = 1.0
    queue: list[Track] = field(default_factory=list)
    is_paused: bool = False


class MusicManager:
    def __init__(self, music_dir: Path, lyrics_dir: Path, data_dir: Path):
        self.music_dir = music_dir
        self.lyrics_dir = lyrics_dir
        self.names = DisplayNameStore(data_dir)

        self.state = PlayerState()
        self.started_at: float | None = None
        self.paused_at: float = 0.0
        self.last_elapsed = 0.0

        self.lyric_message: discord.Message | None = None
        self.lyric_channel: discord.abc.Messageable | None = None
        self._last_rendered_key: tuple | None = None
        self._last_edit_at: float = 0.0

        self._voice: discord.VoiceClient | None = None
        self._lock = asyncio.Lock()
        self._on_state_changed: Callable[[], Awaitable[None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._empty_since: float | None = None

    def start_loops(self) -> None:
        """Start the background tasks. Must be called from within a running
        event loop (e.g. from Bot.setup_hook), not from __init__."""
        if not self.sync_loop.is_running():
            self.sync_loop.start()
        if not self.watchdog_loop.is_running():
            self.watchdog_loop.start()

    # ---------- wiring ----------

    def set_state_changed_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Called whenever playback state changes in a way the control
        panel embed should re-render for (new track, pause, queue change...)."""
        self._on_state_changed = callback

    def close(self) -> None:
        self.sync_loop.cancel()
        self.watchdog_loop.cancel()

    async def _notify(self) -> None:
        if self._on_state_changed:
            await self._on_state_changed()

    # ---------- library ----------

    def scan(self) -> list[Track]:
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.lyrics_dir.mkdir(parents=True, exist_ok=True)
        tracks: list[Track] = []
        for path in sorted(self.music_dir.iterdir(), key=lambda p: p.name.lower()):
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                lyric_path = self.lyrics_dir / f"{path.stem}.lrc"
                display = self.names.get(path.stem, path.stem)
                tracks.append(
                    Track(
                        path=path,
                        lyrics_path=lyric_path if lyric_path.exists() else None,
                        display_name=display,
                    )
                )
        return tracks

    def find_track(self, name: str) -> Track | None:
        wanted = name.strip().lower()
        for track in self.scan():
            if track.display_name.lower() == wanted or track.stem.lower() == wanted:
                return track
        return None

    # ---------- voice plumbing ----------

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self._voice

    def _after_play(self, error: Exception | None) -> None:
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._handle_finished(error), self._loop)

    async def _handle_finished(self, error: Exception | None) -> None:
        if error:
            print(f"Audio playback error: {error}")

        finished = self.state.current

        if self.state.loop_mode == LoopMode.TRACK and finished is not None:
            await self._start_track(finished, restart=True)
            return

        if self.state.loop_mode == LoopMode.QUEUE and finished is not None:
            self.state.queue.append(finished)

        if self.state.queue:
            next_track = self.state.queue.pop(0)
            await self._start_track(next_track, restart=True)
            return

        self.state.current = None
        self.started_at = None
        self.state.lyrics = []
        await self._notify()

    async def connect(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        if self.voice and self.voice.is_connected():
            if self.voice.channel != channel:
                await self.voice.move_to(channel)
            return self.voice
        self._voice = await channel.connect()
        return self._voice

    # ---------- playback control ----------

    async def enqueue_or_play(
        self,
        track: Track,
        channel: discord.VoiceChannel,
        text_channel: discord.abc.Messageable | None = None,
    ) -> bool:
        """Returns True if playback started immediately, False if queued."""
        async with self._lock:
            self._loop = asyncio.get_running_loop()
            await self.connect(channel)

            if text_channel is not None:
                self.lyric_channel = text_channel

            if self.state.current is None:
                await self._start_track(track, restart=True)
                return True
            else:
                self.state.queue.append(track)
                await self._notify()
                return False

    async def _start_track(self, track: Track, restart: bool) -> None:
        voice = self._voice
        if voice is None:
            return
        if voice.is_playing() or voice.is_paused():
            voice.stop()
            await asyncio.sleep(0.15)

        source = discord.FFmpegPCMAudio(str(track.path))
        source = discord.PCMVolumeTransformer(source, volume=self.state.volume)
        voice.play(source, after=self._after_play)

        self.state.current = track
        self.state.is_paused = False
        self.started_at = asyncio.get_running_loop().time()
        self.paused_at = 0.0
        self.last_elapsed = 0.0
        self.state.lyrics = parse_lrc(track.lyrics_path) if track.lyrics_path else []
        self.lyric_message = None
        self._last_rendered_key = None
        await self._notify()

    async def skip(self) -> bool:
        if not self.voice or not (self.voice.is_playing() or self.voice.is_paused()):
            return False
        # Prevent track-loop from re-queuing the same track on skip.
        if self.state.loop_mode == LoopMode.TRACK:
            self.state.loop_mode = LoopMode.OFF
        self.voice.stop()  # _after_play picks the next track
        return True

    async def stop(self) -> None:
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()
        self.state.current = None
        self.state.queue.clear()
        self.state.loop_mode = LoopMode.OFF
        self.started_at = None
        self.state.lyrics = []
        self.last_elapsed = 0.0
        await self._notify()

    async def disconnect(self) -> None:
        await self.stop()
        if self.voice and self.voice.is_connected():
            await self.voice.disconnect()
        self._voice = None
        await self._notify()

    async def pause_or_resume(self) -> bool:
        """Toggle. Returns True if now playing, False if now paused."""
        if not self.voice:
            return False
        if self.voice.is_playing():
            self._freeze_elapsed()
            self.voice.pause()
            self.state.is_paused = True
            await self._notify()
            return False
        if self.voice.is_paused():
            self.started_at = asyncio.get_running_loop().time() - self.paused_at
            self.voice.resume()
            self.state.is_paused = False
            await self._notify()
            return True
        return False

    def _freeze_elapsed(self) -> None:
        if self.started_at is not None:
            self.last_elapsed = asyncio.get_running_loop().time() - self.started_at
            self.paused_at = self.last_elapsed
            self.started_at = None

    async def set_volume(self, volume: float) -> None:
        volume = max(0.0, min(2.0, volume))
        self.state.volume = volume
        if self.voice and isinstance(self.voice.source, discord.PCMVolumeTransformer):
            self.voice.source.volume = volume
        await self._notify()

    async def cycle_loop(self) -> LoopMode:
        order = [LoopMode.OFF, LoopMode.TRACK, LoopMode.QUEUE]
        current_idx = order.index(self.state.loop_mode)
        self.state.loop_mode = order[(current_idx + 1) % len(order)]
        await self._notify()
        return self.state.loop_mode

    async def play_next_in_queue(self) -> bool:
        if not self.state.queue:
            return False
        next_track = self.state.queue.pop(0)
        await self._start_track(next_track, restart=True)
        return True

    def elapsed(self) -> float:
        if self.started_at is None:
            return self.paused_at if self.voice and self.voice.is_paused() else self.last_elapsed
        return max(0.0, asyncio.get_running_loop().time() - self.started_at)

    # ---------- lyric window for the embed ----------

    def current_lyric_window(self) -> LyricWindow:
        return lyric_window(self.state.lyrics, self.elapsed(), upcoming_count=UPCOMING_LINES)

    # ---------- background loops ----------

    @tasks.loop(seconds=0.25)
    async def sync_loop(self) -> None:
        """Checks lyric position frequently, but only asks the embed to
        redraw at most once per EMBED_UPDATE_INTERVAL seconds (coalesced),
        so we never risk hitting Discord's edit rate limit even on fast
        songs with closely-packed lyric lines."""
        if not self.state.current or not self.state.lyrics:
            return
        if self.voice and self.voice.is_paused():
            return

        window = self.current_lyric_window()
        key = (
            window.previous.text if window.previous else None,
            window.current.text if window.current else None,
            tuple(u.text for u in window.upcoming),
        )
        if key == self._last_rendered_key:
            return

        now = time.monotonic()
        if now - self._last_edit_at < EMBED_UPDATE_INTERVAL:
            return  # coalesce: wait for the next tick, keep the newest key

        self._last_rendered_key = key
        self._last_edit_at = now
        await self._notify()

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await asyncio.sleep(1)

    @tasks.loop(seconds=15)
    async def watchdog_loop(self) -> None:
        """Auto-disconnect if the bot ends up alone in its voice channel."""
        voice = self._voice
        if voice is None or not voice.is_connected():
            self._empty_since = None
            return

        channel = voice.channel
        humans = [m for m in channel.members if not m.bot]
        if humans:
            self._empty_since = None
            return

        if self._empty_since is None:
            self._empty_since = time.monotonic()
            return

        if time.monotonic() - self._empty_since >= AUTO_DISCONNECT_SECONDS:
            self._empty_since = None
            await self.disconnect()

    @watchdog_loop.before_loop
    async def before_watchdog_loop(self) -> None:
        await asyncio.sleep(1)
