from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path

TIMESTAMP_RE = re.compile(r"\[(?P<min>\d{1,3}):(?P<sec>\d{2})(?:[.:](?P<frac>\d{1,3}))?\]")


@dataclass(frozen=True)
class LyricLine:
    timestamp: float
    text: str


@dataclass(frozen=True)
class LyricWindow:
    """A snapshot of previous / current / upcoming lines for the embed."""
    previous: LyricLine | None
    current: LyricLine | None
    upcoming: list[LyricLine]


def _fraction_to_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    if len(value) == 1:
        return int(value) / 10
    if len(value) == 2:
        return int(value) / 100
    return int(value) / 1000


def parse_lrc(path: Path) -> list[LyricLine]:
    if not path.exists():
        return []

    result: list[LyricLine] = []
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        matches = list(TIMESTAMP_RE.finditer(raw_line))
        if not matches:
            continue
        text = TIMESTAMP_RE.sub("", raw_line).strip()
        for match in matches:
            minutes = int(match.group("min"))
            seconds = int(match.group("sec"))
            fraction = _fraction_to_seconds(match.group("frac"))
            result.append(LyricLine(minutes * 60 + seconds + fraction, text or "♪"))

    result.sort(key=lambda line: line.timestamp)
    return result


def current_index(lines: list[LyricLine], elapsed: float) -> int:
    """Binary search: index of the last line whose timestamp <= elapsed, or -1."""
    if not lines:
        return -1

    lo, hi = 0, len(lines) - 1
    best = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if lines[mid].timestamp <= elapsed:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def current_line(lines: list[LyricLine], elapsed: float) -> tuple[int, LyricLine | None]:
    index = current_index(lines, elapsed)
    if index < 0:
        return -1, None
    return index, lines[index]


def lyric_window(lines: list[LyricLine], elapsed: float, upcoming_count: int = 1) -> LyricWindow:
    """Build the previous/current/upcoming snapshot used by the live embed.

    upcoming_count controls how many future lines are shown (default 1, per
    the agreed previous/current/next-1 layout).
    """
    index = current_index(lines, elapsed)
    if index < 0:
        upcoming = lines[:upcoming_count]
        return LyricWindow(previous=None, current=None, upcoming=upcoming)

    previous = lines[index - 1] if index > 0 else None
    current = lines[index]
    upcoming = lines[index + 1 : index + 1 + upcoming_count]
    return LyricWindow(previous=previous, current=current, upcoming=upcoming)


def format_lyric_block(window: LyricWindow) -> str:
    """Render the previous/current/upcoming window using the agreed markdown style.

    -# previous line   (subtext, small/gray)
    ### current line   (large heading, emphasized)
    -# upcoming line(s) (subtext, small/gray)
    """
    lines_out: list[str] = []
    if window.previous is not None:
        lines_out.append(f"-# {window.previous.text}")
    if window.current is not None:
        lines_out.append(f"### {window.current.text}")
    else:
        lines_out.append("### ♪")
    for up in window.upcoming:
        lines_out.append(f"-# {up.text}")
    return "\n".join(lines_out)
