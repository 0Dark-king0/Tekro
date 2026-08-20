from __future__ import annotations

import discord

from .lyrics import format_lyric_block
from .music import LoopMode, MusicManager

BRAND_COLOR = 0x5575F9  # مستخرج من هوية تكرو (النغمة البنفسجية/الزرقاء)

LOOP_LABELS = {
    LoopMode.OFF: "🔁 التكرار: متوقف",
    LoopMode.TRACK: "🔂 التكرار: هذي الأغنية",
    LoopMode.QUEUE: "🔁 التكرار: القائمة",
}


def _progress_bar(elapsed: float, total: float | None, width: int = 14) -> str:
    if not total or total <= 0:
        return "▬" * width
    ratio = max(0.0, min(1.0, elapsed / total))
    filled = int(ratio * width)
    filled = max(0, min(width, filled))
    bar = "▬" * filled + "🔘" + "▬" * (width - filled - 1)
    return bar[:width] if len(bar) > width else bar


def _format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def build_player_embed(manager: MusicManager) -> discord.Embed:
    state = manager.state

    if state.current is None:
        embed = discord.Embed(
            title="🎵 تكرو",
            description="ما فيه شي يشتغل حالياً.\nافتح **📂 المكتبة** واختر أغنية عشان تبدأ.",
            color=BRAND_COLOR,
        )
        if state.queue:
            queued = "\n".join(f"{i+1}. {t.display_name}" for i, t in enumerate(state.queue[:10]))
            embed.add_field(name=f"القائمة ({len(state.queue)})", value=queued, inline=False)
        return embed

    track = state.current
    elapsed = manager.elapsed()

    embed = discord.Embed(title=f"🎶 {track.display_name}", color=BRAND_COLOR)

    if state.lyrics:
        window = manager.current_lyric_window()
        embed.description = format_lyric_block(window)
    else:
        embed.description = "-# ما فيه كلمات متزامنة لهذي الأغنية"

    status_icon = "⏸️" if state.is_paused else "▶️"
    embed.add_field(
        name="الحالة",
        value=f"{status_icon} {_format_time(elapsed)}",
        inline=True,
    )
    embed.add_field(
        name="الصوت",
        value=f"🔊 {int(state.volume * 100)}%",
        inline=True,
    )
    embed.add_field(
        name="التكرار",
        value=LOOP_LABELS[state.loop_mode],
        inline=True,
    )

    if state.queue:
        queued = "\n".join(f"{i+1}. {t.display_name}" for i, t in enumerate(state.queue[:5]))
        more = f"\n…و{len(state.queue) - 5} إضافية" if len(state.queue) > 5 else ""
        embed.add_field(name=f"التالي في القائمة ({len(state.queue)})", value=queued + more, inline=False)

    embed.set_footer(text="🎧 تكرو")

    return embed


