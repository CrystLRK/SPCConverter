from __future__ import annotations

from fractions import Fraction
import math
import struct
from typing import Any

from chart_validation import validate_standard_chart


MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF
TIMING_GROUP_MASK = 0x1FFFFFFF
NOINPUT_BIT = 0x80000000
NOCLIP_BIT = 0x40000000
NOINPUT_NOTE_FLAG = 0x8000
BOMB_FLAG = 0x8000
FLICK_DIRECTION_MASK = 0xFF00
SKYAREA_LEFT_EASE_MASK = 0x1C
SKYAREA_RIGHT_EASE_MASK = 0xE0
SKYAREA_LEFT_EASE_FLAGS = (0x04, 0x08, 0x10)
SKYAREA_RIGHT_EASE_FLAGS = (0x20, 0x40, 0x80)

TRACKMOVE_MAGIC = 0x54524B
SHOWTEXT_MAGIC = 0x545854
AUDIOOFFSET_MAGIC = 0x414F46
SCENECONTROL_MAGIC = 0x53434E
SETCOLOR_MAGIC = 0x434F4C
SCENE_TRACKDISPLAY = 1
SCENE_HIDEGROUP = 2


def _num(value: Any) -> str:
    if isinstance(value, int):
        return str(value)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"非有限数值：{value}")
    return repr(number)


def _raw_group(value: Any) -> int:
    return int(value) & MASK64


def _split_group(value: Any) -> tuple[int, int, bool, bool]:
    raw = _raw_group(value)
    low = raw & MASK32
    high = (raw >> 32) & MASK32
    group = high & TIMING_GROUP_MASK
    noinput = bool(high & NOINPUT_BIT)
    noclip = bool(high & NOCLIP_BIT)
    if group == 0 and (noinput or noclip):
        raise ValueError("timinggroup 模式位存在，但 group id 为 0")
    return low, group, noinput, noclip


def _fraction(pair: Any) -> Fraction:
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise ValueError(f"非法分数对：{pair!r}")
    numerator = int(pair[0])
    denominator = int(pair[1])
    if denominator == 0:
        raise ValueError(f"分母为 0：{pair!r}")
    return Fraction(numerator, denominator)


def _effective_flags(note: dict[str, Any]) -> int:
    """Fold standard-model convenience fields back into the native flags."""
    note_type = int(note["type"])
    flags = int(note.get("flags", 0)) & MASK32
    if note_type == 4 and note.get("flick") is not None:
        direction = int(note["flick"])
        if not 0 <= direction <= 0xFF:
            raise ValueError(f"flick 方向越界：{direction}")
        flags = (flags & ~FLICK_DIRECTION_MASK) | (direction << 8)
    elif note_type == 5:
        if note.get("leftEase") is not None:
            ease = int(note["leftEase"])
            if ease not in (0, 1, 2):
                raise ValueError(f"SkyArea 左 ease 越界：{ease}")
            flags = (
                (flags & ~SKYAREA_LEFT_EASE_MASK)
                | SKYAREA_LEFT_EASE_FLAGS[ease]
            )
        if note.get("rightEase") is not None:
            ease = int(note["rightEase"])
            if ease not in (0, 1, 2):
                raise ValueError(f"SkyArea 右 ease 越界：{ease}")
            flags = (
                (flags & ~SKYAREA_RIGHT_EASE_MASK)
                | SKYAREA_RIGHT_EASE_FLAGS[ease]
            )
    return flags & MASK32


