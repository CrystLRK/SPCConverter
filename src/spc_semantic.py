from __future__ import annotations
from special_screen import serialize_payload as serialize_special_screen

from fractions import Fraction
import math
import struct
from typing import Any

from chart_validation import validate_standard_chart
from scene_program import (TABLES as SCENE_TABLES, canonical_bytes, editable_source,
                           is_marker as is_scene_marker, serialize_action,
                           source_from_program, validate_action, validate_chart_scene)


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
LINE_MAGIC = 0x4C494E
ARC_MAGIC = 0x415243
FLICK3D_MAGIC = 0x464C33
BOMBAREA_MAGIC = 0x424D41
TIMINGGROUP_ANGLE_MAGIC = 0x544741
ARC_NATIVE_FLAG = 0x02000000
FLICK3D_NATIVE_FLAG = 0x01000000
SCENE_TRACKDISPLAY = 1
SCENE_HIDEGROUP = 2
SCENE_SCREEN = 3
SCREEN_EFFECTS = {
    1: "blur",
    2: "crt",
    3: "chromatic",
    4: "glass",
    5: "analogglitch",
    6: "vignette",
    7: "filmgrain",
    8: "coloradjust",
    9: "bloom",
    10: "clear",
}
SCREEN_EFFECT_PARAMETER_COUNT = {
    1: 1,
    2: 1,
    3: 4,
    4: 5,
    5: 5,
    6: 2,
    7: 2,
    8: 4,
    9: 3,
    10: 0,
}


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
        magic = (flags >> 8) & 0xFFFFFF
        if magic in (LINE_MAGIC, ARC_MAGIC, FLICK3D_MAGIC):
            low, timing_group, noinput_group, noclip_group = _split_group(raw_group)
        else:
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
    """Canonical speed/BPM states; stable same-time order is significant."""
    pending: dict[int, list[tuple[int, float, float]]] = {}
    for event in events:
        if int(event["type"]) != 5:
            continue
        group = int(event["speed"])
        if group <= 0 or group > TIMING_GROUP_MASK or float(event["speed"]) != group:
            raise ValueError(f"timinggroup id 越界或不是整数：{event['speed']}")
        value = float(event["value"])
        try:
            bpm = _float32(event.get("groupBpm", 0.0))
        except (ValueError, OverflowError, struct.error) as exc:
            raise ValueError(f"timinggroup {group} groupBpm 无法表示为float32") from exc
        if not math.isfinite(value) or not math.isfinite(bpm):
            raise ValueError(f"timinggroup {group} 含非有限速度/BPM")
        time_ms = int(event["timeMs"])
        if time_ms < 0:
            raise ValueError("type5 event 时间不能为负数")
        pending.setdefault(group, []).append((time_ms, value, bpm))

    result: dict[int, tuple] = {}
    for group, values in pending.items():
        values.sort(key=lambda item: item[0])  # Python stable: original same-time order.
        collapsed = [(0, 1.0, 0.0)]
        for index, state in enumerate(values):
            if index == 0 and state[0] == 0:
                collapsed[0] = state
            elif state[1:] != collapsed[-1][1:]:
                collapsed.append(state)
        result[group] = tuple(collapsed)
    return result


