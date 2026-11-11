from __future__ import annotations

from pathlib import Path
from typing import Callable


AUDIO_EXTENSIONS = frozenset({".ogg", ".wav", ".mp3"})
AudioChooser = Callable[[str, str, tuple[Path, ...]], Path | None]


class AudioDiscoveryError(RuntimeError):
    pass


def discover_audio_files(root: Path) -> tuple[Path, ...]:
    normalized_root = root.resolve()
    candidates: list[Path] = []
    for path in normalized_root.rglob("*"):
        if not path.is_file():
            continue
        if detect_audio_format(path) is None:
            continue
        candidates.append(path.resolve())
    return tuple(
        sorted(
            candidates,
            key=lambda item: item.relative_to(normalized_root)
            .as_posix()
            .casefold(),
        )
    )


def resolve_discovered_audio(
    root: Path,
    configured: str,
    label: str,
    chooser: AudioChooser | None = None,
) -> Path:
    candidates = discover_audio_files(root)
    if not candidates:
        raise AudioDiscoveryError(
            f"{label}路径不存在，且在 {root} 内没有找到有效的 OGG/WAV/MP3："
            f"{configured}"
        )
    if chooser is not None:
        selected = chooser(configured, label, candidates)
        if selected is None:
            raise AudioDiscoveryError(f"用户取消了{label}选择")
        selected = selected.resolve()
        if selected not in candidates:
            raise AudioDiscoveryError(f"{label}选择结果不在候选列表中")
        return selected
    if len(candidates) == 1:
        return candidates[0]
    details = "\n".join(str(path) for path in candidates[:20])
    raise AudioDiscoveryError(
        f"{label}路径不存在，且找到多个有效音频：\n{details}"
    )


def detect_audio_format(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(12)
    except OSError:
        return None
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "WAV"
    if header[:4] == b"OggS":
        return "OGG"
    if header[:3] == b"ID3":
        return "MP3"
    if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0:
        return "MP3"
    return None
