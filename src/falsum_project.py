from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from audio_resolution import (
    AudioChooser,
    AudioDiscoveryError,
    resolve_discovered_audio,
)
from song_package import ConversionError
from spc_converter import read_source
from studio_project import (
    StudioDifficulty,
    StudioProject,
    _bounded_int,
)


FALSUM_FORMAT = "falsum"
FALSUM_VERSION = 1
MAX_FALSUM_MEMBERS = 4096
MAX_FALSUM_UNCOMPRESSED = 512 * 1024 * 1024


@dataclass
class FalsumSession:
    source: Path
    project: StudioProject
    _temporary: tempfile.TemporaryDirectory[str]

    def close(self) -> None:
        self._temporary.cleanup()


def locate_falsum(path: Path) -> Path:
    candidate = path.resolve()
    if candidate.is_file():
        if candidate.suffix.casefold() != ".falsum":
            raise ConversionError(f"选择的文件不是 .falsum：{candidate}")
        return candidate
    if not candidate.is_dir():
        raise ConversionError(f"Falsum 路径不存在：{candidate}")
    files = sorted(
        (
            item
            for item in candidate.iterdir()
            if item.is_file() and item.suffix.casefold() == ".falsum"
        ),
        key=lambda item: item.name.casefold(),
    )
    if len(files) != 1:
        raise ConversionError(
            f"所选文件夹必须且只能包含一个 .falsum，实际为 {len(files)} 个"
        )
    return files[0].resolve()


def load_falsum_project(
    path: Path,
    audio_chooser: AudioChooser | None = None,
) -> FalsumSession:
    source = locate_falsum(path)
    temporary = tempfile.TemporaryDirectory(prefix="ifce-falsum-")
    extract_root = Path(temporary.name)
    try:
        with zipfile.ZipFile(source) as archive:
            members = _validated_members(archive)
            _extract_members(archive, members, extract_root)
        song_member = _find_member(members, "song.json")
        if song_member is None:
            raise ConversionError("Falsum 包缺少 song.json")
        song_path = extract_root.joinpath(*PurePosixPath(song_member).parts)
        data = json.loads(song_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ConversionError("Falsum song.json 根节点必须是对象")
        if data.get("format") != FALSUM_FORMAT:
            raise ConversionError(
                f"不支持的 Falsum 格式：{data.get('format')!r}"
            )
        if data.get("version") != FALSUM_VERSION:
            raise ConversionError(
                f"不支持的 Falsum 版本：{data.get('version')!r}"
            )

        title = _required_text(data, "title", "歌曲标题")
        artist = _text(data, "artist")
        song_id = _required_text(data, "songId", "歌曲内部 ID")
        audio_name = _nested_text(
            data,
            "audio",
            "file",
            required=True,
        )
        audio_member = _find_member(members, audio_name)
        if audio_member is not None:
            audio = extract_root.joinpath(
                *PurePosixPath(audio_member).parts
            )
        else:
            try:
                audio = resolve_discovered_audio(
                    extract_root,
                    audio_name,
                    "歌曲音频",
                    audio_chooser,
                )
            except AudioDiscoveryError as exc:
                raise ConversionError(str(exc)) from exc
        cover = _member_path(
            extract_root,
            members,
            _nested_text(data, "cover", "file", required=False),
            "歌曲封面",
            required=False,
        )
        background = _resolve_background(
            extract_root,
            members,
            _text(data, "background"),
        )

        raw_difficulties = data.get("difficulties")
        if not isinstance(raw_difficulties, list):
            raise ConversionError("Falsum song.json 缺少 difficulties")
        by_index: dict[int, StudioDifficulty] = {}
        for raw in raw_difficulties:
            if not isinstance(raw, dict):
                raise ConversionError("Falsum 难度项必须是对象")
            index = _bounded_int(raw.get("index"), 0, 3, "difficulty.index")
            if index in by_index:
                raise ConversionError(f"Falsum 难度槽位重复：{index}")
            available = bool(raw.get("available", False))
            chart_text = _text(raw, "chart")
            chart = None
            note_count = 0
            event_count = 0
            if available:
                chart = _member_path(
                    extract_root,
                    members,
                    chart_text,
                    f"难度 {index} 谱面",
                )
                try:
                    source_chart = read_source(chart)
                except Exception as exc:
                    raise ConversionError(
                        f"难度 {index} 谱面无法解析：{chart}：{exc}"
                    ) from exc
                note_count = len(source_chart.standard.get("notes", []))
                event_count = len(source_chart.standard.get("events", []))
            by_index[index] = StudioDifficulty(
                index=index,
                name=_text(raw, "name") or str(index),
                rating_text=str(raw.get("rating", "")).strip(),
                designer=_text(raw, "chartDesigner"),
                jacket_designer=_text(raw, "jacketDesigner"),
                chart=chart,
                background=None,
                note_count=note_count,
                event_count=event_count,
            )
        if not by_index:
            raise ConversionError("Falsum 包没有难度定义")
        for index in range(4):
            by_index.setdefault(
                index,
                StudioDifficulty(
                    index=index,
                    name=("MIN", "EVO", "ULT", "FBD")[index],
                    rating_text="?",
                    designer="",
                    jacket_designer="",
                    chart=None,
                    background=None,
                    note_count=0,
                    event_count=0,
                ),
            )
        active = next(
            (
                index
                for index in range(4)
                if not by_index[index].is_empty
            ),
            0,
        )
        project = StudioProject(
            root=extract_root,
            manifest=source,
            title=title,
            artist=artist,
            audio=audio,
            jacket=cover,
            background=background,
            source_code_hint=song_id,
            active_difficulty=active,
            preview_start_seconds=0.0,
            preview_end_seconds=0.0,
            difficulties=tuple(by_index[index] for index in range(4)),
        )
        return FalsumSession(source, project, temporary)
    except Exception:
        temporary.cleanup()
        raise


def _validated_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        if len(result) >= MAX_FALSUM_MEMBERS:
            raise ConversionError("Falsum 包文件数量异常")
        normalized = info.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or ":" in path.parts[0]
        ):
            raise ConversionError(f"Falsum 包包含非法路径：{info.filename}")
        key = normalized.casefold()
        if key in result:
            raise ConversionError(f"Falsum 包包含重复路径：{normalized}")
        total += max(0, info.file_size)
        if total > MAX_FALSUM_UNCOMPRESSED:
            raise ConversionError("Falsum 包解压后体积超过安全上限")
        result[key] = info
    return result


