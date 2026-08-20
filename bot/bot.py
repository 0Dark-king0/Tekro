from __future__ import annotations

import os
from pathlib import Path

import discord
from discord.ext import commands

from .music import MusicManager
from .player_view import PlayerController


class MusicBot(commands.Bot):
    def __init__(self, music_dir: Path, lyrics_dir: Path, data_dir: Path):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.members = True  # needed to see who else is in a voice channel
        intents.message_content = True  # needed for the "!join"/"!leave" prefix commands
        super().__init__(command_prefix="!", intents=intents)

        self.music_manager = MusicManager(music_dir, lyrics_dir, data_dir)
        self.controller = PlayerController(self.music_manager, self)

        raw_guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        self.allowed_guild_id: int | None = int(raw_guild_id) if raw_guild_id.isdigit() else None

    def is_guild_allowed(self, guild: discord.Guild | None) -> bool:
        if self.allowed_guild_id is None:
            return True  # no lock configured -> allow (dev convenience)
        return guild is not None and guild.id == self.allowed_guild_id

    async def setup_hook(self) -> None:
        self.music_manager.start_loops()
        await self.add_cog(MusicCog(self, self.music_manager, self.controller))

        try:
            if self.allowed_guild_id is not None:
                guild = discord.Object(id=self.allowed_guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"Slash commands synced instantly to locked guild {self.allowed_guild_id}")
            else:
                await self.tree.sync()
                print("Global slash commands synced (no DISCORD_GUILD_ID set — can take up to 1h to propagate)")
        except discord.Forbidden:
            print(
                "WARNING: Missing Access while syncing slash commands. "
                "Make sure the bot was invited with BOTH 'bot' and 'applications.commands' "
                "scopes, and that DISCORD_GUILD_ID matches a server the bot is actually in. "
                "Continuing startup without synced slash commands (prefix commands like ! still work)."
            )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if not self.is_guild_allowed(guild):
            print(f"Leaving unauthorized guild: {guild.name} ({guild.id})")
            await guild.leave()

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Library tracks: {len(self.music_manager.scan())}")

        if self.allowed_guild_id is not None:
            for guild in self.guilds:
                if guild.id != self.allowed_guild_id:
                    print(f"Leaving unauthorized guild found at startup: {guild.name} ({guild.id})")
                    await guild.leave()

    async def close(self) -> None:
        self.music_manager.close()
        await super().close()


def guild_locked():
    """Command check: refuse to run in any guild other than the configured one."""

    async def predicate(ctx: commands.Context) -> bool:
        bot: MusicBot = ctx.bot  # type: ignore[assignment]
        if not bot.is_guild_allowed(ctx.guild):
            await ctx.reply("❌ هذا البوت مو مصرح له يشتغل بهذا السيرفر.")
            return False
        return True

    return commands.check(predicate)


