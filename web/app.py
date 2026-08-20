from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bot.bot import MusicBot
from bot.music import AUDIO_EXTENSIONS
from bot.naming import safe_stem, unique_stem

# Max upload size (bytes) — protects the host from someone dumping a huge
# file through the panel. 50 MB comfortably fits a long MP3.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Login brute-force protection: max attempts per IP within the window.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes


class WebApp:
    def __init__(self, bot: MusicBot, base_dir: Path):
        self.bot = bot
        self.base_dir = base_dir
        self.templates = Jinja2Templates(directory=str(base_dir / "web" / "templates"))
        self.music_dir = base_dir / os.getenv("MUSIC_DIR", "library/music")
        self.lyrics_dir = base_dir / os.getenv("LYRICS_DIR", "library/lyrics")
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.lyrics_dir.mkdir(parents=True, exist_ok=True)

        # Session tokens live only in memory: {token: expiry_timestamp}.
        # The cookie carries a random token, never the real password.
        self._sessions: dict[str, float] = {}
        self._session_ttl = 86400 * 30

        # Simple in-memory rate limiter: {ip: [timestamps]}
        self._login_attempts: dict[str, list[float]] = {}

        self.app = FastAPI(title="تكرو", version="2.0.0")
        self.app.mount(
            "/static",
            StaticFiles(directory=str(base_dir / "web" / "static")),
            name="static",
        )
        self._register_routes()

    # ---------- auth helpers ----------

    def _client_ip(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _is_rate_limited(self, ip: str) -> bool:
        now = time.time()
        attempts = [t for t in self._login_attempts.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        self._login_attempts[ip] = attempts
        return len(attempts) >= LOGIN_MAX_ATTEMPTS

    def _record_failed_attempt(self, ip: str) -> None:
        self._login_attempts.setdefault(ip, []).append(time.time())

    def _issue_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + self._session_ttl
        return token

    def _session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        expiry = self._sessions.get(token)
        if expiry is None:
            return False
        if expiry < time.time():
            del self._sessions[token]
            return False
        return True

    def auth(self, request: Request) -> None:
        password = os.getenv("WEB_PASSWORD", "")
        if not password:
            raise HTTPException(500, "WEB_PASSWORD is not configured")
        token = request.cookies.get("panel_session")
        if not self._session_valid(token):
            raise HTTPException(401, "Unauthorized")

    def require_page_auth(self, request: Request) -> RedirectResponse | None:
        """Like auth(), but for full-page browser routes: instead of a raw
        401 JSON body, sends the user to the login page."""
        password = os.getenv("WEB_PASSWORD", "")
        if not password:
            raise HTTPException(500, "WEB_PASSWORD is not configured")
        token = request.cookies.get("panel_session")
        if not self._session_valid(token):
            return self._redirect("/login")
        return None

    def tracks(self):
        return self.bot.music_manager.scan()

    def voice_channels(self):
        """List of (id, name) voice channels in the locked guild, for the
        web panel's channel picker dropdown."""
        guild_id = self.bot.allowed_guild_id
        guild = self.bot.get_guild(guild_id) if guild_id else (self.bot.guilds[0] if self.bot.guilds else None)
        if guild is None:
            return []
        import discord
        return [(c.id, c.name) for c in guild.channels if isinstance(c, discord.VoiceChannel)]

    def text_channels(self):
        guild_id = self.bot.allowed_guild_id
        guild = self.bot.get_guild(guild_id) if guild_id else (self.bot.guilds[0] if self.bot.guilds else None)
        if guild is None:
            return []
        import discord
        return [(c.id, c.name) for c in guild.channels if isinstance(c, discord.TextChannel)]

    def _redirect(self, path: str = "/"):
        return RedirectResponse(path, status_code=303)

    def _resolve_track_or_404(self, name: str):
        track = self.bot.music_manager.find_track(name)
        if not track:
            raise HTTPException(404, "Track not found")
        return track

    # ---------- routes ----------

    def _register_routes(self) -> None:
        @self.app.get("/health")
        async def health():
            return {"ok": True, "bot_ready": self.bot.is_ready(), "tracks": len(self.tracks())}

        @self.app.get("/login", response_class=HTMLResponse)
        async def login_page(request: Request):
            return self.templates.TemplateResponse(request, "login.html")

        @self.app.post("/login")
        async def login(request: Request, password: Annotated[str, Form()]):
            ip = self._client_ip(request)
            if self._is_rate_limited(ip):
                return HTMLResponse(
                    "<h3>محاولات كثيرة جداً</h3><p>انتظر شوي وحاول مرة ثانية.</p>"
                    "<p><a href='/login'>رجوع</a></p>",
                    status_code=429,
                )

            expected = os.getenv("WEB_PASSWORD", "")
            if not expected or not secrets.compare_digest(password, expected):
                self._record_failed_attempt(ip)
                return HTMLResponse(
                    "<h3>كلمة السر غلط</h3><p><a href='/login'>رجوع</a></p>", status_code=401
                )

            self._login_attempts.pop(ip, None)
            token = self._issue_session()
            response = self._redirect("/")
            response.set_cookie(
                "panel_session",
                token,
                httponly=True,
                samesite="lax",
                secure=True,
                max_age=self._session_ttl,
            )
            return response

        @self.app.get("/logout")
        async def logout(request: Request):
            token = request.cookies.get("panel_session")
            if token:
                self._sessions.pop(token, None)
            response = self._redirect("/login")
            response.delete_cookie("panel_session")
            return response

        @self.app.get("/", response_class=HTMLResponse)
        async def dashboard(request: Request):
            redirect = self.require_page_auth(request)
            if redirect is not None:
                return redirect
            state = self.bot.music_manager.state
            return self.templates.TemplateResponse(
                request,
                "index.html",
                {
                    "tracks": self.tracks(),
                    "current": state.current,
                    "elapsed": self.bot.music_manager.elapsed() if state.current else 0,
                    "queue": state.queue,
                    "loop_mode": state.loop_mode.value,
                    "volume": int(state.volume * 100),
                    "is_paused": state.is_paused,
                    "voice_channels": self.voice_channels(),
                    "text_channels": self.text_channels(),
                    "default_voice_id": os.getenv("DEFAULT_VOICE_CHANNEL_ID", ""),
                    "default_text_id": os.getenv("DEFAULT_TEXT_CHANNEL_ID", ""),
                },
            )

        @self.app.post("/upload")
        async def upload(
            request: Request,
            audio: Annotated[UploadFile, File()],
            lyrics: Annotated[UploadFile | None, File()] = None,
        ):
            self.auth(request)

            original_name = audio.filename or "song"
            suffix = Path(original_name).suffix.lower()
            if suffix not in AUDIO_EXTENSIONS:
                raise HTTPException(400, "صيغة صوت غير مدعومة")

            audio_bytes = await audio.read()
            if len(audio_bytes) > MAX_UPLOAD_BYTES:
                raise HTTPException(400, f"الملف أكبر من الحد المسموح ({MAX_UPLOAD_BYTES // (1024*1024)}MB)")

            # Build a filesystem-safe stem (spaces -> underscores, unsafe
            # chars stripped) but remember the original readable name so the
            # dashboard and Discord always show what the uploader typed.
            stem = safe_stem(original_name)
            stem = unique_stem(self.music_dir, stem)

            audio_path = self.music_dir / f"{stem}{suffix}"
            audio_path.write_bytes(audio_bytes)

            display_name = Path(original_name).stem
            self.bot.music_manager.names.set(stem, display_name)

            if lyrics and lyrics.filename:
                if Path(lyrics.filename).suffix.lower() != ".lrc":
                    raise HTTPException(400, "ملف الكلمات لازم يكون .lrc")
                lyrics_bytes = await lyrics.read()
                if len(lyrics_bytes) > MAX_UPLOAD_BYTES:
                    raise HTTPException(400, "ملف الكلمات كبير جداً")
                (self.lyrics_dir / f"{stem}.lrc").write_bytes(lyrics_bytes)

            return self._redirect("/")

        @self.app.post("/delete/{name}")
        async def delete(request: Request, name: str):
            self.auth(request)
            track = self._resolve_track_or_404(name)
            if self.bot.music_manager.state.current and self.bot.music_manager.state.current.path == track.path:
                await self.bot.music_manager.stop()
            track.path.unlink(missing_ok=True)
            if track.lyrics_path:
                track.lyrics_path.unlink(missing_ok=True)
            self.bot.music_manager.names.remove(track.stem)
            return self._redirect("/")

        @self.app.post("/play/{name}")
        async def play(
            request: Request,
            name: str,
            voice_channel_id: Annotated[str, Form()] = "",
            text_channel_id: Annotated[str, Form()] = "",
        ):
            self.auth(request)
            track = self._resolve_track_or_404(name)
            voice_id = voice_channel_id.strip() or os.getenv("DEFAULT_VOICE_CHANNEL_ID", "")
            text_id = text_channel_id.strip() or os.getenv("DEFAULT_TEXT_CHANNEL_ID", "")
            if not voice_id.isdigit():
                raise HTTPException(400, "حدد Voice Channel ID")

            import discord

            channel = self.bot.get_channel(int(voice_id))
            if channel is None:
                raise HTTPException(404, "الروم الصوتي غير موجود")
            if not isinstance(channel, discord.VoiceChannel):
                raise HTTPException(400, "الـ ID المدخل مو لروم صوتي")

            text_channel = self.bot.get_channel(int(text_id)) if text_id.isdigit() else None
            await self.bot.music_manager.enqueue_or_play(track, channel, text_channel)
            return self._redirect("/")

        @self.app.post("/stop")
        async def stop(request: Request):
            self.auth(request)
            await self.bot.music_manager.stop()
            return self._redirect("/")

        @self.app.post("/skip")
        async def skip(request: Request):
            self.auth(request)
            await self.bot.music_manager.skip()
            return self._redirect("/")

        @self.app.post("/disconnect")
        async def disconnect(request: Request):
            self.auth(request)
            await self.bot.music_manager.disconnect()
            return self._redirect("/")

        @self.app.post("/pause")
        async def pause(request: Request):
            self.auth(request)
            await self.bot.music_manager.pause_or_resume()
            return self._redirect("/")

        @self.app.post("/resume")
        async def resume(request: Request):
            self.auth(request)
            await self.bot.music_manager.pause_or_resume()
            return self._redirect("/")

        @self.app.post("/volume")
        async def set_volume(request: Request, percent: Annotated[int, Form()]):
            self.auth(request)
            await self.bot.music_manager.set_volume(percent / 100)
            return self._redirect("/")

        @self.app.post("/loop")
        async def cycle_loop(request: Request):
            self.auth(request)
            await self.bot.music_manager.cycle_loop()
            return self._redirect("/")