def _extract_members(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    root: Path,
) -> None:
    for info in members.values():
        relative = PurePosixPath(info.filename.replace("\\", "/"))
        target = root.joinpath(*relative.parts).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError as exc:
            raise ConversionError(
                f"Falsum 包路径逃逸：{info.filename}"
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)


def _find_member(
    members: dict[str, zipfile.ZipInfo],
    value: str,
) -> str | None:
    normalized = value.replace("\\", "/").lstrip("/")
    info = members.get(normalized.casefold())
    return None if info is None else info.filename


def _member_path(
    root: Path,
    members: dict[str, zipfile.ZipInfo],
    value: str,
    label: str,
    *,
    required: bool = True,
) -> Path | None:
    actual = _find_member(members, value)
    if actual is None:
        if required:
            raise ConversionError(f"Falsum 包缺少{label}：{value}")
        return None
    return root.joinpath(*PurePosixPath(actual).parts)


def _resolve_background(
    root: Path,
    members: dict[str, zipfile.ZipInfo],
    value: str,
) -> Path | None:
    if not value:
        return None
    exact = _find_member(members, value)
    if exact is not None:
        return root.joinpath(*PurePosixPath(exact).parts)
    basename = PurePosixPath(value.replace("\\", "/")).name.casefold()
    matches = [
        info.filename
        for info in members.values()
        if PurePosixPath(info.filename.replace("\\", "/")).name.casefold()
        == basename
    ]
    if len(matches) == 1:
        return root.joinpath(*PurePosixPath(matches[0]).parts)
    return None


def _required_text(data: dict[str, Any], key: str, label: str) -> str:
    value = _text(data, key)
    if not value:
        raise ConversionError(f"Falsum 包缺少{label}：{key}")
    return value


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _nested_text(
    data: dict[str, Any],
    key: str,
    child: str,
    *,
    required: bool,
) -> str:
    value = data.get(key)
    nested = value.get(child) if isinstance(value, dict) else None
    text = nested.strip() if isinstance(nested, str) else ""
    if required and not text:
        raise ConversionError(f"Falsum 包缺少 {key}.{child}")
    return text
