from __future__ import annotations

from fractions import Fraction
import math
from typing import Any, Mapping


MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF
TIMING_GROUP_MASK = 0x1FFFFFFF
TIMING_GROUP_FLOAT_EXACT_MAX = 1 << 24
NOINPUT_BIT = 0x80000000
NOCLIP_BIT = 0x40000000

TRACKMOVE_MAGIC = 0x54524B
SHOWTEXT_MAGIC = 0x545854
AUDIOOFFSET_MAGIC = 0x414F46
SCENECONTROL_MAGIC = 0x53434E
SETCOLOR_MAGIC = 0x434F4C
SUPPORTED_MARKERS = {
    TRACKMOVE_MAGIC,
    SHOWTEXT_MAGIC,
    AUDIOOFFSET_MAGIC,
    SCENECONTROL_MAGIC,
    SETCOLOR_MAGIC,
}


class ChartValidationError(ValueError):
    pass


def _fail(path: str, message: str) -> None:
    raise ChartValidationError(f"{path}: {message}")


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool):
        _fail(path, "必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        _fail(path, f"必须是整数，得到 {value!r}")
    try:
        if float(value) != number:
            _fail(path, f"必须是整数值，得到 {value!r}")
    except (TypeError, ValueError, OverflowError):
        pass
    return number


def _finite(value: Any, path: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ChartValidationError(f"{path}: 必须是数值，得到 {value!r}") from exc
    if not math.isfinite(number):
        _fail(path, f"必须是有限数值，得到 {value!r}")
    return number


def _group_flags(value: Any, path: str) -> tuple[int, int, bool, bool]:
    raw = _integer(value, path) & MASK64
    high = (raw >> 32) & MASK32
    group = high & TIMING_GROUP_MASK
    noinput = bool(high & NOINPUT_BIT)
    noclip = bool(high & NOCLIP_BIT)
    unknown = high & ~(
        TIMING_GROUP_MASK | NOINPUT_BIT | NOCLIP_BIT
    ) & MASK32
    if unknown:
        _fail(path, f"包含未知 group 高位 0x{unknown:08X}")
    if group == 0 and (noinput or noclip):
        _fail(path, "timinggroup 模式位存在但 group id 为 0")
    if group > TIMING_GROUP_FLOAT_EXACT_MAX:
        _fail(
            path,
            "timinggroup id 超过 float 精确整数范围 "
            f"({TIMING_GROUP_FLOAT_EXACT_MAX})，无法无损编码",
        )
    return raw & MASK32, group, noinput, noclip


def _fractions(value: Any, path: str) -> list[Fraction]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        _fail(path, "fractions 必须包含四个分数对")
    result: list[Fraction] = []
    for index, pair in enumerate(value):
        pair_path = f"{path}[{index}]"
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _fail(pair_path, "分数对必须是两个整数")
        numerator = _integer(pair[0], f"{pair_path}[0]")
        denominator = _integer(pair[1], f"{pair_path}[1]")
        if denominator == 0:
            _fail(pair_path, "分母不能为 0")
        result.append(Fraction(numerator, denominator))
    return result


def _note_type(note: Mapping[str, Any], path: str) -> int:
    note_type = _integer(note.get("type"), f"{path}.type")
    if note_type not in (1, 2, 3, 4, 5):
        _fail(f"{path}.type", f"不支持的 note type {note_type}")
    return note_type


def _validate_note(note: Any, index: int) -> None:
    path = f"notes[{index}]"
    if not isinstance(note, Mapping):
        _fail(path, "note 必须是对象")
    note_type = _note_type(note, path)
    start_ms = _integer(note.get("timeMs"), f"{path}.timeMs")
    end_ms = _integer(note.get("endMs"), f"{path}.endMs")
    if start_ms < 0:
        _fail(f"{path}.timeMs", "不能为负数")
    if note_type != 3 and end_ms < start_ms:
        _fail(f"{path}.endMs", "不能早于 timeMs")

    fractions = _fractions(note.get("fractions"), f"{path}.fractions")
    flags = _integer(note.get("flags", 0), f"{path}.flags") & MASK32
    line = _integer(note.get("line", 0), f"{path}.line")

    if note_type in (1, 2):
        lane_number = fractions[0] * 4
        if lane_number.denominator != 1 or not 0 <= lane_number <= 5:
            _fail(f"{path}.fractions[0]", "地键轨道必须在 0..5")
        width_number = fractions[2] * 4
        if width_number.denominator != 1 or width_number < 0:
            _fail(f"{path}.fractions[2]", "地键宽度不能为负数")
    elif note_type == 4:
        direction = _integer(
            note.get("flick", (flags >> 8) & 0xFF),
            f"{path}.flick",
        )
        if direction not in (4, 16):
            _fail(f"{path}.flick", "只支持右 4 或左 16")
    elif note_type == 5:
        left_ease = _integer(
            note.get("leftEase", 0), f"{path}.leftEase")
        right_ease = _integer(
            note.get("rightEase", 0), f"{path}.rightEase")
        if left_ease not in (0, 1, 2):
            _fail(f"{path}.leftEase", "必须是 0、1 或 2")
        if right_ease not in (0, 1, 2):
            _fail(f"{path}.rightEase", "必须是 0、1 或 2")
    else:
        magic = (flags >> 8) & 0xFFFFFF
        if magic not in SUPPORTED_MARKERS:
            _fail(path, f"不支持的 type3 marker magic 0x{magic:06X}")
        if magic == TRACKMOVE_MAGIC:
            ease = flags & 0xFF
            if ease not in (0, 1, 2, 3):
                _fail(f"{path}.flags", "trackmove ease 必须在 0..3")

    if note_type != 3:
        _group_flags(line, f"{path}.line")


def _validate_event(event: Any, index: int) -> None:
    path = f"events[{index}]"
    if not isinstance(event, Mapping):
        _fail(path, "event 必须是对象")
    event_type = _integer(event.get("type"), f"{path}.type")
    time_ms = _integer(event.get("timeMs"), f"{path}.timeMs")
    if time_ms < 0:
        _fail(f"{path}.timeMs", "不能为负数")

    if event_type == 0:
        _finite(event.get("value"), f"{path}.value")
    elif event_type in (1, 2):
        bpm = _finite(event.get("bpm"), f"{path}.bpm")
        if bpm <= 0:
            _fail(f"{path}.bpm", "必须大于 0")
        _finite(event.get("speed", 0.0), f"{path}.speed")
    elif event_type == 3:
        flags = _integer(event.get("flags", 0), f"{path}.flags")
        lane = flags & 0xFF
        if lane > 5:
            _fail(f"{path}.flags", "lane index 必须在 0..5")
        if flags & ~0x1FF:
            _fail(f"{path}.flags", "包含未知 lane 标志位")
    elif event_type == 5:
        group = _integer(event.get("speed"), f"{path}.speed")
        if not 1 <= group <= TIMING_GROUP_FLOAT_EXACT_MAX:
            _fail(
                f"{path}.speed",
                "timinggroup id 必须在 1.."
                f"{TIMING_GROUP_FLOAT_EXACT_MAX}，否则 float 编码会丢整数",
            )
        _finite(event.get("value"), f"{path}.value")
    else:
        _fail(f"{path}.type", f"不支持的 event type {event_type}")


def validate_standard_chart(
    obj: Mapping[str, Any],
) -> None:
    if not isinstance(obj, Mapping):
        raise ChartValidationError("标准谱面根节点必须是对象")
    notes = obj.get("notes")
    events = obj.get("events", [])
    if not isinstance(notes, list):
        _fail("notes", "必须是数组")
    if not isinstance(events, list):
        _fail("events", "必须是数组")
    for index, note in enumerate(notes):
        _validate_note(note, index)
        note_index = _integer(
            note.get("index"), f"notes[{index}].index")
        if note_index != index:
            _fail(
                f"notes[{index}].index",
                f"必须等于数组位置 {index}，得到 {note_index}",
            )
    for index, event in enumerate(events):
        _validate_event(event, index)
        event_id = _integer(
            event.get("id"), f"events[{index}].id")
        if event_id != index:
            _fail(
                f"events[{index}].id",
                f"必须等于数组位置 {index}，得到 {event_id}",
            )
    if "noteCount" not in obj:
        _fail("noteCount", "缺少 noteCount")
    if "eventCount" not in obj:
        _fail("eventCount", "缺少 eventCount")
    note_count = _integer(obj.get("noteCount"), "noteCount")
    event_count = _integer(obj.get("eventCount"), "eventCount")
    if note_count != len(notes):
        _fail(
            "noteCount",
            f"必须等于 notes 数组长度 {len(notes)}，得到 {note_count}",
        )
    if event_count != len(events):
        _fail(
            "eventCount",
            f"必须等于 events 数组长度 {len(events)}，得到 {event_count}",
        )

    bpm = _finite(obj.get("bpm", 0.0), "bpm")
    meter = _finite(obj.get("meter", 0.0), "meter")
    if bpm <= 0:
        _fail("bpm", "必须大于 0")
    if meter <= 0:
        _fail("meter", "必须大于 0")
