from __future__ import annotations

import json
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable

from audio_resolution import (
    AudioChooser,
    AudioDiscoveryError,
    resolve_discovered_audio,
)
from song_package import DifficultyInput, SongPackageSpec
from spc_converter import ConversionError, read_source


STUDIO_FORMAT = "InFalsusStudioSong"
STUDIO_VERSION = 1
STUDIO_MANIFEST = "infalsus.studio.json"
STUDIO_ARCHIVE_MAX_MEMBERS = 4096
STUDIO_ARCHIVE_MAX_UNCOMPRESSED = 512 * 1024 * 1024
INTERNAL_CODE = re.compile(r"^[A-Za-z0-9_-]+$")
AUTO_CODE_SOURCE = re.compile(r"^[A-Za-z0-9]+$")


@dataclass(frozen=True)
class StudioDifficulty:
    index: int
    name: str
    rating_text: str
    designer: str
    jacket_designer: str
    chart: Path | None
    background: Path | None
    note_count: int
    event_count: int

    @property
    def is_empty(self) -> bool:
        return self.chart is None or self.note_count == 0

    @property
    def rating(self) -> int:
        return rating_fields(self.rating_text)[0]

    @property
    def level_section_indicator(self) -> str:
        return rating_fields(self.rating_text)[1]


@dataclass(frozen=True)
class StudioProject:
    root: Path
    manifest: Path
    title: str
    artist: str
    audio: Path
    jacket: Path | None
    background: Path | None
    source_code_hint: str | None
    active_difficulty: int
    preview_start_seconds: float
    preview_end_seconds: float
    difficulties: tuple[StudioDifficulty, ...]

    @property
    def non_empty_difficulties(self) -> tuple[StudioDifficulty, ...]:
        return tuple(item for item in self.difficulties if not item.is_empty)

    @property
    def active(self) -> StudioDifficulty | None:
        return next(
            (
                item
                for item in self.difficulties
                if item.index == self.active_difficulty
            ),
            None,
        )

    def author_defaults(self) -> tuple[str, str]:
        ordered = []
        if self.active is not None:
            ordered.append(self.active)
        ordered.extend(item for item in self.difficulties if item is not self.active)
        chart_designer = next(
            (item.designer.strip() for item in ordered if item.designer.strip()),
            "",
        )
        jacket_designer = next(
            (
                item.jacket_designer.strip()
                for item in ordered
                if item.jacket_designer.strip()
            ),
            "",
        )
        return chart_designer, jacket_designer

    def to_song_spec(
        self,
        internal_code: str,
        output_root: Path,
        *,
        pack_folder_name: str = "User Charts",
        included_slots: Iterable[int] | None = None,
        overwrite: bool = False,
    ) -> SongPackageSpec:
        code = validate_internal_code(internal_code)
        selected = (
            self.non_empty_difficulties
            if included_slots is None
            else tuple(
                item
                for item in self.difficulties
                if item.index in set(included_slots)
            )
        )
        if not selected:
            raise ConversionError("Studio 工程没有选择任何难度")
        chart_designer, jacket_designer = self.author_defaults()
        preview_end = (
            self.preview_end_seconds
            if self.preview_end_seconds > self.preview_start_seconds
            else None
        )
        return SongPackageSpec(
            output_root=output_root,
            folder_name=code,
            chart_id=code,
            base_name=code,
            song_title=self.title,
            artist=self.artist,
            chart_designer=chart_designer,
            jacket_designer=jacket_designer,
            audio=self.audio,
            jacket_large=self.jacket,
            jacket_small=self.jacket,
            background=self.background,
            preview_start_seconds=self.preview_start_seconds,
            preview_end_seconds=preview_end,
            difficulties=[
                DifficultyInput(
                    slot=item.index,
                    chart=item.chart,
                    rating=item.rating,
                    level_section_indicator=item.level_section_indicator,
                    background=item.background,
                )
                for item in selected
            ],
            pack_folder_name=pack_folder_name,
            overwrite=overwrite,
        )


@dataclass
class StudioProjectSession:
    source: Path
    project: StudioProject
    _temporary: tempfile.TemporaryDirectory[str]

    def close(self) -> None:
        self._temporary.cleanup()


def locate_manifest(path: Path) -> Path:
    candidate = path.resolve()
    if candidate.is_file():
        if candidate.name.casefold() != STUDIO_MANIFEST.casefold():
            raise ConversionError(
                f"选择的文件不是 {STUDIO_MANIFEST}：{candidate}"
            )
        return candidate
    manifest = candidate / STUDIO_MANIFEST
    if not manifest.is_file():
        raise ConversionError(
            f"所选目录缺少 {STUDIO_MANIFEST}：{candidate}"
        )
    return manifest


def infer_internal_code(title: str) -> str:
    value = title.strip()
    return value.lower() if AUTO_CODE_SOURCE.fullmatch(value) else ""


def validate_internal_code(value: str) -> str:
    code = value.strip()
    if not code or not INTERNAL_CODE.fullmatch(code):
        raise ConversionError(
            "内部代号只能包含 ASCII 字母、数字、下划线和连字符"
        )
    return code