def _note_semantic(note: dict[str, Any]) -> tuple:
    note_type = int(note["type"])
    raw_group = _raw_group(note.get("line", 0))
    fractions = tuple(_fraction(pair) for pair in note.get("fractions", []))
    flags = _effective_flags(note)

    if note_type == 3:
        low = raw_group
        timing_group = 0
        noinput_group = False
        noclip_group = False
    else:
        low, timing_group, noinput_group, noclip_group = _split_group(raw_group)
    if note_type in (1, 2, 4):
        # ground low32 is opaque and has no V0 representation
        low = 0
    elif note_type == 3:
        # type3 carries marker data in line; it never uses timinggroup high bits
        timing_group = 0
        noinput_group = False
        noclip_group = False
        low = raw_group
    elif note_type != 5:
        raise ValueError(f"不支持的 note type：{note_type}")

    return (
        note_type,
        int(note["timeMs"]),
        int(note["endMs"]),
        int(note.get("track", 0)),
        flags,
        low,
        timing_group,
        noinput_group,
        noclip_group,
        fractions,
    )


def _event_semantic(event: dict[str, Any]) -> tuple | None:
    event_type = int(event["type"])
    time_ms = int(event["timeMs"])
    if event_type == 0:
        return ("track", time_ms, round(float(event["value"]), 12))
    if event_type == 1:
        return (
            "bpm",
            time_ms,
            round(float(event["bpm"]), 12),
            _float32(event.get("speed", 0.0)),
        )
    if event_type == 2:
        return (
            "bpm-transition",
            time_ms,
            round(float(event["bpm"]), 12),
            _float32(event.get("speed", 0.0)),
        )
    if event_type == 3:
        return ("lane", time_ms, int(event.get("flags", 0)) & MASK32)
    # type5 is normalized as a group-speed timeline below
    if event_type == 5:
        return None
    raise ValueError(f"不支持的 event type：{event_type}")


def _float32(value: Any) -> float:
    """Canonicalize values encoded as IEEE-754 binary32 by SPC V1."""
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _group_timelines(events: list[dict[str, Any]]) -> dict[int, tuple]:
    pending: dict[int, list[tuple[int, float]]] = {}
    for index, event in enumerate(events):
        if int(event["type"]) != 5:
            continue
        if "speed" not in event:
            raise ValueError("type5 event 缺少 speed 字段")
        group = int(round(float(event["speed"])))
        if group <= 0 or group > TIMING_GROUP_MASK:
            raise ValueError(f"timinggroup id 越界：{group}")
        value = float(event["value"])
        if not math.isfinite(value):
            raise ValueError("type5 event 含非有限速度")
        pending.setdefault(group, []).append((int(event["timeMs"]), value))

    result: dict[int, tuple] = {}
    for group, values in pending.items():
        values.sort(key=lambda item: item[0])
        by_time: dict[int, float] = {}
        for time_ms, value in values:
            if time_ms < 0:
                raise ValueError("type5 event 时间不能为负数")
            by_time[time_ms] = value
        collapsed: list[tuple[int, float]] = [
            (0, by_time.get(0, 1.0))
        ]
        collapsed.extend(
            (time_ms, value)
            for time_ms, value in sorted(by_time.items())
            if time_ms != 0
        )
        result[group] = tuple(collapsed)
    return result


def _group_speed_series(
        events: list[dict[str, Any]]) -> dict[int, list[tuple[int, float]]]:
    """Collapse V1 type5 events to the effective per-group speed timeline."""
    by_group: dict[int, dict[int, float]] = {}
    for event in events:
        if int(event["type"]) != 5:
            continue
        group = int(round(float(event["speed"])))
        if group <= 0 or group > TIMING_GROUP_MASK:
            raise ValueError(f"timinggroup id 越界：{group}")
        value = float(event["value"])
        if not math.isfinite(value):
            raise ValueError("type5 event 含非有限速度")
        time_ms = int(event["timeMs"])
        if time_ms < 0:
            raise ValueError("type5 event 时间不能为负数")
        by_group.setdefault(group, {})[time_ms] = value
    return {
        group: sorted(values.items())
        for group, values in by_group.items()
    }


