from __future__ import annotations

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
from showtext_validation import validate_showtext_standard  # noqa: E402


VERSION = "1.2.3"
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
        raw = raw_decode(path.read_bytes(), key)
        standard = raw_to_standard(raw)
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
        obj = read_spc_v0_chart(str(path))
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
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    sidecar_target = (
        output.with_name(output.stem + ".text.json")
        if sidecar_source is not None
        else None
    )
    if (
        sidecar_target is not None
        and sidecar_target.exists()
        and not overwrite
        and sidecar_target.resolve() != sidecar_source.resolve()
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
    try:
        staged_chart = staging / output.name
        staged_chart.write_bytes(chart_data)
        publish: list[tuple[Path, Path]] = [
            (output, staged_chart)
        ]
        if (
            sidecar_target is not None
            and sidecar_target.resolve() != sidecar_source.resolve()
        ):
            staged_sidecar = staging / sidecar_target.name
            shutil.copy2(sidecar_source, staged_sidecar)
            publish.append((sidecar_target, staged_sidecar))

        for target, _ in publish:
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
        for backup in backups.values():
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
) -> ConvertResult:
    source = source.resolve()
    chart = read_source(source, source_key)
    validate_showtext_standard(chart.standard, source)
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
    warnings: list[str] = []
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
            if chart.kind == "v1":
                text = serialize_v0_semantic(chart.standard)
                if key_for_comment:
                    text = (
                        f"# SPC key: {key_for_comment}\n"
                        "# Re-encoding to SPC V1 will reuse this key automatically.\n"
                        + text
                    )
            else:
                text = _serialize_v0_with_key(chart.standard, key_for_comment)
            _v0_roundtrip_check(
                text,
                len(chart.standard["notes"]),
                len(chart.standard.get("events", [])),
                chart.standard,
            )
            output_data = text.encode("utf-8")
    else:
        payload = dict(chart.standard)
        embedded = chart.embedded_key or (source.name if chart.kind == "v1" else None)
        if embedded:
            payload["sourceKey"] = embedded
        output_data = json.dumps(
            payload, ensure_ascii=False, indent=2
        ).encode("utf-8")

    _publish_outputs(
        output,
        output_data,
        overwrite,
        sidecar_source,
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
        return (
            f"{path.name}: V1 校验通过，notes={len(raw['notes'])} events={len(raw['events'])}，"
            f"重编码字节差={diffs}"
        )
    chart = read_source(path, key)
    return (
        f"{path.name}: {FORMAT_NAMES[kind]} 解析通过，"
        f"notes={len(chart.standard['notes'])} events={len(chart.standard.get('events', []))}"
    )


def _print_result(result: ConvertResult) -> None:
    print(
        f"[完成] {result.source.name}: {FORMAT_NAMES[result.source_format]} → "
        f"{FORMAT_NAMES[result.target_format]}"
    )
    print(f"       输出：{result.output}")
    print(f"       notes={result.notes} events={result.events}")
    if result.target_key:
        print(f"       V1 密钥：{result.target_key}")
    for warning in result.warnings:
        print(f"       [提示] {warning}")


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
    parser.add_argument("--verify", action="store_true", help="只校验，不转换")
    parser.add_argument("--gui", action="store_true", help="启动图形界面")
    parser.add_argument("--interactive", action="store_true", help="启动控制台交互向导")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)

    if args.verify:
        if not args.inputs:
            parser.error("--verify 需要至少一个输入文件")
        try:
            for item in collect_inputs(args.inputs):
                print(verify_path(item, args.source_key if len(args.inputs) == 1 else None))
            return 0
        except Exception as exc:
            print(f"[失败] {exc}", file=sys.stderr)
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
            )
            for result in results:
                _print_result(result)
        return 0
    except Exception as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1


def interactive_console() -> int:
    print(f"In Falsus SPC 转换器 {VERSION}")
    print("支持 SPC V0 明文 / SPC V1 二进制 / 标准 JSON")
    try:
        source = input("源文件或文件夹：").strip().strip('"')
        if not source:
            print("[失败] 未输入源路径")
            return 1
        print("目标格式：")
        print("  1. 自动（V1→V0，V0/JSON→V1）")
        print("  2. SPC V1 二进制")
        print("  3. SPC V0 明文")
        print("  4. 标准 JSON")
        choice = input("选择 [1]：").strip() or "1"
        target = {"1": "auto", "2": "v1", "3": "v0", "4": "json"}.get(choice)
        if target is None:
            print("[失败] 无效选项")
            return 1
        output_text = input(f"输出目录 [{default_output_dir()}]：").strip().strip('"')
        output_dir = Path(output_text) if output_text else default_output_dir()
        inputs = collect_inputs([source])
        results = batch_convert(inputs, target, output_dir)
        for result in results:
            _print_result(result)
        print(f"全部完成：{output_dir.resolve()}")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n已取消")
        return 1
    except Exception as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