def rating_fields(value: Any) -> tuple[int, str]:
    text = str(value or "").strip()
    try:
        rating = int(text)
    except ValueError:
        return 0, text or "?"
    if rating < 0:
        return 0, text
    return rating, "?"


def chart_names(internal_code: str, slots: Iterable[int]) -> dict[int, str]:
    code = validate_internal_code(internal_code)
    return {slot: f"{code}{slot}.spc" for slot in slots}


def find_chart_name_conflicts(
    charts_root: Path,
    internal_code: str,
    slots: Iterable[int],
    *,
    ignore_folder: Path | None = None,
) -> list[Path]:
    root = charts_root.resolve()
    if not root.is_dir():
        return []
    expected = {
        name.casefold()
        for name in chart_names(internal_code, slots).values()
    }
    ignored = ignore_folder.resolve() if ignore_folder is not None else None
    conflicts: list[Path] = []
    for path in root.rglob("*.spc"):
        if path.name.casefold() not in expected:
            continue
        try:
            parent = path.resolve().parent
        except OSError:
            parent = path.parent
        if ignored is not None and parent == ignored:
            continue
        conflicts.append(path)
    conflicts.sort(key=lambda item: str(item).casefold())
    return conflicts


def load_studio_project(
    path: Path,
    audio_chooser: AudioChooser | None = None,
) -> StudioProject:
    manifest = locate_manifest(path)
    return _load_studio_manifest(manifest, audio_chooser)


def load_studio_source(
    path: Path,
    audio_chooser: AudioChooser | None = None,
) -> tuple[StudioProject, StudioProjectSession | None]:
    """Load a Studio folder, JSON manifest or safely extracted ZIP project."""
    candidate = path.resolve()
    if candidate.is_file() and candidate.suffix.casefold() == ".zip":
        temporary = tempfile.TemporaryDirectory(prefix="ifce-studio-")
        extraction_root = Path(temporary.name)
        try:
            _extract_studio_archive(candidate, extraction_root)
            manifests = sorted(
                (
                    item
                    for item in extraction_root.rglob("*")
                    if item.is_file()
                    and item.name.casefold()
                    == STUDIO_MANIFEST.casefold()
                ),
                key=lambda item: str(item).casefold(),
            )
            if not manifests:
                raise ConversionError(
                    f"压缩包内没有 {STUDIO_MANIFEST}：{candidate}"
                )
            if len(manifests) > 1:
                details = "\n".join(str(item) for item in manifests[:12])
                raise ConversionError(
                    "压缩包内存在多个 Studio 工程，无法自动选择：\n"
                    + details
                )
            project = _load_studio_manifest(
                manifests[0],
                audio_chooser,
            )
            return project, StudioProjectSession(
                candidate,
                project,
                temporary,
            )
        except Exception:
            temporary.cleanup()
            raise
    return load_studio_project(candidate, audio_chooser), None


def _load_studio_manifest(
    manifest: Path,
    audio_chooser: AudioChooser | None,
) -> StudioProject:
    root = manifest.parent.resolve()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ConversionError(f"无法解析 Studio 工程：{manifest}：{exc}") from exc
    if not isinstance(data, dict):
        raise ConversionError("Studio 工程根节点必须是对象")
    if data.get("format") != STUDIO_FORMAT:
        raise ConversionError(
            f"不支持的 Studio 工程格式：{data.get('format')!r}"
        )
    if data.get("version") != STUDIO_VERSION:
        raise ConversionError(
            f"不支持的 Studio 工程版本：{data.get('version')!r}"
        )

    title = _required_text(data, "title", "歌曲标题")
    artist = _optional_text(data, "artist")
    audio = _resolve_project_audio(
        root,
        _required_text(data, "audio", "歌曲音频"),
        "歌曲音频",
        audio_chooser,
    )
    jacket_text = _optional_text(data, "jacket")
    jacket = (
        _resolve_project_path(root, jacket_text, "歌曲封面")
        if jacket_text
        else None
    )
    background = _resolve_optional_project_path(
        root,
        _optional_text(data, "background"),
        "歌曲背景",
    )
    active = _bounded_int(data.get("activeDifficulty", 0), 0, 3, "activeDifficulty")
    preview_start = _finite_number(
        data.get("previewStartSeconds", 0), "previewStartSeconds"
    )
    preview_end = _finite_number(
        data.get("previewEndSeconds", 0), "previewEndSeconds"
    )

    raw_difficulties = data.get("difficulties")
    if not isinstance(raw_difficulties, list) or len(raw_difficulties) != 4:
        raise ConversionError("Studio 工程必须包含四个难度槽位")
    by_index: dict[int, StudioDifficulty] = {}
    for raw in raw_difficulties:
        if not isinstance(raw, dict):
            raise ConversionError("Studio 难度项必须是对象")
        index = _bounded_int(raw.get("index"), 0, 3, "difficulty.index")
        if index in by_index:
            raise ConversionError(f"Studio 难度槽位重复：{index}")
        working = _resolve_project_path(
            root,
            _required_text(raw, "working", f"难度 {index} 的工作谱面"),
            f"难度 {index} 的工作谱面",
            search_fallback=True,
            missing_ok=True,
        )
        notes: list[Any] = []
        events: list[Any] = []
        if working.is_file():
            try:
                source = read_source(working)
            except Exception as exc:
                raise ConversionError(
                    f"难度 {index} 工作谱面无法解析：{working}：{exc}"
                ) from exc
            notes = source.standard.get("notes", [])
            events = source.standard.get("events", [])
        by_index[index] = StudioDifficulty(
            index=index,
            name=_optional_text(raw, "name") or str(index),
            rating_text=str(raw.get("rating", "")).strip(),
            designer=_optional_text(raw, "designer"),
            jacket_designer=_optional_text(raw, "jacketDesigner"),
            chart=working,
            background=_resolve_optional_project_path(
                root,
                _optional_text(raw, "background"),
                f"难度 {index} 背景",
            ),
            note_count=len(notes),
            event_count=len(events),
        )
    if set(by_index) != {0, 1, 2, 3}:
        raise ConversionError("Studio 工程必须包含 0..3 四个唯一难度槽位")

    return StudioProject(
        root=root,
        manifest=manifest,
        title=title,
        artist=artist,
        audio=audio,
        jacket=jacket,
        background=background,
        source_code_hint=None,
        active_difficulty=active,
        preview_start_seconds=preview_start,
        preview_end_seconds=preview_end,
        difficulties=tuple(by_index[index] for index in range(4)),
    )