def semantic_digest(obj: dict[str, Any]) -> tuple:
    """Return a format-independent representation of playable chart semantics."""
    notes = [_note_semantic(note) for note in obj.get("notes", [])]
    notes.sort(key=lambda item: (item[1], item[2], item[0]))

    events = []
    for event in obj.get("events", []):
        item = _event_semantic(event)
        if item is not None:
            events.append(item)
    events.sort(key=lambda item: (item[1], item[0], item[2:]))

    timelines = _group_timelines(obj.get("events", []))
    for note in notes:
        if note[6] > 0:
            timelines.setdefault(note[6], ((0, 1.0),))

    return (
        round(float(obj.get("bpm", 0.0)), 12),
        round(float(obj.get("meter", 0.0)), 12),
        tuple(notes),
        tuple(events),
        tuple(sorted(timelines.items())),
    )


def _v0_note_lines(note: dict[str, Any]) -> list[str]:
    note_type = int(note["type"])
    time_ms = int(note["timeMs"])
    fractions = note.get("fractions", [])
    flags = _effective_flags(note)

    if note_type == 1:
        lane = int(round(
            4.0 * float(fractions[0][0]) / float(fractions[0][1])))
        width = int(round(
            4.0 * float(fractions[2][0]) / float(fractions[2][1])))
        name = "bomb" if flags & BOMB_FLAG else "tap"
        return [f"{name}({time_ms},{width},{lane})"]
    if note_type == 2:
        lane = int(round(
            4.0 * float(fractions[0][0]) / float(fractions[0][1])))
        width = int(round(
            4.0 * float(fractions[2][0]) / float(fractions[2][1])))
        duration = int(note["endMs"]) - time_ms
        return [f"hold({time_ms},{lane},{width},{duration})"]
    if note_type == 4:
        center = int(fractions[0][0])
        denominator = int(fractions[0][1])
        width = int(fractions[2][0])
        direction = (flags >> 8) & 0xFF
        return [f"flick({time_ms},{center},{denominator},{width},{direction})"]
    if note_type == 5:
        sc, sden = int(fractions[0][0]), int(fractions[0][1])
        ec, eden = int(fractions[1][0]), int(fractions[1][1])
        sw, ew = int(fractions[2][0]), int(fractions[3][0])
        le = {0x04: 0, 0x08: 1, 0x10: 2}.get(flags & 0x1C, 0)
        re = {0x20: 0, 0x40: 1, 0x80: 2}.get(flags & 0xE0, 0)
        group = _split_group(note.get("line", 0))[0]
        lines = [
            f"skyarea({time_ms},{sc},{sden},{sw},{ec},{eden},{ew},"
            f"{le},{re},{int(note['endMs']) - time_ms},{group})"
        ]
        if flags & NOINPUT_NOTE_FLAG:
            lines.append(f"noinput({group})")
        return lines
    if note_type != 3:
        raise ValueError(f"不支持的 note type：{note_type}")

    magic = (flags >> 8) & 0xFFFFFF
    if magic == SHOWTEXT_MAGIC:
        return [f"showtext({time_ms},{int(note['line'])},{int(fractions[0][0])})"]
    if magic == AUDIOOFFSET_MAGIC:
        return [f"audiooffset({int(note['line'])})"]
    if magic == SETCOLOR_MAGIC:
        rgba = int(note["endMs"]) & MASK32
        color = f"#{rgba:08X}" if rgba & 0xFF != 0xFF else f"#{rgba >> 8:06X}"
        edge = int(fractions[0][0]) != 0
        return [
            f"setcolor({time_ms},{int(note['line'])},{color}"
            + ("" if edge else ",0")
            + ")"
        ]
    if magic == SCENECONTROL_MAGIC:
        mode = flags & 0xFF
        if mode == SCENE_TRACKDISPLAY:
            alpha = int(fractions[0][0])
            return [
                f"scenecontrol({time_ms},trackdisplay,"
                f"{int(note['endMs'])},{alpha})"
            ]
        if mode == SCENE_HIDEGROUP:
            return [
                f"scenecontrol({time_ms},hidegroup,"
                f"{int(note['line'])},{int(note['endMs'])})"
            ]
        raise ValueError(f"不支持的 scenecontrol mode：{mode}")
    if magic == TRACKMOVE_MAGIC:
        x = float(fractions[0][0]) / float(fractions[0][1])
        y = float(fractions[1][0]) / float(fractions[1][1])
        z = float(fractions[2][0]) / float(fractions[2][1])
        xoz = float(fractions[3][0]) / float(fractions[3][1])
        group = int(note["line"]) & MASK64
        yoz = ((group & MASK32) ^ 0x80000000) - 0x80000000
        xoy = ((group >> 32) ^ 0x80000000) - 0x80000000
        return [
            f"trackmove({time_ms},{_num(x)},{_num(y)},{_num(z)},"
            f"{_num(xoz)},{_num(yoz / 1000)},{_num(xoy / 1000)},"
            f"{flags & 0xFF},{int(note['endMs']) - time_ms})"
        ]
    raise ValueError(f"不支持的 type3 marker magic：0x{magic:06X}")