class MusicCog(commands.Cog):
    def __init__(self, bot: MusicBot, manager: MusicManager, controller: PlayerController):
        self.bot = bot
        self.manager = manager
        self.controller = controller

    async def cog_check(self, ctx: commands.Context) -> bool:
        if not self.bot.is_guild_allowed(ctx.guild):
            await ctx.reply("❌ هذا البوت مو مصرح له يشتغل بهذا السيرفر.")
            return False
        return True

    @commands.hybrid_command(name="play", description="شغّل أغنية من المكتبة")
    async def play(self, ctx: commands.Context, *, song: str):
        track = self.manager.find_track(song)
        if not track:
            await ctx.reply("❌ الأغنية مو موجودة بالمكتبة. جرب `/library` عشان تشوف القائمة.")
            return
        if not isinstance(ctx.author, discord.Member) or not ctx.author.voice or not isinstance(ctx.author.voice.channel, discord.VoiceChannel):
            await ctx.reply("❌ لازم تدخل روم صوتي أول. تقدر تستخدم `!join`.")
            return

        track.requested_by = ctx.author.id
        started = await self.manager.enqueue_or_play(track, ctx.author.voice.channel, ctx.channel)

        if started:
            await ctx.reply(f"▶️ تشغيل **{track.display_name}**")
            if self.controller.message is None:
                await self.controller.send_panel(ctx.channel)
        else:
            await ctx.reply(f"➕ تمت الإضافة للقائمة: **{track.display_name}**")

    @commands.hybrid_command(name="panel", description="افتح لوحة تحكم المشغل التفاعلية")
    async def panel(self, ctx: commands.Context):
        await self.controller.send_panel(ctx.channel)
        if isinstance(ctx.interaction, discord.Interaction):
            pass  # send_panel already posted the visible panel
        else:
            await ctx.message.delete(delay=0)

    @commands.hybrid_command(name="library", aliases=["list"], description="اعرض كل الأغاني بالمكتبة")
    async def library(self, ctx: commands.Context):
        tracks = self.manager.scan()
        if not tracks:
            await ctx.reply("📂 المكتبة فارغة حالياً.")
            return
        lines = []
        for i, t in enumerate(tracks, start=1):
            has_lyrics = "🎤" if t.lyrics_path else "  "
            lines.append(f"{i}. {has_lyrics} {t.display_name}")
        # Discord messages cap at ~2000 chars; chunk if the library is large.
        chunk: list[str] = []
        length = 0
        chunks: list[str] = []
        for line in lines:
            if length + len(line) + 1 > 1800:
                chunks.append("\n".join(chunk))
                chunk, length = [], 0
            chunk.append(line)
            length += len(line) + 1
        if chunk:
            chunks.append("\n".join(chunk))

        header = f"📂 **مكتبة الأغاني ({len(tracks)})**\n"
        await ctx.reply(header + chunks[0])
        for extra in chunks[1:]:
            await ctx.send(extra)

    @commands.hybrid_command(name="stop", description="أوقف التشغيل وامسح القائمة")
    async def stop(self, ctx: commands.Context):
        await self.manager.stop()
        await ctx.reply("⏹️ تم الإيقاف ومسح القائمة.")

    @commands.hybrid_command(name="skip", description="انتقل للأغنية التالية")
    async def skip(self, ctx: commands.Context):
        if await self.manager.skip():
            await ctx.reply("⏭️ تم التخطي.")
        else:
            await ctx.reply("❌ ما فيه شي يشتغل.")

    @commands.hybrid_command(name="volume", description="اضبط مستوى الصوت (0-200)")
    async def volume(self, ctx: commands.Context, percent: int):
        await self.manager.set_volume(percent / 100)
        await ctx.reply(f"🔊 تم ضبط الصوت على {percent}%")

    @commands.hybrid_command(name="loop", description="بدّل وضع التكرار (متوقف / أغنية / قائمة)")
    async def loop(self, ctx: commands.Context):
        mode = await self.manager.cycle_loop()
        labels = {"off": "متوقف", "track": "الأغنية الحالية", "queue": "القائمة كاملة"}
        await ctx.reply(f"🔁 وضع التكرار الآن: **{labels[mode.value]}**")

    @commands.hybrid_command(name="nowplaying", description="اعرض الأغنية الحالية")
    async def nowplaying(self, ctx: commands.Context):
        if not self.manager.state.current:
            await ctx.reply("🎵 ما فيه شي يشتغل.")
            return
        await ctx.reply(f"🎵 **{self.manager.state.current.display_name}** — {self.manager.elapsed():.1f}s")

    @commands.command(name="join")
    async def join(self, ctx: commands.Context):
        if not self.bot.is_guild_allowed(ctx.guild):
            await ctx.reply("❌ هذا البوت مو مصرح له يشتغل بهذا السيرفر.")
            return
        if not isinstance(ctx.author, discord.Member) or not ctx.author.voice or not isinstance(ctx.author.voice.channel, discord.VoiceChannel):
            await ctx.reply("❌ لازم تكون داخل روم صوتي أول.")
            return
        await self.manager.connect(ctx.author.voice.channel)
        self.manager.lyric_channel = ctx.channel
        await ctx.reply(f"✅ دخلت **{ctx.author.voice.channel.name}**")

    @commands.command(name="leave")
    async def leave(self, ctx: commands.Context):
        if not self.bot.is_guild_allowed(ctx.guild):
            await ctx.reply("❌ هذا البوت مو مصرح له يشتغل بهذا السيرفر.")
            return
        await self.manager.disconnect()
        await ctx.reply("👋 طلعت من الروم الصوتي.")