def _extract_studio_archive(source: Path, target_root: Path) -> None:
    members: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConversionError(f"无法读取 Studio ZIP：{source}：{exc}") from exc
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if len(members) >= STUDIO_ARCHIVE_MAX_MEMBERS:
                raise ConversionError("Studio ZIP 文件数量异常")
            normalized = info.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            if (
                not normalized
                or path.is_absolute()
                or ".." in path.parts
                or any(":" in part for part in path.parts)
            ):
                raise ConversionError(
                    f"Studio ZIP 包含非法路径：{info.filename}"
                )
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise ConversionError(
                    f"Studio ZIP 不允许符号链接：{info.filename}"
                )
            key = normalized.casefold()
            if key in members:
                raise ConversionError(
                    f"Studio ZIP 包含重复路径：{normalized}"
                )
            total_size += max(0, info.file_size)
            if total_size > STUDIO_ARCHIVE_MAX_UNCOMPRESSED:
                raise ConversionError("Studio ZIP 解压后体积超过安全上限")
            members[key] = info

        for info in members.values():
            relative = PurePosixPath(info.filename.replace("\\", "/"))
            target = target_root.joinpath(*relative.parts).resolve()
            try:
                target.relative_to(target_root.resolve())
            except ValueError as exc:
                raise ConversionError(
                    f"Studio ZIP 路径逃逸：{info.filename}"
                ) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as input_file, target.open("wb") as output:
                shutil.copyfileobj(input_file, output)


def _required_text(
    data: dict[str, Any],
    key: str,
    label: str,
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConversionError(f"Studio 工程缺少{label}：{key}")
    return value.strip()


def _optional_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _resolve_project_path(
    root: Path,
    value: str,
    label: str,
    *,
    search_fallback: bool = False,
    missing_ok: bool = False,
) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ConversionError(f"{label}必须是工程内相对路径：{value}")
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ConversionError(f"{label}不能逃逸工程目录：{value}") from exc
    if target.is_file():
        return target
    if search_fallback:
        matches = sorted(
            (
                candidate.resolve()
                for candidate in root.rglob(relative.name)
                if candidate.is_file()
            ),
            key=lambda item: str(item).casefold(),
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            details = "\n".join(str(item) for item in matches[:12])
            raise ConversionError(
                f"{label}在原路径不存在，且找到多个同名文件：\n{details}"
            )
    if missing_ok:
        return target
    raise ConversionError(f"{label}不存在：{target}")


def _resolve_project_audio(
    root: Path,
    value: str,
    label: str,
    chooser: AudioChooser | None,
) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ConversionError(f"{label}必须是工程内相对路径：{value}")
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ConversionError(f"{label}不能逃逸工程目录：{value}") from exc
    if target.is_file():
        return target
    try:
        return resolve_discovered_audio(
            root,
            value,
            label,
            chooser,
        )
    except AudioDiscoveryError as exc:
        raise ConversionError(str(exc)) from exc


def _resolve_optional_project_path(
    root: Path,
    value: str,
    label: str,
) -> Path | None:
    if not value:
        return None
    relative = Path(value)
    if relative.is_absolute():
        raise ConversionError(f"{label}必须是工程内相对路径：{value}")
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ConversionError(f"{label}不能逃逸工程目录：{value}") from exc
    if not target.is_file():
        return None
    return target


def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConversionError(f"{label} 必须是整数") from exc
    if not minimum <= number <= maximum:
        raise ConversionError(f"{label} 必须在 {minimum}..{maximum}")
    return number


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConversionError(f"{label} 必须是数值") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ConversionError(f"{label} 必须是有限数值")
    return number
