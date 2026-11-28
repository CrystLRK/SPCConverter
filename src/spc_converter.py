from __future__ import annotations

from ascii_log import print_log

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from chart_packer import pack_chart  # noqa: E402
from chart_validation import (  # noqa: E402
    ChartValidationError,
    validate_standard_chart,
)
from icp1_codec import decode as raw_decode  # noqa: E402
from icp1_codec import encode as raw_encode  # noqa: E402
from spc_json_conv import raw_to_standard, serialize_v0  # noqa: E402
from spc_semantic import semantic_digest, serialize_v0_semantic  # noqa: E402
from spc_v0_chart_decoder import read_spc_v0_chart  # noqa: E402
from showtext_validation import validate_showtext_standard, validate_showtext_model  # noqa: E402
from scene_program import (  # noqa: E402
    SceneError, attach_bound_program, canonical_bytes, compile_program, editable_source,
    is_marker as is_scene_marker, load_catalog as load_scene_catalog,
    read_json as read_scene_json, sidecar_path as scene_sidecar_path,
    source_from_program, validate_chart_scene, verify_program,
)
from lyric_statement import (  # noqa: E402
    build_showtext_styles,
)


VERSION = "1.3.0"
FormatName = Literal["v0", "v1", "json"]
TargetFormat = Literal["auto", "v0", "v1", "json"]

FORMAT_NAMES: dict[str, str] = {
    "v0": "SPC V0 明文",
    "v1": "SPC V1 / ICP1 二进制",
    "json": "标准 JSON",
    "unknown": "未知格式",
}

KEY_COMMENT_RE = re.compile(
    r"^\s*(?:#|//)\s*(?:spc|icp1)(?:\s+v1)?\s+key\s*[:=]\s*(\S+)\s*$",
    re.IGNORECASE,
)


class ConversionError(RuntimeError):
    pass


@dataclass
class SourceChart:
    path: Path
    kind: FormatName
    standard: dict
    raw: dict | None = None
    embedded_key: str | None = None


@dataclass
class ConvertResult:
    source: Path
    output: Path
    source_format: str
    target_format: str
    source_key: str | None
    target_key: str | None
    notes: int
    events: int
    warnings: list[str] = field(default_factory=list)


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def default_output_dir() -> Path:
    return executable_dir() / "output"


def detect_format(path: Path) -> FormatName:
    path = path.resolve()
    if not path.is_file():
        raise ConversionError(f"文件不存在：{path}")
    head = path.read_bytes()[:4096]
    if head.startswith(b"ICP1"):
        return "v1"
    if path.suffix.lower() == ".json":
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise ConversionError(f"JSON 无法读取：{path} ({exc})") from exc
        if isinstance(obj, dict) and isinstance(obj.get("notes"), list):
            return "json"
        raise ConversionError(f"不是 SPC 标准 JSON：{path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gb18030")
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        line = line.split("//", 1)[0].strip()
        if line.startswith("chart("):
            return "v0"
        break
    raise ConversionError(f"无法识别 SPC 格式：{path}")


