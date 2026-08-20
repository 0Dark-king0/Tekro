from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

# Characters that are unsafe in filenames across common filesystems.
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")


def safe_stem(original_name: str, fallback: str = "track") -> str:
    """Turn a user-supplied filename stem into a filesystem-safe stem.

    - Spaces become underscores.
    - Path separators and other unsafe characters are stripped.
    - '..' segments are neutralised so this can never escape the target dir.
    - Unicode (Arabic, etc.) is preserved; only structurally unsafe bits go.
    """
    stem = Path(original_name).stem
    stem = unicodedata.normalize("NFC", stem)
    stem = _UNSAFE_CHARS.sub("", stem)
    stem = stem.replace("..", "_")
    stem = _WHITESPACE.sub("_", stem).strip("_. ")
    return stem or fallback


def unique_stem(base_dir: Path, stem: str) -> str:
    """Avoid collisions: 'song', 'song_2', 'song_3', ..."""
    candidate = stem
    counter = 2
    existing = {p.stem.lower() for p in base_dir.glob("*") if p.is_file()}
    while candidate.lower() in existing:
        candidate = f"{stem}_{counter}"
        counter += 1
    return candidate


class DisplayNameStore:
    """Maps safe on-disk stems to the original, human-readable name the
    uploader used (e.g. 'اغنية_حماسية' -> 'اغنية حماسية.mp3').

    Stored as a single JSON file so it costs one read/write, not one file
    per track.
    """

    def __init__(self, data_dir: Path):
        self.path = data_dir / "display_names.json"
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            if self.path.exists():
                try:
                    self._cache = json.loads(self.path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    self._cache = {}
            else:
                self._cache = {}
        return self._cache

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._load(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def set(self, stem: str, display_name: str) -> None:
        data = self._load()
        data[stem] = display_name
        self._save()

    def get(self, stem: str, fallback: str) -> str:
        return self._load().get(stem, fallback)

    def remove(self, stem: str) -> None:
        data = self._load()
        if stem in data:
            del data[stem]
            self._save()
