from __future__ import annotations

import json
import os
import re
import shutil
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audio_resolution import detect_audio_format
from config_profiles import (
    ProfileDefinition,
    load_profiles,
    profile_label_map,
    render_template,
)
from pack_library import (
    DEFAULT_PACK_FOLDER,
    DEFAULT_PACK_ID,
    DEFAULT_PACK_STYLE,
    DEFAULT_PACK_TITLE,
    PackDirectory,
    read_pack_directory,
    scan_pack_directories,
)
from showtext_validation import validate_showtext_sidecar
from spc_converter import (
    ConversionError,
    convert_file,
    detect_format,
    read_source,
)


CURRENT_PROFILE = "current_loader"
LEGACY_PROFILE = "legacy_collection"
TEMPLATE_PROFILE = "simple_example"

PROFILE_LABELS = profile_label_map(load_profiles())

DIFFICULTY_SLOTS = (
    ("0", "MIN"),
    ("1", "EVO"),
    ("2", "ULT"),
    ("3", "FBD"),
)


@dataclass
class DifficultyInput:
    slot: int
    chart: Path | None = None
    rating: int = 1
    level_section_indicator: str = "1"
    chart_designer: str = ""
    jacket_designer: str = ""
    background: Path | None = None


@dataclass
class SongPackageSpec:
    output_root: Path
    folder_name: str
    chart_id: str
    base_name: str
    song_title: str
    artist: str
    chart_designer: str
    jacket_designer: str
    audio: Path
    jacket_large: Path | None
    jacket_small: Path | None
    background: Path | None = None
    pack_folder_name: str = "User Charts"
    pack_id: str = "user-charts"
    pack_title: str = "User Charts"
    pack_artist: str = ""
    pack_description: str = ""
    style_pack_slug: str = "act-4"
    pack_sort_order: int = 0
    pack_jacket: Path | None = None
    template_song_id: int = 10
    gameplay_background: str | None = None
    character_identifier: str | None = None
    preview_start_seconds: float = 0.0
    preview_end_seconds: float | None = None
    preview_audio: Path | None = None
    preview_audio_start: float | None = None
    preview_audio_end: float | None = None
    difficulties: list[DifficultyInput] = field(default_factory=list)
    profile: str = CURRENT_PROFILE
    profile_config: Path | None = None
    overwrite: bool = False


@dataclass
class ExportResult:
    pack_folder: Path | None
    pack_config: Path | None
    folder: Path
    config: Path
    audio: Path
    jacket_large: Path
    jacket_small: Path
    charts: list[Path]
    profile: str
    warnings: list[str] = field(default_factory=list)