def extract_v0_key(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gb18030")
    for line in text.splitlines()[:80]:
        match = KEY_COMMENT_RE.match(line)
        if match:
            return match.group(1)
    return None


def read_source(path: Path, source_key: str | None = None) -> SourceChart:
    path = path.resolve()
    kind = detect_format(path)
    if kind == "v1":
        key = source_key or path.name
        payload = path.read_bytes()
        raw = raw_decode(payload, key)
        standard = raw_to_standard(raw)
        attach_bound_program(standard, path, payload, key)
        return SourceChart(path, kind, standard, raw, key)
    if kind == "json":
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        embedded = obj.get("sourceKey") or obj.get("key")
        return SourceChart(path, kind, normalize_standard_json(obj), None,
                           str(embedded) if embedded else None)
    embedded = extract_v0_key(path)
    return SourceChart(path, kind, read_spc_v0_chart(str(path)), None, embedded)


def normalize_standard_json(obj: dict) -> dict:
    if not isinstance(obj.get("notes"), list):
        raise ConversionError("标准 JSON 缺少 notes 数组")
    if not isinstance(obj.get("events", []), list):
        raise ConversionError("标准 JSON 的 events 必须是数组")
    result = dict(obj)
    result.setdefault("format", "ICP1")
    result.setdefault("version", 1)
    result.setdefault("bpm", 0.0)
    result.setdefault("meter", 4.0)
    result.setdefault("events", [])
    try:
        validate_standard_chart(result)
    except ChartValidationError as exc:
        raise ConversionError(f"标准 JSON 输入域校验失败：{exc}") from exc
    return result


def choose_target_format(source_format: FormatName, requested: TargetFormat) -> FormatName:
    if requested != "auto":
        return requested
    return "v0" if source_format == "v1" else "v1"


def choose_target_key(
    output: Path,
    explicit_key: str | None,
    embedded_key: str | None,
) -> str:
    if explicit_key:
        return explicit_key
    if embedded_key and output.name == embedded_key:
        return embedded_key
    return output.name


def target_path_for(source: Path, output_dir: Path, target_format: FormatName) -> Path:
    return output_dir / (source.stem + (".json" if target_format == "json" else ".spc"))


def _serialize_v0_with_key(obj: dict, key: str | None) -> str:
    body = serialize_v0(obj)
    if not key:
        return body
    return (
        f"# SPC key: {key}\n"
        "# Re-encoding to SPC V1 will reuse this key automatically.\n"
        + body
    )


def _v0_roundtrip_check(
        text: str, expected_notes: int, expected_events: int,
        expected_standard: dict | None = None) -> dict:
    temp_dir = Path(tempfile.mkdtemp(prefix="spc-v0-check-"))
    try:
        path = temp_dir / "check.spc"
        path.write_text(text, encoding="utf-8")
        # This check validates the source representation without applying any
        # gameplay-only expansion.
        scene = expected_standard.get("scene") if expected_standard else None
        manifest = (editable_source(scene) if scene and scene["kind"] == "source"
                    else source_from_program(scene) if scene else None)
        obj = read_spc_v0_chart(str(path), scene_manifest=manifest)
        if expected_standard is not None and (
                semantic_digest(obj) != semantic_digest(expected_standard)):
            raise ConversionError("转成 V0 后游戏语义摘要不一致")
        if len(obj["notes"]) != expected_notes or len(obj["events"]) != expected_events:
            if expected_standard is not None:
                return obj
            raise ConversionError(
                "转成 V0 后数量不一致，可能包含 V0 无法完整表达的扩展语句；"
                "请改用 JSON 保留全部字段。"
            )
        return obj
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _v1_roundtrip_check(
        data: bytes, key: str, expected_notes: int, expected_events: int,
        expected_standard: dict | None = None) -> None:
    raw = raw_decode(data, key)
    if expected_standard is not None:
        actual = raw_to_standard(raw)
        if "scene" in expected_standard:
            actual["scene"] = expected_standard["scene"]
        if semantic_digest(actual) != semantic_digest(expected_standard):
            raise ConversionError("生成的 V1 游戏语义摘要不一致")
    if len(raw["notes"]) != expected_notes or len(raw["events"]) != expected_events:
        raise ConversionError("新生成 SPC V1 的数量校验失败")


def _find_sidecar(source: Path) -> Path | None:
    candidates = [
        source.with_suffix(".text.json"),
        source.with_name(source.name + ".text.json"),
    ]
    for sidecar in candidates:
        if not sidecar.is_file():
            continue
        return sidecar
    return None


def _publish_outputs(
    output: Path,
    chart_data: bytes,
    overwrite: bool,
    sidecar_source: Path | None,
    text_data: bytes | None,
    scene_data: bytes | None = None,
    screen_files: list | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    sidecar_target = (
        output.with_name(output.stem + ".text.json")
        if text_data is not None or sidecar_source is not None
        else None
    )
    if (
        sidecar_target is not None
        and sidecar_target.exists()
        and not overwrite
        and (
            sidecar_source is None
            or sidecar_target.resolve() != sidecar_source.resolve()
        )
    ):
        raise ConversionError(
            f"showtext 输出文件已存在：{sidecar_target}"
            "（勾选“覆盖已有文件”后可继续）"
        )

    staging = Path(tempfile.mkdtemp(
        prefix=f".{output.stem}-publish-",
        dir=output.parent,
    ))
    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    rollback_incomplete = False
    try:
        staged_chart = staging / output.name
        staged_chart.write_bytes(chart_data)
        publish: list[tuple[Path, Path | None]] = [
            (output, staged_chart)
        ]
        if sidecar_target is not None and text_data is not None:
            staged_sidecar = staging / sidecar_target.name
            staged_sidecar.write_bytes(text_data)
            publish.append((sidecar_target, staged_sidecar))
        elif (
            sidecar_target is not None
            and sidecar_source is not None
            and sidecar_target.resolve() != sidecar_source.resolve()
        ):
            staged_sidecar = staging / sidecar_target.name
            shutil.copy2(sidecar_source, staged_sidecar)
            publish.append((sidecar_target, staged_sidecar))

        if scene_data is not None:
            scene_target = scene_sidecar_path(output)
            staged_scene = staging / scene_target.name
            staged_scene.write_bytes(scene_data)
            publish.append((scene_target, staged_scene))

        for relative, data in screen_files or []:
            target = output.parent / relative
            from screen_resources import _publication_target
            target = _publication_target(output, relative)
            if data is None:
                publish.append((target, None))
            else:
                staged = staging / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(data)
                publish.append((target, staged))

        for target, _ in publish:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                continue
            if not overwrite and target != output:
                raise ConversionError(
                    f"输出文件已存在：{target}"
                    "（勾选“覆盖已有文件”后可继续）"
                )
            fd, backup_name = tempfile.mkstemp(
                prefix=f".{target.name}.backup-",
                dir=target.parent,
            )
            os.close(fd)
            os.unlink(backup_name)
            backup = Path(backup_name)
            os.replace(target, backup)
            backups[target] = backup

        for target, staged in publish:
            if staged is not None:
                os.replace(staged, target)
                installed.append(target)
    except Exception as exc:
        rollback_errors: list[str] = []
        for target in reversed(installed):
            try:
                if target.exists():
                    target.unlink()
            except Exception as rollback_exc:
                rollback_errors.append(
                    f"删除新文件 {target} 失败：{rollback_exc}"
                )
        for target, backup in backups.items():
            try:
                if target.exists():
                    target.unlink()
                os.replace(backup, target)
            except Exception as rollback_exc:
                rollback_errors.append(
                    f"恢复 {target} 失败（备份 {backup}）："
                    f"{rollback_exc}"
                )
        if rollback_errors:
            rollback_incomplete = True
            raise ConversionError(
                "转换产物发布失败，且回滚不完整："
                + "；".join(rollback_errors)
            ) from exc
        if isinstance(exc, ConversionError):
            raise
        raise ConversionError(
            "转换产物发布失败，已恢复原文件："
            + str(exc)
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        for backup in (() if rollback_incomplete else backups.values()):
            try:
                if backup.exists():
                    backup.unlink()
            except Exception:
                pass


def convert_file(
    source: Path,
    target_format: TargetFormat = "auto",
    output: Path | None = None,
    output_dir: Path | None = None,
    source_key: str | None = None,
    target_key: str | None = None,
    overwrite: bool = False,
    copy_sidecars: bool = True,
    scene_catalog: Path | None = None,
) -> ConvertResult:
    source = source.resolve()
    chart = read_source(source, source_key)
    actual_target = choose_target_format(chart.kind, target_format)
    if not (chart.kind == "v1" and actual_target == "v1"):
        try:
            validate_standard_chart(chart.standard)
        except ChartValidationError as exc:
            raise ConversionError(f"输入域校验失败：{exc}") from exc
    if output is None:
        output_dir = (output_dir or default_output_dir()).resolve()
        output = target_path_for(source, output_dir, actual_target)
    else:
        output = output.resolve()
    if output == source:
        if chart.kind != actual_target:
            raise ConversionError("不允许原地改变格式；请选择不同的输出目录或文件名")
        if not overwrite:
            raise ConversionError("源文件和输出文件相同；请勾选覆盖，或换一个输出目录")
    if output.exists() and not overwrite and output != source:
        raise ConversionError(f"输出文件已存在：{output}（勾选“覆盖已有文件”后可继续）")

    sidecar_source = _find_sidecar(source) if copy_sidecars else None
    from screen_resources import publication_files
    try:
        screen_files = publication_files(source, output, copy_sidecars)
    except (ValueError, OSError) as exc:
        raise ConversionError("screen资源清单错误：" + str(exc)) from exc
    text_data = None
    if chart.kind == "v0":
        generated = build_showtext_styles(source)
        if generated:
            styles: list[dict] = []
            if sidecar_source is not None and sidecar_source.is_file():
                try:
                    root = json.loads(
                        sidecar_source.read_text(encoding="utf-8-sig"))
                    existing = (
                        root.get("texts") if isinstance(root, dict) else root
                    )
                    if isinstance(existing, list):
                        styles.extend(
                            item for item in existing
                            if isinstance(item, dict)
                        )
                except Exception as exc:
                    raise ConversionError(
                        f"showtext 配置无法解析：{sidecar_source}：{exc}"
                    ) from exc
            generated_ids = {int(item["id"]) for item in generated}
            # text() owns its automatically generated IDs. Refresh these
            # on republishing; explicit marker collisions were rejected by
            # the source parser before merging. Other style IDs stay intact.
            styles = [item for item in styles
                      if int(item.get("id", -1)) not in generated_ids]
            styles.extend(generated)
            text_data = json.dumps(
                {"texts": styles},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
    if text_data is None:
        validate_showtext_standard(chart.standard, source)
    else:
        validate_showtext_model(chart.standard, json.loads(text_data), str(source))
    warnings: list[str] = []
    scene = validate_chart_scene(chart.standard)
    scene_data = None
    if scene is not None and not copy_sidecars:
        raise ConversionError("mode4的scene清单是必需产物，不能使用no-sidecars省略")
    target_key_value: str | None = None
    output_data: bytes
    if actual_target == "v1":
        target_key_value = choose_target_key(output, target_key, chart.embedded_key)
        if chart.kind == "v1" and chart.raw is not None:
            data = raw_encode(chart.raw, target_key_value)
        else:
            data = bytes(pack_chart(chart.standard, target_key_value))
        _v1_roundtrip_check(
            data,
            target_key_value,
            len(chart.standard["notes"]),
            len(chart.standard.get("events", [])),
            chart.standard,
        )
        output_data = data
    elif actual_target == "v0":
        if chart.kind == "v0":
            output_data = source.read_bytes()
        else:
            key_for_comment = chart.embedded_key or (source.name if chart.kind == "v1" else None)
            try:
                text = _serialize_v0_with_key(chart.standard, key_for_comment)
            except (ValueError, OverflowError) as exc:
                raise ConversionError(f"{source}: 无法无损转换为明文：{exc}") from exc
            _v0_roundtrip_check(
                text,
                len(chart.standard["notes"]),
                len(chart.standard.get("events", [])),
                chart.standard,
            )
            output_data = text.encode("utf-8")
    else:
        payload = dict(chart.standard)
        if scene is not None:
            source_scene = (editable_source(scene) if scene["kind"] == "source"
                            else source_from_program(scene))
            payload["scene"] = {**source_scene, "actions": scene.get("actions", scene.get("compiledActions", []))}
        embedded = chart.embedded_key or (source.name if chart.kind == "v1" else None)
        if embedded:
            payload["sourceKey"] = embedded
        output_data = json.dumps(
            payload, ensure_ascii=False, indent=2
        ).encode("utf-8")

    if scene is not None:
        source_scene = (editable_source(scene) if scene["kind"] == "source"
                        else source_from_program(scene))
        if actual_target == "v1":
            source_scene["actions"] = scene.get("actions", scene.get("compiledActions", []))
            catalog = load_scene_catalog(source_scene["target"], scene_catalog)
            compiled = compile_program(source_scene, output_data, target_key_value, catalog)
            verify_program(compiled, output_data, target_key_value)
            scene_data = canonical_bytes(compiled)
        else:
            scene_data = canonical_bytes(source_scene)

    _publish_outputs(
        output,
        output_data,
        overwrite,
        sidecar_source,
        text_data,
        scene_data,
        screen_files,
    )

    if chart.kind == "v1" and actual_target == "v1" and chart.raw is not None:
        warnings.append(f"已按输出文件名重新加密：{target_key_value}。游戏内文件名必须与之完全一致。")

    return ConvertResult(
        source=source,
        output=output,
        source_format=chart.kind,
        target_format=actual_target,
        source_key=chart.embedded_key,
        target_key=target_key_value,
        notes=len(chart.standard["notes"]),
        events=len(chart.standard.get("events", [])),
        warnings=warnings,
    )


def collect_inputs(items: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in items:
        path = Path(item)
        if path.is_dir():
            for candidate in sorted(path.rglob("*")):
                if (
                    candidate.is_file()
                    and candidate.suffix.lower() in (".spc", ".json")
                    and not candidate.name.lower().endswith(".text.json")
                    and not candidate.name.lower().endswith(".screen.json")
                ):
                    files.append(candidate.resolve())
        elif path.is_file():
            files.append(path.resolve())
        else:
            raise ConversionError(f"路径不存在：{item}")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    if not unique:
        raise ConversionError("没有找到 .spc 或 .json 文件")
    return unique


def make_unique_output(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ConversionError(f"无法生成唯一输出文件名：{path}")


def batch_convert(
    inputs: list[Path],
    target_format: TargetFormat,
    output_dir: Path,
    *,
    source_key: str | None = None,
    target_key: str | None = None,
    overwrite: bool = False,
    copy_sidecars: bool = True,
    unique_names: bool = True,
    scene_catalog: Path | None = None,
) -> list[ConvertResult]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[ConvertResult] = []
    used: set[Path] = set()
    for source in inputs:
        chart = read_source(source, source_key if len(inputs) == 1 else None)
        actual = choose_target_format(chart.kind, target_format)
        output = target_path_for(source, output_dir, actual)
        if output in used:
            output = make_unique_output(output)
        elif unique_names and output.exists() and not overwrite:
            output = make_unique_output(output)
        used.add(output)
        results.append(
            convert_file(
                source,
                actual,
                output=output,
                source_key=source_key if len(inputs) == 1 else None,
                target_key=target_key if len(inputs) == 1 else None,
                overwrite=overwrite,
                copy_sidecars=copy_sidecars,
                scene_catalog=scene_catalog,
            )
        )
    return results


def verify_path(path: Path, key: str | None = None) -> str:
    path = path.resolve()
    kind = detect_format(path)
    if kind == "v1":
        key = key or path.name
        data = path.read_bytes()
        raw = raw_decode(data, key)
        rebuilt = raw_encode(raw, key)
        diffs = sum(1 for a, b in zip(data, rebuilt) if a != b) + abs(len(data) - len(rebuilt))
        if diffs:
            raise ConversionError(
                f"{path.name}: V1 重编码字节差为 {diffs}"
            )
        standard = raw_to_standard(raw)
        attach_bound_program(standard, path, data, key)
        try:
            validate_standard_chart(standard)
        except ChartValidationError as exc:
            raise ConversionError(
                f"{path.name}: V1 输入域校验失败：{exc}"
            ) from exc
        return (
            f"{path.name}: V1 verified, notes={len(raw['notes'])} events={len(raw['events'])},"
            f"reencoded byte differences={diffs}"
        )
    chart = read_source(path, key)
    try:
        validate_standard_chart(chart.standard)
    except ChartValidationError as exc:
        raise ConversionError(
            f"{path.name}: 输入域校验失败：{exc}"
        ) from exc
    return (
        f"{path.name}: {kind} parsed successfully, "
        f"notes={len(chart.standard['notes'])} events={len(chart.standard.get('events', []))}"
    )


def _print_result(result: ConvertResult) -> None:
    print_log(
        f'[OK] {result.source.name}: {result.source_format} -> {result.target_format}'
    )
    print_log(f'       output: {result.output}')
    print_log(f"       notes={result.notes} events={result.events}")
    if result.target_key:
        print_log(f'       V1 key: {result.target_key}')
    for warning in result.warnings:
        print_log(f'       [INFO] {warning}')


def cli_main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(
        prog="SPCConverter",
        description="In Falsus SPC V0 / V1 / 标准 JSON 纯 Python 命令行转换器",
    )
    parser.add_argument("inputs", nargs="*", help="输入文件或文件夹")
    parser.add_argument("--to", choices=("auto", "v0", "v1", "json"), default="auto",
                        help="目标格式；auto: V1→V0，V0/JSON→V1")
    parser.add_argument("-o", "--output-dir", help="批量输出目录，默认是工具目录下的 output")
    parser.add_argument("--output", help="单个文件的明确输出路径")
    parser.add_argument("--source-key", help="读取 SPC V1 时使用的源密钥，默认源文件名")
    parser.add_argument("--target-key", help="写入 SPC V1 时使用的目标密钥，默认输出文件名")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖输出文件")
    parser.add_argument("--no-sidecars", action="store_true", help="不复制同名 .text.json")
    parser.add_argument("--scene-catalog", type=Path, help="指定匹配版本的scene素材目录；默认随程序目录")
    parser.add_argument("--export-scene", action="store_true", help="离线导出可编辑scene动作和来源覆盖报告")
    parser.add_argument("--game-root", type=Path, help="导出演出时核对的游戏安装目录（只读）")
    parser.add_argument("--song", help="导出演出时精确筛选原歌曲ID；默认保留全部分支")
    parser.add_argument("--scene-performances", type=Path, help="指定经过版本核对的演出数据目录")
    parser.add_argument("--verify", action="store_true", help="只校验，不转换")
    parser.add_argument("--gui", action="store_true", help="启动图形界面")
    parser.add_argument("--interactive", action="store_true", help="启动控制台交互向导")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)

    if args.export_scene:
        if args.inputs or args.verify or args.gui or args.interactive or args.overwrite or args.no_sidecars:
            parser.error("--export-scene不能与普通转谱/GUI/覆盖或关闭侧文件选项混用")
        if args.game_root is None or not (args.output or args.output_dir):
            parser.error("--export-scene需要--game-root和--output（新目录）")
        from scene_export import export_scenes
        try:
            destination = Path(args.output or args.output_dir)
            report = export_scenes(args.game_root, destination, song=args.song,
                catalog_path=args.scene_catalog, performances_path=args.scene_performances)
            print_log(f"[OK] Scene export: {len(report['branches'])} branch(es), status={report['status']}: {destination}")
            if report["status"] != "complete":
                print_log('[INFO] Unresolved/unsupported sources remain; see coverage/unresolved. This is not a complete original performance export.')
            return 0
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print_log(f'[ERROR] Scene export failed: {exc}', file=sys.stderr)
            return 1
    if args.game_root is not None or args.song is not None or args.scene_performances is not None:
        parser.error("--game-root/--song/--scene-performances须与--export-scene一起使用")

    if args.verify:
        if not args.inputs:
            parser.error("--verify 需要至少一个输入文件")
        try:
            for item in collect_inputs(args.inputs):
                print_log(verify_path(item, args.source_key if len(args.inputs) == 1 else None))
            return 0
        except Exception as exc:
            print_log(f'[ERROR] {exc}', file=sys.stderr)
            return 1

    if args.interactive:
        return interactive_console()
    if args.gui or not args.inputs:
        from gui import launch_gui

        launch_gui(args.inputs)
        return 0

    try:
        inputs = collect_inputs(args.inputs)
        if args.output and len(inputs) != 1:
            raise ConversionError("--output 只能用于单个输入文件")
        if args.output:
            result = convert_file(
                inputs[0],
                args.to,
                output=Path(args.output),
                source_key=args.source_key,
                target_key=args.target_key,
                overwrite=args.overwrite,
                copy_sidecars=not args.no_sidecars,
                scene_catalog=args.scene_catalog,
            )
            _print_result(result)
        else:
            output_dir = Path(args.output_dir) if args.output_dir else default_output_dir()
            results = batch_convert(
                inputs,
                args.to,
                output_dir,
                source_key=args.source_key,
                target_key=args.target_key,
                overwrite=args.overwrite,
                copy_sidecars=not args.no_sidecars,
                unique_names=not args.overwrite,
                scene_catalog=args.scene_catalog,
            )
            for result in results:
                _print_result(result)
        return 0
    except Exception as exc:
        print_log(f'[ERROR] {exc}', file=sys.stderr)
        return 1


def interactive_console() -> int:
    print_log(f'In Falsus SPC Converter {VERSION}')
    print_log('Supports SPC V0 text / SPC V1 binary / standard JSON')
    try:
        source = input("源文件或文件夹：").strip().strip('"')
        if not source:
            print_log('[ERROR] No source path provided')
            return 1
        print_log('Target format:')
        print_log('  1. Auto (V1 -> V0, V0/JSON -> V1)')
        print_log('  2. SPC V1 binary')
        print_log('  3. SPC V0 text')
        print_log('  4. Standard JSON')
        choice = input("选择 [1]：").strip() or "1"
        target = {"1": "auto", "2": "v1", "3": "v0", "4": "json"}.get(choice)
        if target is None:
            print_log('[ERROR] Invalid option')
            return 1
        output_text = input(f"输出目录 [{default_output_dir()}]：").strip().strip('"')
        output_dir = Path(output_text) if output_text else default_output_dir()
        inputs = collect_inputs([source])
        results = batch_convert(inputs, target, output_dir)
        for result in results:
            _print_result(result)
        print_log(f'All completed: {output_dir.resolve()}')
        return 0
    except (KeyboardInterrupt, EOFError):
        print_log('\nCancelled')
        return 1
    except Exception as exc:
        print_log(f'[ERROR] {exc}', file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
