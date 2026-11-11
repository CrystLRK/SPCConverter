from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from icp1_codec import decode


SHOWTEXT_MAGIC = 0x545854


class ShowTextValidationError(ValueError):
    pass


def showtext_ids(payload: bytes, parser_key: str) -> list[int]:
    chart = decode(payload, parser_key)
    return showtext_ids_from_standard(_adapt_raw_notes(chart))


def _raw_notes(chart: dict[str, Any]) -> list[dict[str, Any]]:
    notes = chart.get("notes")
    if not isinstance(notes, list):
        raise ShowTextValidationError("谱面缺少 notes 数组")
    return notes


def _adapt_raw_notes(chart: dict[str, Any]) -> dict[str, Any]:
    """Adapt decoded raw notes to the fields used by the standard JSON form."""
    adapted = []
    for note in _raw_notes(chart):
        adapted.append(
            {
                "type": int(note["type"]),
                "flags": int(note["flags"]),
                "line": int(note["group"]),
            }
        )
    return {"notes": adapted}


def showtext_ids_from_standard(chart: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for note in _raw_notes(chart):
        if int(note["type"]) != 3:
            continue
        if ((int(note["flags"]) >> 8) & 0xFFFFFF) == SHOWTEXT_MAGIC:
            ids.append(int(note.get("line", note.get("group", -1))))
    return ids


def sidecar_candidates(chart_path: Path) -> tuple[Path, Path]:
    return (
        chart_path.with_suffix(".text.json"),
        chart_path.with_name(chart_path.name + ".text.json"),
    )


def _style_entries(root: Any) -> list[dict[str, Any]]:
    values = root.get("texts") if isinstance(root, dict) else root
    if not isinstance(values, list):
        raise ShowTextValidationError("showtext 配置根节点必须是数组或包含 texts 数组")
    if not all(isinstance(item, dict) for item in values):
        raise ShowTextValidationError("showtext 配置中的每一项都必须是对象")
    return values


def _validate_style(style: dict[str, Any]) -> int:
    try:
        text_id = int(style["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ShowTextValidationError("showtext 样式缺少有效的整数 id") from exc
    if text_id < 0:
        raise ShowTextValidationError(f"showtext id 不能为负数：{text_id}")
    if "text" not in style:
        raise ShowTextValidationError(
            f"showtext id={text_id} 缺少 text 字段"
        )
    if not isinstance(style["text"], str):
        raise ShowTextValidationError(f"showtext id={text_id} 的 text 必须是字符串")

    for field in (
        "duration", "fadeIn", "fadeOut", "x", "y", "scale",
        "shadowAlpha", "shadowScaleX", "shadowScaleY", "shadowOffsetY",
    ):
        if field not in style:
            continue
        try:
            value = float(style[field])
        except (TypeError, ValueError) as exc:
            raise ShowTextValidationError(
                f"showtext id={text_id} 的 {field} 必须是数字"
            ) from exc
        if not math.isfinite(value):
            raise ShowTextValidationError(
                f"showtext id={text_id} 的 {field} 不能为 NaN/Infinity"
            )
        if field in ("duration", "fadeIn", "fadeOut") and value < 0:
            raise ShowTextValidationError(
                f"showtext id={text_id} 的 {field} 不能为负数"
            )

    if "size" in style:
        try:
            size = int(style["size"])
        except (TypeError, ValueError) as exc:
            raise ShowTextValidationError(
                f"showtext id={text_id} 的 size 必须是整数"
            ) from exc
        if size <= 0:
            raise ShowTextValidationError(
                f"showtext id={text_id} 的 size 必须大于 0"
            )
    if "shadow" in style and not isinstance(style["shadow"], bool):
        raise ShowTextValidationError(
            f"showtext id={text_id} 的 shadow 必须是布尔值"
        )
    if "color" in style:
        color = style["color"]
        if not isinstance(color, str):
            raise ShowTextValidationError(
                f"showtext id={text_id} 的 color 必须是字符串"
            )
        value = color.lstrip("#")
        if len(value) not in (6, 8):
            raise ShowTextValidationError(
                f"showtext id={text_id} 的 color 必须是 #RRGGBB 或 #RRGGBBAA"
            )
        try:
            int(value, 16)
        except ValueError as exc:
            raise ShowTextValidationError(
                f"showtext id={text_id} 的 color 含非十六进制字符"
            ) from exc
    return text_id


def validate_showtext_standard(chart: dict[str, Any], chart_path: Path) -> None:
    """Validate a standard chart before conversion writes any output."""
    _validate_showtext_ids(showtext_ids_from_standard(chart), chart_path)


def validate_showtext_sidecar(chart_path: Path, parser_key: str) -> None:
    """Fail before packaging when a chart references missing/invalid showtext."""
    chart_path = chart_path.resolve()
    referenced = showtext_ids(chart_path.read_bytes(), parser_key)
    _validate_showtext_ids(referenced, chart_path)


def _validate_showtext_ids(referenced: list[int], chart_path: Path) -> None:
    if not referenced:
        return

    found = next((path for path in sidecar_candidates(chart_path) if path.is_file()), None)
    if found is None:
        names = " 或 ".join(str(path) for path in sidecar_candidates(chart_path))
        raise ShowTextValidationError(
            f"谱面包含 showtext，但缺少文本配置：{names}"
        )
    try:
        root = json.loads(found.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ShowTextValidationError(
            f"showtext 配置不是有效 JSON：{found} ({exc})"
        ) from exc

    styles = _style_entries(root)
    defined = {_validate_style(style) for style in styles}
    missing = sorted(set(referenced) - defined)
    if missing:
        raise ShowTextValidationError(
            f"showtext 配置缺少谱面引用的 id：{missing} ({found})"
        )