def _safe_stem(value: str, fallback: str = "custom_song") -> str:
    value = (value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", "_", value).strip("._")
    return value or fallback


def _safe_folder(value: str, fallback: str = "Custom Song") -> str:
    value = (value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def _unique_backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = path.with_name(f"{path.name}.backup_{stamp}")
    suffix = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.backup_{stamp}_{suffix}")
        suffix += 1
    return candidate


def _write_placeholder_png(path: Path, width: int, height: int) -> None:
    """Write a small dependency-free gradient PNG."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            # Dark magenta-to-blue gradient with a subtle grid.
            r = 36 + int(48 * (x / max(1, width - 1)))
            g = 24 + int(20 * (y / max(1, height - 1)))
            b = 74 + int(90 * (y / max(1, height - 1)))
            if x % 64 < 2 or y % 64 < 2:
                r, g, b = 105, 55, 125
            rows.extend((r, g, b))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def _copy_jacket(source: Path | None, target: Path, width: int, height: int) -> None:
    if source is None:
        _write_placeholder_png(target, width, height)
        return
    if not source.is_file():
        raise ConversionError("封面文件不存在：" + str(source))
    with source.open("rb") as stream:
        signature = stream.read(8)
    if signature == b"\x89PNG\r\n\x1a\n":
        shutil.copy2(source, target)
        return
    try:
        from PIL import Image  # type: ignore

        with Image.open(source) as image:
            image.convert("RGBA").save(target, "PNG")
    except Exception as exc:
        raise ConversionError("封面无法转换为 PNG：" + str(source)) from exc


def _copy_background(source: Path, target_dir: Path, slot: int) -> str:
    if not source.is_file():
        raise ConversionError("背景文件不存在：" + str(source))
    if source.suffix.lower() == ".mp4":
        name = f"background_{slot}.mp4"
        shutil.copy2(source, target_dir / name)
        return name
    name = f"background_{slot}.png"
    _copy_jacket(source, target_dir / name, 1920, 1080)
    return name


def _copy_song_background(source: Path, target_dir: Path) -> str:
    if not source.is_file():
        raise ConversionError("歌曲背景文件不存在：" + str(source))
    if source.suffix.lower() == ".mp4":
        name = "background.mp4"
        shutil.copy2(source, target_dir / name)
        return name
    name = "background.png"
    _copy_jacket(source, target_dir / name, 1920, 1080)
    return name


def _audio_suffix(source: Path) -> str:
    detected = detect_audio_format(source)
    if detected is not None:
        return {
            "OGG": ".ogg",
            "WAV": ".wav",
            "MP3": ".mp3",
        }[detected]
    suffix = source.suffix.lower()
    if suffix not in (".ogg", ".wav", ".mp3"):
        raise ConversionError("音频格式应为 ogg/wav/mp3：" + str(source))
    return suffix


def _song_schema_values(
    spec: SongPackageSpec,
    chart_stems: dict[int, str],
    background_names: dict[int, str | None],
    audio_name: str,
    jacket_large_name: str,
    jacket_small_name: str,
    preview_end: float,
) -> dict[str, Any]:
    difficulties: dict[str, dict[str, Any]] = {}
    for item in sorted(
        (entry for entry in spec.difficulties if entry.chart is not None),
        key=lambda entry: entry.slot,
    ):
        name = DIFFICULTY_SLOTS[item.slot][1]
        background = background_names.get(item.slot)
        difficulties[name] = {
            "chart": chart_stems[item.slot] + ".spc",
            "rating": int(item.rating),
            "levelSectionIndicator": str(
                item.level_section_indicator or "?"
            ),
            "background": background,
        }

    first_background = next(
        (
            value
            for value in background_names.values()
            if value
        ),
        None,
    )
    preview = None
    if spec.preview_audio is not None:
        preview = {
            "audio": "preview" + _audio_suffix(spec.preview_audio),
            "start": spec.preview_audio_start,
            "end": spec.preview_audio_end,
        }
    return {
        "schema_version": 1,
        "song_id": _safe_stem(spec.chart_id or spec.folder_name, "custom_song"),
        "song_title": spec.song_title,
        "artist": spec.artist,
        "chart_designer": spec.chart_designer,
        "jacket_designer": spec.jacket_designer,
        "audio_file": audio_name,
        "preview_start": float(spec.preview_start_seconds),
        "preview_end": float(preview_end),
        "preview": preview,
        "background": first_background,
        "jacket_large_file": jacket_large_name,
        "jacket_small_file": jacket_small_name,
        "difficulties": difficulties,
    }


def _pack_config(
    pack_id: str,
    title: str,
    artist: str = "",
    description: str = "",
    style_pack_slug: str = DEFAULT_PACK_STYLE,
    sort_order: int = 0,
) -> dict[str, Any]:
    if not pack_id.strip():
        raise ConversionError("曲包 id 不能为空")
    if not title.strip():
        raise ConversionError("曲包 title 不能为空")
    return {
        "schemaVersion": 1,
        "id": pack_id.strip(),
        "title": title.strip(),
        "artist": artist,
        "description": description,
        "stylePackSlug": style_pack_slug.strip() or DEFAULT_PACK_STYLE,
        "sortOrder": int(sort_order),
    }


def _audio_duration(source: Path) -> float | None:
    try:
        from mutagen import File as MutagenFile  # type: ignore

        media = MutagenFile(str(source))
        if media is not None and media.info is not None:
            value = float(media.info.length)
            if value > 0:
                return value
    except Exception:
        pass
    if source.suffix.lower() == ".wav":
        try:
            import wave

            with wave.open(str(source), "rb") as handle:
                return handle.getnframes() / float(handle.getframerate())
        except Exception:
            pass
    return None


def _profile_context(
    spec: SongPackageSpec,
    chart_stems: dict[int, str],
    background_names: dict[int, str | None],
    audio_name: str,
    jacket_large_name: str,
    jacket_small_name: str,
    preview_end: float,
) -> dict[str, Any]:
    chart_id = _safe_stem(spec.chart_id or spec.folder_name, "custom_song")
    context: dict[str, Any] = {
        "folder_name": _safe_stem(spec.folder_name or chart_id, "custom_song"),
        "base_name": _safe_stem(spec.base_name or chart_id, "custom_song"),
        "template_song_id": int(spec.template_song_id),
        "gameplay_background": spec.gameplay_background or None,
        "character_identifier": spec.character_identifier,
    }
    context.update(
        _song_schema_values(
            spec,
            chart_stems,
            background_names,
            audio_name,
            jacket_large_name,
            jacket_small_name,
            preview_end,
        )
    )
    context["chart_id"] = chart_id
    by_slot = {item.slot: item for item in spec.difficulties if item.chart is not None}
    first_present = next((by_slot[s] for s in range(4) if s in by_slot), None)
    for slot in range(4):
        item = by_slot.get(slot)
        chart_stem = chart_stems.get(slot)
        chart_file = (chart_stem + ".spc") if chart_stem else None
        background_file = background_names.get(slot)
        difficulty = None
        if item is not None:
            difficulty = {
                "externalChartId": chart_stem,
                "levelSectionIndicator": str(item.level_section_indicator),
                "rating": int(item.rating),
                "backgroundFile": background_file,
            }
            if item.chart_designer.strip():
                difficulty["chartDesigner"] = item.chart_designer
            if item.jacket_designer.strip():
                difficulty["jacketDesigner"] = item.jacket_designer
        context.update({
            f"difficulty_present_{slot}": item is not None,
            f"difficulty_{slot}": difficulty,
            f"chart_{slot}": chart_file,
            f"chart_file_{slot}": chart_file,
            f"chart_stem_{slot}": chart_stem,
            f"rating_{slot}": int(item.rating) if item else None,
            f"section_{slot}": str(item.level_section_indicator) if item else None,
            f"chart_designer_{slot}": (
                item.chart_designer or spec.chart_designer if item else None
            ),
            f"jacket_designer_{slot}": (
                item.jacket_designer or spec.jacket_designer if item else None
            ),
            f"background_{slot}": background_file,
            f"background_file_{slot}": background_file,
        })
    if first_present is not None:
        first_slot = first_present.slot
        for alias, source in (
            ("chart", f"chart_{first_slot}"),
            ("chart_file", f"chart_file_{first_slot}"),
            ("chart_stem", f"chart_stem_{first_slot}"),
            ("rating", f"rating_{first_slot}"),
            ("section", f"section_{first_slot}"),
            ("chart_designer_first", f"chart_designer_{first_slot}"),
            ("jacket_designer_first", f"jacket_designer_{first_slot}"),
            ("background_file", f"background_file_{first_slot}"),
        ):
            context[alias] = context[source]
    else:
        for alias in (
            "difficulty", "chart", "chart_file", "chart_stem", "rating", "section",
            "chart_designer_first", "jacket_designer_first",
            "background_file",
        ):
            context[alias] = None
    return context


def _config_file_name(value: str) -> str:
    name = (value or "").strip()
    if not name or Path(name).name != name or name in (".", ".."):
        raise ConversionError("配置文件名不能包含目录：" + repr(value))
    return name


def create_pack_folder(
    output_root: Path,
    folder_name: str,
    pack_id: str,
    title: str,
    *,
    artist: str = "",
    description: str = "",
    style_pack_slug: str = DEFAULT_PACK_STYLE,
    sort_order: int = 0,
    jacket: Path | None = None,
) -> PackDirectory:
    """Create one pack without exporting or modifying any song."""
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_folder(folder_name, DEFAULT_PACK_FOLDER)
    target = root / safe_name
    if target.exists():
        raise ConversionError(f"曲包文件夹已存在：{target}")

    scan = scan_pack_directories(root)
    for pack in scan.packs:
        if pack.pack_id.casefold() == pack_id.strip().casefold():
            raise ConversionError(
                f"曲包 id 已被使用：{pack.pack_id} ({pack.folder})"
            )
    for item in scan.invalid:
        if item.reason.casefold().startswith("曲包 id 重复"):
            raise ConversionError(
                f"现有曲包存在重复 id，无法安全创建：{item.folder}：{item.reason}"
            )

    config = _pack_config(
        pack_id,
        title,
        artist,
        description,
        style_pack_slug,
        sort_order,
    )
    temp = Path(tempfile.mkdtemp(prefix=".spc-pack-", dir=root))
    try:
        (temp / "pack_config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if jacket is not None:
            _copy_jacket(jacket, temp / "pack_jacket.png", 512, 512)
        os.replace(temp, target)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return read_pack_directory(target)


def ensure_default_pack(output_root: Path) -> tuple[PackDirectory, bool]:
    """Return the default pack, creating it only when it does not exist."""
    root = output_root.resolve()
    target = root / DEFAULT_PACK_FOLDER
    if target.exists():
        try:
            return read_pack_directory(target), False
        except ValueError as exc:
            raise ConversionError(
                f"默认曲包目录已存在但不可用：{target}：{exc}"
            ) from exc
    return (
        create_pack_folder(
            root,
            DEFAULT_PACK_FOLDER,
            DEFAULT_PACK_ID,
            DEFAULT_PACK_TITLE,
            style_pack_slug=DEFAULT_PACK_STYLE,
            sort_order=0,
        ),
        True,
    )


def build_config(
    spec: SongPackageSpec,
    chart_stems: dict[int, str],
    background_names: dict[int, str | None],
    audio_name: str,
    jacket_large_name: str,
    jacket_small_name: str,
    preview_end: float,
    profile: ProfileDefinition | None = None,
) -> dict[str, Any]:
    if profile is None:
        profiles = load_profiles(spec.profile_config)
        profile = profiles.get(spec.profile)
    if profile is None:
        raise ConversionError(
            "未知配置结构："
            + spec.profile
            + "。请检查程序目录下的 config_profiles.json"
        )
    context = _profile_context(
        spec, chart_stems, background_names, audio_name,
        jacket_large_name, jacket_small_name, preview_end,
    )
    return render_template(profile.template, context)


def _validate_export_spec(
    spec: SongPackageSpec,
) -> tuple[list[DifficultyInput], ProfileDefinition, list[str]]:
    selected = [d for d in spec.difficulties if d.chart is not None]
    if not selected:
        raise ConversionError("至少添加一个谱面")
    warnings: list[str] = []
    importable: list[DifficultyInput] = []
    seen_slots: set[int] = set()
    for item in selected:
        if item.slot not in range(4):
            raise ConversionError(
                f"难度槽位必须在 0..3，实际为 {item.slot}")
        if item.slot in seen_slots:
            raise ConversionError(f"难度槽位 {item.slot} 重复")
        seen_slots.add(item.slot)
        try:
            rating = int(item.rating)
        except (TypeError, ValueError) as exc:
            raise ConversionError(
                f"难度槽位 {item.slot} 的 rating 必须是整数") from exc
        if rating < 0:
            raise ConversionError(
                f"难度槽位 {item.slot} 的 rating 不能为负数")
        label = DIFFICULTY_SLOTS[item.slot][1]
        if item.chart is None or not item.chart.is_file():
            warnings.append(
                f"{label} 谱面文件不存在，按空难度跳过："
                f"{item.chart or '(未提供路径)'}"
            )
            continue
        detect_format(item.chart)
        source = read_source(item.chart)
        notes = source.standard.get("notes", [])
        if not notes:
            warnings.append(
                f"{label} 谱面 note_count=0，按空难度跳过：{item.chart}"
            )
            continue
        importable.append(item)
    if not importable:
        details = "\n".join(warnings) if warnings else "没有可导入难度"
        raise ConversionError("所有选择难度均为空，已停止导入：\n" + details)
    if not spec.audio.is_file():
        raise ConversionError("音频文件不存在：" + str(spec.audio))
    if spec.background is not None and not spec.background.is_file():
        raise ConversionError("歌曲背景文件不存在：" + str(spec.background))
    for item in importable:
        if item.background is not None and not item.background.is_file():
            raise ConversionError("背景文件不存在：" + str(item.background))

    profiles = load_profiles(spec.profile_config)
    profile = profiles.get(spec.profile)
    if profile is None:
        raise ConversionError(
            "未知配置结构："
            + spec.profile
            + "。请检查程序目录下的 config_profiles.json"
        )
    return importable, profile, warnings


def export_song_folder(spec: SongPackageSpec) -> ExportResult:
    selected, profile, warnings = _validate_export_spec(spec)
    output_root = spec.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    folder_name = _safe_folder(
        spec.folder_name or spec.chart_id,
        "Custom Song",
    )
    target = output_root / folder_name
    return _export_song(
        spec,
        selected,
        profile,
        output_root,
        target,
        None,
        warnings,
    )


def export_song_package(spec: SongPackageSpec) -> ExportResult:
    selected, profile, warnings = _validate_export_spec(spec)
    pack_folder_name = _safe_folder(
        spec.pack_folder_name or "User Charts",
        "User Charts",
    )
    folder_name = _safe_folder(
        spec.folder_name or spec.chart_id,
        "Custom Song",
    )
    output_root = spec.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    pack_folder = output_root / pack_folder_name
    if not pack_folder.is_dir():
        raise ConversionError(f"目标曲包不存在：{pack_folder}")
    try:
        read_pack_directory(pack_folder)
    except ValueError as exc:
        raise ConversionError(str(exc)) from exc
    target = pack_folder / folder_name
    return _export_song(
        spec,
        selected,
        profile,
        output_root,
        target,
        pack_folder,
        warnings,
    )


def _export_song(
    spec: SongPackageSpec,
    selected: list[DifficultyInput],
    profile: ProfileDefinition,
    output_root: Path,
    target: Path,
    pack_folder: Path | None,
    warnings: list[str],
) -> ExportResult:
    config_file = _config_file_name(profile.config_file)
    folder_name = _safe_folder(
        spec.folder_name or spec.chart_id,
        "Custom Song",
    )
    temp = Path(tempfile.mkdtemp(prefix=".spc-package-", dir=output_root))
    backup: Path | None = None
    try:
        audio_name = "audio" + _audio_suffix(spec.audio)
        shutil.copy2(spec.audio, temp / audio_name)
        if spec.preview_audio is not None:
            if not spec.preview_audio.is_file():
                raise ConversionError(
                    "预览音频不存在：" + str(spec.preview_audio)
                )
            shutil.copy2(
                spec.preview_audio,
                temp / ("preview" + _audio_suffix(spec.preview_audio)),
            )
        jacket_large_name = "jacketLarge.png"
        jacket_small_name = "jacketSmall.png"
        _copy_jacket(spec.jacket_large, temp / jacket_large_name, 512, 512)
        _copy_jacket(spec.jacket_small, temp / jacket_small_name, 256, 256)
        song_background_name = (
            _copy_song_background(spec.background, temp)
            if spec.background is not None
            else None
        )

        chart_stems: dict[int, str] = {}
        background_names: dict[int, str | None] = {}
        chart_paths: list[Path] = []
        for item in sorted(selected, key=lambda d: d.slot):
            stem = _safe_stem(f"{folder_name}{item.slot}", f"{folder_name}{item.slot}")
            stem_path = temp / f"{stem}.spc"
            convert_file(
                item.chart,
                "v1",
                output=stem_path,
                overwrite=True,
                copy_sidecars=True,
            )
            validate_showtext_sidecar(stem_path, stem_path.name)
            chart_stems[item.slot] = stem
            chart_paths.append(stem_path)
            if item.background is not None:
                background_names[item.slot] = _copy_background(
                    item.background, temp, item.slot
                )
            else:
                background_names[item.slot] = song_background_name

        duration = _audio_duration(spec.audio)
        preview_end = spec.preview_end_seconds
        if preview_end is None:
            preview_end = min(30.0, duration) if duration else 30.0
        config = build_config(
            spec,
            chart_stems,
            background_names,
            audio_name,
            jacket_large_name,
            jacket_small_name,
            max(float(preview_end), float(spec.preview_start_seconds) + 0.1),
            profile=profile,
        )
        (temp / config_file).write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        publish_source = temp
        merge_stage: Path | None = None
        if target.exists() and pack_folder is not None:
            merge_stage = Path(
                tempfile.mkdtemp(
                    prefix=".spc-merge-",
                    dir=output_root,
                )
            )
            shutil.copytree(
                target,
                merge_stage,
                dirs_exist_ok=True,
            )
            incoming_config = json.loads(
                (temp / config_file).read_text(encoding="utf-8")
            )
            old_config_path = merge_stage / config_file
            old_config = None
            if old_config_path.is_file():
                try:
                    old_config = json.loads(
                        old_config_path.read_text(encoding="utf-8")
                    )
                except Exception as exc:
                    raise ConversionError(
                        f"现有歌曲配置无法解析，未写入任何文件："
                        f"{old_config_path}：{exc}"
                    ) from exc
            merged_config = _merge_song_config(
                old_config,
                incoming_config,
                {item.slot for item in selected},
            )
            shutil.copytree(
                temp,
                merge_stage,
                dirs_exist_ok=True,
            )
            (merge_stage / config_file).write_text(
                json.dumps(
                    merged_config,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            publish_source = merge_stage

        if target.exists():
            if pack_folder is None and not spec.overwrite:
                raise ConversionError(
                    f"输出歌曲文件夹已存在：{target}（勾选覆盖后会先备份）"
                )
            backup = _unique_backup(target)
            os.replace(target, backup)
        try:
            os.replace(publish_source, target)
        except Exception as exc:
            restore_error: Exception | None = None
            if backup is not None and not target.exists():
                try:
                    os.replace(backup, target)
                except Exception as recovery_exc:
                    restore_error = recovery_exc
            backup_hint = (
                f"；备份位于 {backup}" if backup is not None else ""
            )
            if restore_error is not None:
                raise ConversionError(
                    "发布歌曲文件夹失败，且自动恢复旧目录失败"
                    + backup_hint
                    + f"；恢复错误：{restore_error}"
                ) from exc
            raise ConversionError(
                "发布歌曲文件夹失败，旧目录已恢复"
                + backup_hint
            ) from exc
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
        if merge_stage is not None:
            shutil.rmtree(merge_stage, ignore_errors=True)
            merge_stage = None

        return ExportResult(
            pack_folder=pack_folder,
            pack_config=(
                pack_folder / "pack_config.json"
                if pack_folder is not None
                else None
            ),
            folder=target,
            config=target / config_file,
            audio=target / audio_name,
            jacket_large=target / jacket_large_name,
            jacket_small=target / jacket_small_name,
            charts=[target / p.name for p in chart_paths],
            profile=spec.profile,
            warnings=warnings,
        )
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        if "merge_stage" in locals() and merge_stage is not None:
            shutil.rmtree(merge_stage, ignore_errors=True)
        raise


_DIFFICULTY_ARRAY_KEYS = {
    "background",
    "charter",
    "chart",
    "difficulty",
    "level",
    "lv",
    "rating",
}


def _merge_song_config(
    old: Any,
    incoming: Any,
    selected_slots: set[int],
) -> Any:
    """Merge one imported song without dropping unselected difficulties."""
    if not isinstance(old, dict) or not isinstance(incoming, dict):
        return incoming
    merged = dict(old)
    for key, value in incoming.items():
        previous = merged.get(key)
        if key == "difficulties" and isinstance(value, dict):
            difficulties = (
                dict(previous) if isinstance(previous, dict) else {}
            )
            for slot in sorted(selected_slots):
                name = DIFFICULTY_SLOTS[slot][1]
                if name in value:
                    difficulties[name] = value[name]
            merged[key] = difficulties
        elif (
            key in _DIFFICULTY_ARRAY_KEYS
            and isinstance(value, list)
            and len(value) == 4
            and isinstance(previous, list)
            and len(previous) == 4
        ):
            items = list(previous)
            for slot in selected_slots:
                items[slot] = value[slot]
            merged[key] = items
        elif isinstance(value, dict) and isinstance(previous, dict):
            merged[key] = _merge_song_config(
                previous,
                value,
                selected_slots,
            )
        elif (
            isinstance(value, list)
            and isinstance(previous, list)
            and value
            and previous
            and all(isinstance(item, dict) for item in value)
            and all(isinstance(item, dict) for item in previous)
        ):
            items = list(previous)
            for index, item in enumerate(value):
                if index < len(items):
                    items[index] = _merge_song_config(
                        items[index],
                        item,
                        selected_slots,
                    )
                else:
                    items.append(item)
            merged[key] = items
        else:
            merged[key] = value
    return merged