def _group_speed_series(events: list[dict[str, Any]]) -> dict[int, tuple]:
    """Both independent type5 fields, not just visually equivalent speeds."""
    return _group_timelines(events)


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
            timelines.setdefault(note[6], ((0, 1.0, 0.0),))

    result = (
        round(float(obj.get("bpm", 0.0)), 12),
        round(float(obj.get("meter", 0.0)), 12),
        tuple(notes),
        tuple(events),
        tuple(sorted(timelines.items())),
    )
    scene = validate_chart_scene(obj)
    if scene is not None:
        source = editable_source(scene) if scene["kind"] == "source" else source_from_program(scene)
        definitions = {table: source[table] for table in SCENE_TABLES}
        for source_order, rule in enumerate(definitions["rules"].values()):
            rule["sourceOrder"] = source_order
        # Compiled identity checks do not create another editable timeline.
        for resource in definitions["resources"].values():
            resource.pop("expectedIdentity", None)
        actions = [validate_action(a) for a in scene.get("actions", scene.get("compiledActions", []))]
        result += (canonical_bytes({"target": source["target"], "definitions": definitions,
                                    "actions": actions}),)
    return result


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
        note_name = (
            "bombarea" if note.get("bombArea")
            else "skyarea")
        lines = [
            f"{note_name}({time_ms},{sc},{sden},{sw},{ec},{eden},{ew},"
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
        mode = flags & 0xFF
        mode_text = ",line" if mode == 1 else ""
        edge_text = "" if edge else ",0"
        return [
            f"setcolor({time_ms},{int(note['line'])},{color}"
            + edge_text
            + mode_text
            + ")"
        ]
    if magic == LINE_MAGIC:
        start_x = int(fractions[0][0])
        start_y = int(fractions[1][0])
        start_x_den = int(fractions[0][1])
        start_y_den = int(fractions[1][1])
        end_x = int(fractions[2][0])
        end_y = int(fractions[3][0])
        end_x_den = int(fractions[2][1])
        end_y_den = int(fractions[3][1])
        ease_names = (
            "s", "si", "so", "sisi", "siso", "sosi", "soso", "b"
        )
        ease = flags & 0xFF
        if ease >= len(ease_names):
            raise ValueError(f"line ease 非法：{ease}")
        return [
            f"line({time_ms},{start_x},{start_y},"
            f"{start_x_den},{start_y_den},"
            f"{end_x},{end_y},{end_x_den},{end_y_den},"
            f"{ease_names[ease]},{int(note['endMs']) - time_ms},"
            f"{int(note['line']) & MASK32})"
        ]
    if magic == ARC_MAGIC:
        start_x = int(fractions[0][0])
        start_y = int(fractions[1][0])
        start_x_den = int(fractions[0][1])
        start_y_den = int(fractions[1][1])
        end_x = int(fractions[2][0])
        end_y = int(fractions[3][0])
        end_x_den = int(fractions[2][1])
        end_y_den = int(fractions[3][1])
        ease_names = (
            "s", "si", "so", "sisi", "siso", "sosi", "soso", "b"
        )
        ease = flags & 0xFF
        if ease >= len(ease_names):
            raise ValueError(f"arc ease 非法：{ease}")
        return [
            f"arc({time_ms},{start_x},{start_y},"
            f"{start_x_den},{start_y_den},"
            f"{end_x},{end_y},{end_x_den},{end_y_den},"
            f"{ease_names[ease]},{int(note['endMs']) - time_ms},"
            f"{int(note['line']) & MASK32})"
        ]
    if magic == FLICK3D_MAGIC:
        center = int(fractions[0][0])
        denominator = int(fractions[0][1])
        width = int(fractions[1][0])
        y = int(fractions[2][0])
        y_denominator = int(fractions[2][1])
        direction = flags & 0xFF
        return [
            f"flick({time_ms},{center},{denominator},{width},"
            f"{direction},{y},{y_denominator})"
        ]
    if magic == BOMBAREA_MAGIC:
        return []
    if magic == TIMINGGROUP_ANGLE_MAGIC:
        return [
            f"timinggroupangles({int(note['line'])},"
            f"{int(fractions[0][0])},{int(fractions[1][0])})"
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
        if mode == SCENE_SCREEN:
            values = [float(pair[0]) / float(pair[1]) for pair in fractions]
            return [serialize_special_screen(
                time_ms, int(note.get("line", 0)), note["endMs"], values, _num)]
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
    scene = validate_chart_scene(obj)
    all_notes = [n for n in obj.get("notes", []) if not is_scene_marker(n)]
    needs_config = False
    notes: list[dict[str, Any]] = []
    for note in all_notes:
        note_type = int(note["type"])
        flags = int(note.get("flags", 0))
        if note_type == 3:
            magic = (flags >> 8) & 0xFFFFFF
            if magic in (ARC_MAGIC, FLICK3D_MAGIC):
                needs_config = True
        if (note_type == 4 and flags & FLICK3D_NATIVE_FLAG) or (
                note_type == 5 and flags & ARC_NATIVE_FLAG):
            continue
        notes.append(note)
    lines: list[str] = []
    if needs_config:
        lines.append("#config enable_3d_skynote TRUE")
    lines.append(f"chart({_num(obj['bpm'])},{_num(obj['meter'])})")
    events = list(obj.get("events", []))

    groups: dict[int, list[dict[str, Any]]] = {}
    group_modes: dict[int, tuple[bool, bool]] = {}
    group_angles: dict[int, tuple[int, int]] = {}
    global_notes: list[dict[str, Any]] = []
    for note in notes:
        note_type = int(note["type"])
        if note_type == 3:
            flags = int(note.get("flags", 0))
            magic = (flags >> 8) & 0xFFFFFF
            if magic == TIMINGGROUP_ANGLE_MAGIC:
                group = int(note["line"])
                group_angles[group] = (
                    int(note["fractions"][0][0]),
                    int(note["fractions"][1][0]),
                )
                continue
            if magic not in (
                    LINE_MAGIC, ARC_MAGIC, FLICK3D_MAGIC):
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
        angle_x, angle_y = group_angles.get(group, (0, 0))
        if angle_x:
            option += f",anglex({angle_x})"
        if angle_y:
            option += f",angley({angle_y})"
        lines.append(f"timinggroup({group}{option}) {{")
        previous_time = None
        previous_bpm = 0.0
        previous_speed = None
        for time_ms, value, bpm in group_speeds.get(group, ((0, 1.0, 0.0),)):
            if time_ms == previous_time:
                raise ValueError(f"timinggroup {group} 在 {time_ms}ms 有不同的同刻状态，现有明文无法无损表达")
            if bpm != previous_bpm:
                if bpm == 0.0:
                    raise ValueError(f"timinggroup {group} 在 {time_ms}ms 将BPM重置为0，现有明文无法无损表达")
                lines.append(f"    bpm({time_ms},{_num(bpm)})")
            if previous_speed is None or value != previous_speed:
                lines.append(f"    track({time_ms},{_num(value)})")
            previous_time, previous_speed, previous_bpm = time_ms, value, bpm
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
    if scene is not None:
        actions = scene.get("actions", scene.get("compiledActions", []))
        for action in sorted(actions, key=lambda a: a["sourceOrder"]):
            lines.append(serialize_action(action))
    return "\n".join(lines) + "\n"