def serialize_v0_semantic(obj: dict[str, Any]) -> str:
    """Serialize a standard chart to canonical V0 while preserving timing groups."""
    validate_standard_chart(obj)
    lines: list[str] = [f"chart({_num(obj['bpm'])},{_num(obj['meter'])})"]
    notes = list(obj.get("notes", []))
    events = list(obj.get("events", []))

    groups: dict[int, list[dict[str, Any]]] = {}
    group_modes: dict[int, tuple[bool, bool]] = {}
    global_notes: list[dict[str, Any]] = []
    for note in notes:
        note_type = int(note["type"])
        if note_type == 3:
            global_notes.append(note)
            continue
        low, group, noinput, noclip = _split_group(note.get("line", 0))
        if group == 0:
            global_notes.append(note)
            continue
        mode = (noinput, noclip)
        if group in group_modes and group_modes[group] != mode:
            raise ValueError(f"timinggroup {group} 的 noinput/noclip 模式不一致")
        group_modes[group] = mode
        groups.setdefault(group, []).append(note)

    group_speeds = _group_speed_series(events)
    for group in group_speeds:
        group_modes.setdefault(group, (False, False))

    def note_key(note: dict[str, Any]) -> tuple:
        return (int(note["timeMs"]), int(note["endMs"]), int(note.get("index", 0)))

    global_notes.sort(key=note_key)
    for note in global_notes:
        lines.extend(_v0_note_lines(note))

    for group in sorted(set(groups) | set(group_speeds)):
        noinput, noclip = group_modes.get(group, (False, False))
        option = ""
        if noclip:
            option = ",noclip=true"
        elif noinput:
            option = ",noinput=true"
        lines.append(f"timinggroup({group}{option}) {{")
        for time_ms, value in group_speeds.get(group, []):
            lines.append(f"    track({time_ms},{_num(value)})")
        for note in sorted(groups.get(group, []), key=note_key):
            for note_line in _v0_note_lines(note):
                lines.append("    " + note_line)
        lines.append("}")

    for event in sorted(
            events,
            key=lambda item: (int(item["timeMs"]), int(item.get("id", 0)))):
        event_type = int(event["type"])
        time_ms = int(event["timeMs"])
        if event_type == 0:
            lines.append(f"track({time_ms},{_num(event['value'])})")
        elif event_type == 1:
            lines.append(
                f"bpm({time_ms},{_num(event['bpm'])},{_num(event['speed'])})"
            )
        elif event_type == 2:
            lines.append(
                f"bpm({time_ms},{_num(event['bpm'])},{_num(event['speed'])},1)"
            )
        elif event_type == 3:
            event_flags = int(event.get("flags", 0))
            lines.append(
                f"lane({time_ms},{event_flags & 0xFF},"
                f"{1 if event_flags & 0x100 else 0})"
            )
        elif event_type == 5:
            continue
        else:
            raise ValueError(f"不支持的 event type：{event_type}")
    return "\n".join(lines) + "\n"