class TrackSelect(discord.ui.Select):
    def __init__(self, manager: MusicManager, on_pick):
        self.manager = manager
        self.on_pick = on_pick
        tracks = manager.scan()[:25]  # Discord select menus cap at 25 options
        options = [
            discord.SelectOption(label=t.display_name[:100], value=t.stem)
            for t in tracks
        ] or [discord.SelectOption(label="المكتبة فارغة", value="__empty__", default=True)]
        super().__init__(
            placeholder="اختر أغنية من المكتبة...",
            options=options,
            min_values=1,
            max_values=1,
            disabled=not tracks,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "__empty__":
            await interaction.response.send_message("المكتبة فارغة حالياً.", ephemeral=True)
            return
        await self.on_pick(interaction, self.values[0])


class LibraryView(discord.ui.View):
    """Ephemeral view shown when a user opens the library picker."""

    def __init__(self, manager: MusicManager, on_pick):
        super().__init__(timeout=120)
        self.add_item(TrackSelect(manager, on_pick))


class PlayerView(discord.ui.View):
    """The persistent control-panel embed with buttons."""

    def __init__(self, manager: MusicManager, controller: "PlayerController"):
        super().__init__(timeout=None)
        self.manager = manager
        self.controller = controller

    async def _require_voice(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                "❌ لازم تكون داخل روم صوتي عشان تتحكم بالمشغل.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="سابق", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_voice(interaction):
            return
        await interaction.response.defer()
        await self.controller.previous(interaction)

    @discord.ui.button(label="تشغيل/إيقاف", emoji="⏯️", style=discord.ButtonStyle.primary, row=0)
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_voice(interaction):
            return
        await interaction.response.defer()
        await self.manager.pause_or_resume()

    @discord.ui.button(label="تالي", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_voice(interaction):
            return
        await interaction.response.defer()
        await self.manager.skip()

    @discord.ui.button(label="تكرار", emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_voice(interaction):
            return
        await interaction.response.defer()
        await self.manager.cycle_loop()

    @discord.ui.button(label="الصوت", emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def volume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_voice(interaction):
            return
        await interaction.response.send_message(
            "اختر مستوى الصوت:", view=VolumeView(self.manager), ephemeral=True
        )

    @discord.ui.button(label="المكتبة", emoji="📂", style=discord.ButtonStyle.success, row=1)
    async def library_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_voice(interaction):
            return

        async def on_pick(pick_interaction: discord.Interaction, stem: str):
            await self.controller.play_by_stem(pick_interaction, stem)

        await interaction.response.send_message(
            "اختر أغنية:", view=LibraryView(self.manager, on_pick), ephemeral=True
        )

    @discord.ui.button(label="إيقاف كامل", emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_voice(interaction):
            return
        await interaction.response.defer()
        await self.manager.stop()


class VolumeView(discord.ui.View):
    def __init__(self, manager: MusicManager):
        super().__init__(timeout=60)
        self.manager = manager

    @discord.ui.select(
        placeholder="مستوى الصوت",
        options=[
            discord.SelectOption(label="25%", value="0.25"),
            discord.SelectOption(label="50%", value="0.5"),
            discord.SelectOption(label="75%", value="0.75"),
            discord.SelectOption(label="100%", value="1.0"),
            discord.SelectOption(label="150%", value="1.5"),
        ],
    )
    async def select_volume(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self.manager.set_volume(float(select.values[0]))
        await interaction.response.edit_message(content=f"✅ تم ضبط الصوت على {int(float(select.values[0])*100)}%", view=None)


class PlayerController:
    """Owns the single persistent control-panel message per guild and keeps
    it redrawn in sync with MusicManager state changes."""

    def __init__(self, manager: MusicManager, bot):
        self.manager = manager
        self.bot = bot
        self.message: discord.Message | None = None
        self.manager.set_state_changed_callback(self.refresh)

    async def send_panel(self, channel: discord.abc.Messageable) -> discord.Message:
        view = PlayerView(self.manager, self)
        embed = build_player_embed(self.manager)
        self.message = await channel.send(embed=embed, view=view)
        self.manager.lyric_channel = channel
        return self.message

    async def refresh(self) -> None:
        if self.message is None:
            return
        embed = build_player_embed(self.manager)
        try:
            await self.message.edit(embed=embed)
        except (discord.NotFound, discord.HTTPException) as exc:
            print(f"Could not update player panel: {exc}")

    async def play_by_stem(self, interaction: discord.Interaction, stem: str) -> None:
        track = None
        for t in self.manager.scan():
            if t.stem == stem:
                track = t
                break
        if track is None:
            await interaction.response.send_message("❌ الأغنية مو موجودة بالمكتبة.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message("❌ لازم تكون داخل روم صوتي أول.", ephemeral=True)
            return

        track.requested_by = member.id
        started = await self.manager.enqueue_or_play(track, member.voice.channel, self.manager.lyric_channel)
        msg = f"▶️ تشغيل **{track.display_name}**" if started else f"➕ تمت الإضافة للقائمة: **{track.display_name}**"
        await interaction.response.send_message(msg, ephemeral=True)

    async def previous(self, interaction: discord.Interaction) -> None:
        # Simple restart-current behaviour; a full history stack can be
        # added later if needed.
        await interaction.followup.send("⏮️ ما فيه دعم للرجوع للأغنية السابقة بعد — قريباً.", ephemeral=True)
