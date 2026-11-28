
from ascii_log import print_log
import json
import math
import re
from pathlib import Path
from dataclasses import dataclass
from enum import IntEnum

from chart_validation import ChartValidationError, validate_standard_chart
from special_screen import parse_arguments as parse_special_screen
from scene_program import (SCENE_FLAGS, parse_action, read_json as read_scene_json,
                           sidecar_path as scene_sidecar_path, validate_source)
from lyric_statement import (
    TEXT_SHOWTEXT_ID_BASE,
    parse_statement,
    strip_comment,
)


class NoteType(IntEnum):
    TAP = 1
    HOLD = 2
    TRACKMOVE = 3
    FLICK = 4
    SKYAREA = 5

class FlickDirection(IntEnum):
    LEFT = 16
    RIGHT = 4

class SkyAreaEase(IntEnum):
    LINEAR = 0
    SINE_OUT = 1
    SINE_IN = 2

class EventType(IntEnum):
    TRACK = 0
    BPM = 1              # bpm 语句 mode=0：区段型（重置小节相位，携带每小节拍数）
    BPM_TRANSITION = 2   # bpm 语句 mode≠0：过渡型（保持小节相位的变速，demo 的 BpmChangeV3）
    NOINPUT = 2          # 旧版 noinput 的 type2 事件（仅二进制/旧 JSON 兼容；文本解码已改用 skyarea flags）
    LANE = 3

# 新增语句用的标记位（游戏本体在同类型音符上不会读取这些位）
BOMB_FLAG = 0x8000        # tap 上的地雷标记
NOINPUT_FLAG = 0x8000     # skyarea 上的“仅渲染”标记（新表示：落在 note.flags；打包器整体写回 field5）
TRACKMOVE_MAGIC = 0x54524B  # trackmove 容器谱面的标记（存放在 flags 的高 24 位）
TRACKMOVE_DEN = 1000000   # trackmove 坐标的分数分母
SHOWTEXT_MAGIC = 0x545854 # showtext 容器谱面的标记 "TXT"（存放在 flags 的高 24 位）
AUDIOOFFSET_MAGIC = 0x414F46   # "AOF": audiooffset 元数据容器
SCENECONTROL_MAGIC = 0x53434E  # "SCN": scenecontrol 元数据容器
SETCOLOR_MAGIC = 0x434F4C      # "COL": setcolor 元数据容器
LINE_MAGIC = 0x4C494E          # "LIN": line 装饰线容器
ARC_MAGIC = 0x415243           # "ARC": 3D 可判定弧视觉容器
FLICK3D_MAGIC = 0x464C33       # "FL3": 3D flick 视觉容器
BOMBAREA_MAGIC = 0x424D41      # "BMA": bomb SkyArea marker
TIMINGGROUP_ANGLE_MAGIC = 0x544741  # "TGA": timinggroup flick 角度元数据
ARC_NATIVE_FLAG = 0x02000000
FLICK3D_NATIVE_FLAG = 0x01000000
ARC_VISUAL_WIDTH_NUM = 1
ARC_VISUAL_WIDTH_DEN = 10
SCENE_TRACKDISPLAY = 1         # scenecontrol 子模式：轨道透明度
SCENE_HIDEGROUP = 2            # scenecontrol 子模式：隐藏 timinggroup 音符视觉
SCENE_SCREEN = 3               # scenecontrol 子模式：游戏画面效果
SCREEN_EFFECTS = {
    "blur": 1,
    "crt": 2,
    "chromatic": 3,
    "glass": 4,
    "analogglitch": 5,
    "vignette": 6,
    "filmgrain": 7,
    "coloradjust": 8,
    "bloom": 9,
    "clear": 10,
}
SETCOLOR_MODE_SKYAREA = 0      # setcolor 作用于 authored skyarea group
SETCOLOR_MODE_LINE = 1         # setcolor 作用于 line group
LINE_EASES = {
    "s": 0,
    "si": 1,
    "so": 2,
    "sisi": 3,
    "siso": 4,
    "sosi": 5,
    "soso": 6,
    "b": 7,
}
TIMINGGROUP_NOINPUT_BIT = 0x80000000  # timinggroup 高 32 位中的 noinput 标记
TIMINGGROUP_NOCLIP_BIT = 0x40000000   # 过判定线后仍保持视觉的 noinput 变体
TIMINGGROUP_ID_MASK = 0x1FFFFFFF
CONTAINER_FLAG_MAGIC_MASK = 0xFFFFFF00  # 容器 magic 放在 flags 的 8..31 位
CONTAINER_FLAG_MODE_MASK = 0xFF         # 容器低 8 位留给子模式

ARC_NATIVE_EASE = {
    0: 0,  # s
    1: 2,  # si: x=sin
    2: 1,  # so: x=1-cos
    3: 2,  # sisi: x=sin
    4: 2,  # siso: x=sin
    5: 1,  # sosi: x=1-cos
    6: 1,  # soso: x=1-cos
}


def round_ms(value) -> int:
    """Round chart milliseconds with conventional half-up semantics."""
    number = float(value)
    return int(number + 0.5) if number >= 0 else -int(-number + 0.5)



class LaneStatusFlag(IntEnum):
    ON = 256
    OFF = 0


@dataclass
class Note:
    index: int
    group_id: int
    note_type: NoteType
    track_id: int
    time_ms: int
    end_ms: int
    fractions: list[list[int, int], list[int, int], list[int, int], list[int, int]]
    flick: FlickDirection | None = None
    left_ease: SkyAreaEase | None = None
    right_ease: SkyAreaEase | None = None
    flags: int | None = None
    timing_group: int = 0   # timinggroup(n) 状态；转谱期写入 group_id 的高 32 位
    is_bombarea: bool = False

    def to_dict(self) -> dict:
        note_block = {
            "index": self.index,
            "line": self.group_id,
            "type": self.note_type,
            "track": self.track_id,
            "timeMs": self.time_ms,
            "endMs": self.end_ms,
            "fractions": self.fractions
        }
        if (self.note_type == NoteType.FLICK) and self.flick is not None:
            note_block["flick"] = self.flick
        if self.note_type == NoteType.SKYAREA:
            note_block["leftEase"] = self.left_ease
            note_block["rightEase"] = self.right_ease
        if self.is_bombarea:
            note_block["bombArea"] = True
        if self.flags is not None:
            note_block["flags"] = int(self.flags) & 0xFFFFFFFF

        return note_block

@dataclass
class Event:
    index: int
    event_type: EventType
    time_ms: int
    speed_multiplier: float | None = None
    bpm: float | None = None
    beat_amount: float | None = None
    lane_status_flag: int | None = None
    noinput_group: int | None = None
    speed_group: int | None = None    # timinggroup 组内变速（type5）的组号
    group_bpm: float | None = None    # type5 高 32 位：组 BPM（0 表示未指定）
    scope_group: int = 0              # 创建该事件时所在的 timinggroup（0=全局）

    def to_dict(self) -> dict:
        event_block = {
            "id": self.index,
            "type": self.event_type,
            "timeMs": self.time_ms
        }
        if self.event_type == EventType.TRACK or self.event_type == 5:
            event_block["value"] = self.speed_multiplier
        if self.event_type == 5 and self.speed_group is not None:
            event_block["speed"] = float(self.speed_group)
        if self.event_type == 5 and self.group_bpm is not None:
            event_block["groupBpm"] = float(self.group_bpm)
        if self.bpm is not None:
            # type1 区段 / type2 过渡（BpmChangeV3）：bpm + 拍数（文档里的 speed 字段）
            event_block["bpm"] = self.bpm
            event_block["speed"] = self.beat_amount
        if self.event_type == EventType.LANE:
            event_block["flags"] = self.lane_status_flag
        if self.noinput_group is not None:
            # 本 Mod noinput：type2 + time=0 + value2=0，value=天域组号
            event_block["value"] = float(self.noinput_group)

        return event_block



def read_file(file_path):
    payload = Path(file_path).read_bytes()
    failures = []
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError as exc:
            line = payload[:exc.start].count(b"\n") + 1
            failures.append(
                f"{encoding}（第 {line} 行，字节 {exc.start}）：{exc.reason}"
            )
    raise ValueError(
        f"无法以 UTF-8 或 GB18030 读取谱面：{file_path}；" +
        "；".join(failures)
    )

def write_json(file_name: str, obj) -> None:
    with open(file_name, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=4)


def read_spc_v0_chart(file_path, warn=None, scene_manifest=None):
    def _warn(message):
        if warn is not None:
            warn(message)
    content = read_file(file_path)
    pattern = re.compile(r"(\w+)\(((?:[^()]|\([^()]*\))*)\)")
    chart_json = {
        "format": "ICP1",
        "version": 1,
        "bpm": None,
        "meter": None,
        "noteCount": None,
        "eventCount": None,
        "notes": [],
        "events": []
    }
    note_count = 0
    event_count = 0
    parsed_notes: list[Note] = []
    parsed_events: list[Event] = []
    noinput_groups: set[int] = set()   # noinput(group) 收集；解析完成后落到对应 skyarea 的 flags
    current_group = 0                  # timinggroup 状态：后续音符/变速事件归组
    current_group_noinput = False
    current_group_noclip = False
    current_group_angle_x = 0
    current_group_angle_y = 0
    block_group: int | None = None     # 非 None 表示当前处于新式 timinggroup {...} 块
    block_end_token: str | None = None
    declared_block_groups: set[int] = set()
    block_bpm_groups: set[int] = set()
    named_timing_groups: dict[str, int] = {}
    emitted_group_angles: dict[int, tuple[int, int]] = {}
    group_timeline_inputs: list[tuple[int, int, str, float, int]] = []
    source_order = 0
    audiooffset_seen = False
    next_auto_skyarea_group = 0
    text_index = 0
    explicit_showtext_ids: set[int] = set()
    last_v1_skyarea_end: int | None = None
    last_v1_skyarea_group: int | None = None
    config_line_count = 0
    seen_statement = False
    scene_actions = []

    # 新式 timinggroup 的块级规则要求能区分空行/注释和真正的第一条语句。
    def active_line(raw: str) -> str:
        text = strip_comment(raw).strip()
        if not text:
            return ""
        if text.lower().startswith("#config"):
            return text
        if text.startswith("#") or text.startswith("//"):
            return ""
        return text

    def container_note(time_ms: int, group_id: int, end_ms: int,
                       flags: int, fractions=None) -> Note:
        nonlocal note_count
        marker = Note(
            index=note_count,
            group_id=group_id,
            note_type=NoteType.TRACKMOVE,
            track_id=4,
            time_ms=time_ms,
            end_ms=end_ms,
            fractions=fractions or [[0, 1], [0, 1], [0, 1], [0, 1]],
            flags=flags,
        )
        note_count += 1
        parsed_notes.append(marker)
        return marker

    def emit_group_angles(group_id: int, angle_x: int, angle_y: int) -> None:
        previous = emitted_group_angles.get(group_id)
        current = (angle_x, angle_y)
        if previous is not None:
            if previous != current:
                raise ValueError(
                    f"timinggroup({group_id}) 的 anglex/angley 重复定义不一致")
            return
        emitted_group_angles[group_id] = current
        container_note(
            0,
            group_id,
            0,
            TIMINGGROUP_ANGLE_MAGIC << 8,
            fractions=[
                [angle_x, 1],
                [angle_y, 1],
                [0, 1],
                [0, 1],
            ],
        )

    effective_lines = [
        line for raw in content.splitlines()
        if (line := active_line(raw))
    ]

    def split_top_level(text: str) -> list[str]:
        result: list[str] = []
        depth = 0
        start = 0
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                result.append(text[start:index].strip())
                start = index + 1
        result.append(text[start:].strip())
        return result
    first_active = effective_lines[0] if effective_lines else ""
    enable_3d_skynote = False
    chart_line_index = 0
    if first_active.lower().startswith("#config"):
        config_match = re.fullmatch(
            r"#config\s+enable_3d_skynote(?:\s+|\s*=\s*)(TRUE|FALSE)",
            first_active,
            re.IGNORECASE,
        )
        if config_match is None:
            raise ValueError(
                "谱头 #config 必须严格写成 "
                "#config enable_3d_skynote TRUE/FALSE"
            )
        enable_3d_skynote = (
            config_match.group(1).upper() == "TRUE"
        )
        chart_line_index = 1
    if (chart_line_index >= len(effective_lines) or
            not effective_lines[chart_line_index].startswith("chart(")):
        raise ValueError("Invalid SPC v0 chart file")

    # Named timing groups are ordered by first appearance. Reserve a numeric
    # namespace above every explicit numeric group so names never collide.
    numeric_timing_groups: set[int] = set()
    for raw_line in content.splitlines():
        found = pattern.search(active_line(raw_line))
        if not found or found.group(1).lower() != "timinggroup":
            continue
        args = [x.strip() for x in found.group(2).split(",") if x.strip()]
        if not args:
            continue
        try:
            group_id = int(args[0])
        except ValueError:
            continue
        if group_id > 0:
            numeric_timing_groups.add(group_id)
    next_named_timing_group = max(numeric_timing_groups, default=0) + 1

    # audiooffset 是谱面级元数据：先扫描一遍，只接受第一条；随后解析其他语句。
    for raw_line in content.splitlines():
        found = pattern.search(active_line(raw_line))
        if not found or found.group(1) != "audiooffset":
            continue
        args = [x.strip() for x in found.group(2).split(",") if x.strip()]
        if len(args) != 1:
            raise ValueError("audiooffset(timems) 需要且只接受一个参数")
        offset_ms = round_ms(args[0])
        container_note(0, offset_ms, 0, AUDIOOFFSET_MAGIC << 8)
        audiooffset_seen = True
        break

    for line_no, raw_line in enumerate(content.splitlines(), 1):
        line = active_line(raw_line)
        if not line:
            continue
        if line.lower().startswith("#config"):
            config_line_count += 1
            if config_line_count > 1:
                raise ValueError(
                    f"第 {line_no} 行：#config 只能出现一次"
                )
            if seen_statement:
                raise ValueError(
                    f"第 {line_no} 行：#config 必须位于 chart(...) 之前"
                )
            continue
        seen_statement = True
        # text(...) is a standalone lyric statement persisted in a
        # <chart>.lyrics.json sidecar. It never becomes a gameplay note and
        # must not be rejected as an unknown statement by the note parser.
        statement_head = re.split(r"[\(\uFF08]", line, maxsplit=1)[0]
        if statement_head.strip().lower() == "text":
            statement = parse_statement(raw_line, text_index)
            if statement is not None:
                size_override = int(round(
                    statement.option_number("size", 0.0)))
                note = Note(
                    index=note_count,
                    group_id=TEXT_SHOWTEXT_ID_BASE + text_index,
                    note_type=NoteType.TRACKMOVE,
                    track_id=4,
                    time_ms=round_ms(statement.time_ms),
                    end_ms=round_ms(statement.time_ms),
                    fractions=[
                        [max(0, size_override), 1],
                        [0, 1],
                        [0, 1],
                        [0, 1],
                    ],
                    flags=SHOWTEXT_MAGIC << 8,
                )
                note_count += 1
                parsed_notes.append(note)
                text_index += 1
            continue
        if line in ("}", "groupend()", "groupend ()"):
            if block_group is None:
                raise ValueError(f"第 {line_no} 行：多余的 timinggroup 结束标记 {line}")
            expected = "}" if line == "}" else "groupend()"
            if block_end_token != expected:
                raise ValueError(
                    f"第 {line_no} 行：timinggroup 起始/结束标记不匹配"
                    f"（起始为 {block_end_token}，结束为 {expected}）")
            block_group = None
            block_end_token = None
            current_group = 0
            current_group_noinput = False
            current_group_noclip = False
            current_group_angle_x = 0
            current_group_angle_y = 0
            continue
        resolve_stage1 = pattern.search(line)
        if resolve_stage1:
            note_type = resolve_stage1.group(1).lower()
            raw_arguments = resolve_stage1.group(2)
            note_arguments_list = (
                split_top_level(raw_arguments)
                if note_type == "timinggroup"
                else [x.strip() for x in raw_arguments.split(",")]
            )
            note = None
            event = None

            argument_counts = {
                "chart": (2, 2),
                "tap": (3, 3),
                "bomb": (3, 3),
                "hold": (4, 4),
        "flick": (5, 7),
        "arc": (12, 12),
                "skyarea": (10, 11),
                "bombarea": (10, 11),
                "trackmove": (9, 9),
                "noinput": (1, 1),
                "showtext": (2, 3),
                "audiooffset": (1, 1),
                "setcolor": (3, 5),
                "line": (12, 12),
                "scenecontrol": (2, 9),
                "hidegroup": (2, 2),
                "timinggroup": (0, 64),
                "timinggroupangles": (3, 3),
                "lane": (3, 3),
                "bpm": (2, 4),
                "track": (2, 2),
            }
            if note_type in argument_counts:
                minimum, maximum = argument_counts[note_type]
                if (note_type == "scenecontrol" and len(note_arguments_list) > 1
                        and note_arguments_list[1] == "scene"):
                    # New operations validate their own exact arity; old modes
                    # retain their existing parameter limits.
                    minimum, maximum = 5, 12
                actual = (
                    0 if note_arguments_list == [""]
                    else len(note_arguments_list)
                )
                if actual < minimum or actual > maximum:
                    expected = (
                        str(minimum) if minimum == maximum
                        else f"{minimum}..{maximum}"
                    )
                    raise ValueError(
                        f"第 {line_no} 行：{note_type} 需要 "
                        f"{expected} 个参数，实际 {actual} 个"
                    )

            if block_group is not None:
                if note_type == "timinggroup":
                    raise ValueError(f"第 {line_no} 行：timinggroup 块不允许嵌套")
                if note_type == "bpm":
                    block_bpm_groups.add(block_group)

            match note_type:
                case "chart":
                    chart_json["bpm"] = float(note_arguments_list[0])
                    chart_json["meter"] = float(note_arguments_list[1])
                case "groupend":
                    raise ValueError(
                        f"第 {line_no} 行：groupend() 必须单独闭合 timinggroup")
                case "tap":
                    tap_time = round_ms(note_arguments_list[0])
                    tap_width = int(note_arguments_list[1])
                    tap_lane = int(note_arguments_list[2])
                    if not 0 <= tap_lane <= 5:
                        raise ValueError(
                            f"第 {line_no} 行：tap lane 必须在 0..5")
                    if tap_width < 0:
                        raise ValueError(
                            f"第 {line_no} 行：tap width 不能为负数")
                    note = Note(
                        index = note_count,
                        group_id = 0,
                        note_type = NoteType.TAP,
                        track_id = 1 if tap_lane in [1,2,3,4] else (
                            2 if tap_lane == 0 else 3
                        ),
                        time_ms = tap_time,
                        end_ms = tap_time,
                        fractions = [
                            [tap_lane, 4],
                            [tap_lane, 4],
                            [tap_width, 4],
                            [tap_width, 4]
                        ]
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "bomb":
                    bomb_time = round_ms(note_arguments_list[0])
                    bomb_width = int(note_arguments_list[1])
                    bomb_lane = int(note_arguments_list[2])
                    if not 0 <= bomb_lane <= 5:
                        raise ValueError(
                            f"第 {line_no} 行：bomb lane 必须在 0..5")
                    if bomb_width < 0:
                        raise ValueError(
                            f"第 {line_no} 行：bomb width 不能为负数")
                    note = Note(
                        index = note_count,
                        group_id = 0,
                        note_type = NoteType.TAP,
                        track_id = 1 if bomb_lane in [1,2,3,4] else (
                            2 if bomb_lane == 0 else 3
                        ),
                        time_ms = bomb_time,
                        end_ms = bomb_time,
                        fractions = [
                            [bomb_lane, 4],
                            [bomb_lane, 4],
                            [bomb_width, 4],
                            [bomb_width, 4]
                        ],
                        flags = BOMB_FLAG
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "hold":
                    hold_start = round_ms(note_arguments_list[0])
                    hold_duration = round_ms(note_arguments_list[3])
                    hold_lane = int(note_arguments_list[1])
                    hold_width = int(note_arguments_list[2])
                    if not 0 <= hold_lane <= 5:
                        raise ValueError(
                            f"第 {line_no} 行：hold lane 必须在 0..5")
                    if hold_width < 0:
                        raise ValueError(
                            f"第 {line_no} 行：hold width 不能为负数")
                    if hold_duration < 0:
                        raise ValueError(
                            f"第 {line_no} 行：hold duration 不能为负数")
                    note = Note(
                        index = note_count,
                        group_id = 0,
                        note_type = NoteType.HOLD,
                        track_id = 1 if hold_lane in [1,2,3,4] else (
                            2 if hold_lane == 0 else 3
                        ),
                        time_ms = hold_start,
                        end_ms = hold_start + hold_duration,
                        fractions = [
                            [hold_lane, 4],
                            [hold_lane, 4],
                            [hold_width, 4],
                            [hold_width, 4]
                        ]
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "flick":
                    flick_time = round_ms(note_arguments_list[0])
                    flick_center = int(note_arguments_list[1])
                    flick_den = int(note_arguments_list[2])
                    flick_width = int(note_arguments_list[3])
                    flick_direction = int(note_arguments_list[4])
                    if len(note_arguments_list) not in (5, 7):
                        raise ValueError(
                            f"第 {line_no} 行：flick 必须使用原版 5 参数，"
                            "或由 enable_3d_skynote 启用的 7 参数形式"
                        )
                    if flick_den <= 0:
                        raise ValueError(
                            f"第 {line_no} 行：flick denominator 必须大于 0")
                    if flick_direction not in (4, 16):
                        raise ValueError(
                            f"第 {line_no} 行：flick direction 只能是右 4 或左 16")
                    if len(note_arguments_list) == 7:
                        if not enable_3d_skynote:
                            raise ValueError(
                                f"第 {line_no} 行：7 参数 flick 需要谱头 "
                                "#config enable_3d_skynote TRUE"
                            )
                        flick_y = int(round(float(note_arguments_list[5])))
                        flick_y_den = int(note_arguments_list[6])
                        if flick_y_den <= 0:
                            raise ValueError(
                                f"第 {line_no} 行：flick y_den 必须大于 0"
                            )
                        packed_timing_group = current_group
                        if current_group_noinput:
                            packed_timing_group |= TIMINGGROUP_NOINPUT_BIT
                        if current_group_noclip:
                            packed_timing_group |= TIMINGGROUP_NOCLIP_BIT
                        flick_visual = Note(
                            index=note_count,
                            group_id=(
                                (packed_timing_group & 0xFFFFFFFF) << 32
                            ),
                            note_type=NoteType.TRACKMOVE,
                            track_id=4,
                            time_ms=flick_time,
                            end_ms=flick_time,
                            fractions=[
                                [flick_center, flick_den],
                                [flick_width, flick_den],
                                [flick_y, flick_y_den],
                                [0, 1],
                            ],
                            flags=(
                                (FLICK3D_MAGIC << 8)
                                | flick_direction
                            ),
                        )
                        note_count += 1
                        parsed_notes.append(flick_visual)
                    note = Note(
                        index = note_count,
                        group_id = 0,
                        note_type = NoteType.FLICK,
                        track_id = 4,
                        time_ms = flick_time,
                        end_ms = flick_time,
                        fractions = [
                            [flick_center, flick_den],
                            [flick_center, flick_den],
                            [flick_width, flick_den],
                            [flick_width, flick_den]
                        ],
                        flick = flick_direction,
                        flags=(
                            ((flick_direction << 8) | FLICK3D_NATIVE_FLAG)
                            if len(note_arguments_list) == 7 else None
                        ),
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "arc":
                    if not enable_3d_skynote:
                        raise ValueError(
                            f"第 {line_no} 行：arc 需要谱头 "
                            "#config enable_3d_skynote TRUE"
                        )
                    arc_time = round_ms(note_arguments_list[0])
                    start_x = int(round(float(note_arguments_list[1])))
                    start_y = int(round(float(note_arguments_list[2])))
                    start_x_den = int(note_arguments_list[3])
                    start_y_den = int(note_arguments_list[4])
                    end_x = int(round(float(note_arguments_list[5])))
                    end_y = int(round(float(note_arguments_list[6])))
                    end_x_den = int(note_arguments_list[7])
                    end_y_den = int(note_arguments_list[8])
                    ease_text = str(note_arguments_list[9]).strip().lower()
                    duration = round_ms(note_arguments_list[10])
                    arc_group = int(note_arguments_list[11])
                    if min(start_x_den, start_y_den,
                           end_x_den, end_y_den) <= 0:
                        raise ValueError(
                            f"第 {line_no} 行：arc 坐标分母必须大于 0"
                        )
                    if ease_text not in LINE_EASES:
                        raise ValueError(
                            f"第 {line_no} 行：arc ease 必须是 "
                            "s/si/so/sisi/siso/sosi/soso/b"
                        )
                    if duration < 0:
                        raise ValueError(
                            f"第 {line_no} 行：arc duration 不能为负数"
                        )
                    if not 0 <= arc_group <= 0xFFFFFFFF:
                        raise ValueError(
                            f"第 {line_no} 行：arc groupId 越界"
                        )
                    effective_duration = 2 if duration == 1 else duration
                    packed_timing_group = current_group
                    if current_group_noinput:
                        packed_timing_group |= TIMINGGROUP_NOINPUT_BIT
                    if current_group_noclip:
                        packed_timing_group |= TIMINGGROUP_NOCLIP_BIT
                    packed_group = (
                        (packed_timing_group & 0xFFFFFFFF) << 32
                    ) | (arc_group & 0xFFFFFFFF)
                    arc_visual = Note(
                        index=note_count,
                        group_id=packed_group,
                        note_type=NoteType.TRACKMOVE,
                        track_id=4,
                        time_ms=arc_time,
                        end_ms=arc_time + effective_duration,
                        fractions=[
                            [start_x, start_x_den],
                            [start_y, start_y_den],
                            [end_x, end_x_den],
                            [end_y, end_y_den],
                        ],
                        flags=(ARC_MAGIC << 8) | LINE_EASES[ease_text],
                    )
                    note_count += 1
                    parsed_notes.append(arc_visual)

                    # 0ms 是纯视觉弧。noinput/noclip 组也只保留视觉。
                    if effective_duration > 0 and not current_group_noinput:
                        pair = LINE_EASES[ease_text]
                        if pair == 7:
                            if effective_duration < 4:
                                raise ValueError(
                                    f"第 {line_no} 行：可判定 b arc "
                                    "需要至少 4ms，才能生成两个原生判定段"
                                )
                            mid_time = (
                                arc_time + (effective_duration + 1) // 2
                            )
                            mid_x_num = (
                                start_x * end_x_den
                                + end_x * start_x_den
                            )
                            mid_x_den = 2 * start_x_den * end_x_den
                            factor = math.gcd(
                                abs(mid_x_num), mid_x_den)
                            mid_x_num //= factor
                            mid_x_den //= factor
                            arc_segments = [
                                (arc_time, mid_time, start_x,
                                 start_x_den, mid_x_num, mid_x_den, 1),
                                (mid_time, arc_time + effective_duration,
                                 mid_x_num, mid_x_den, end_x, end_x_den, 2),
                            ]
                        else:
                            native_ease = ARC_NATIVE_EASE[pair]
                            arc_segments = [
                                (arc_time, arc_time + effective_duration,
                                 start_x, start_x_den, end_x, end_x_den,
                                 native_ease),
                            ]
                        for seg_start, seg_end, sx, sx_den, ex, ex_den, native_ease in arc_segments:
                            if seg_end <= seg_start:
                                continue
                            left_ease_bits = (0x04, 0x08, 0x10)[
                                native_ease
                            ]
                            right_ease_bits = (0x20, 0x40, 0x80)[
                                native_ease
                            ]
                            native_arc = Note(
                                index=note_count,
                                group_id=arc_group,
                                note_type=NoteType.SKYAREA,
                                track_id=4,
                                time_ms=seg_start,
                                end_ms=seg_end,
                                fractions=[
                                    [sx, sx_den],
                                    [ex, ex_den],
                                    [ARC_VISUAL_WIDTH_NUM,
                                     ARC_VISUAL_WIDTH_DEN],
                                    [ARC_VISUAL_WIDTH_NUM,
                                     ARC_VISUAL_WIDTH_DEN],
                                ],
                                left_ease=native_ease,
                                right_ease=native_ease,
                                flags=(
                                    ARC_NATIVE_FLAG
                                    | left_ease_bits
                                    | right_ease_bits
                                ),
                            )
                            note_count += 1
                            parsed_notes.append(native_arc)
                case "skyarea" | "bombarea":
                    sky_start = round_ms(note_arguments_list[0])
                    sky_duration = round_ms(note_arguments_list[9])
                    sky_numbers = [
                        int(x) for i, x in enumerate(note_arguments_list)
                        if i not in (0, 9)
                    ]
                    explicit_group: int | None = None
                    if len(sky_numbers) == 9:
                        explicit_group = int(note_arguments_list[10])
                        # InFalsus Studio writes -1 for an unassigned
                        # SkyArea. It is semantic sugar for the 10-argument
                        # automatic-group form, not a native group ID.
                        if explicit_group == -1:
                            explicit_group = None
                    if len(sky_numbers) == 8 or explicit_group is None:
                        # 原版 SkyAreaV1（10 参数）没有显式 groupId。
                        # 引擎的规则是：新天域起点与上一段 V1 天域终点相差 <=2ms
                        # 时沿用组号，否则分配下一个自动组号。
                        start_ms = sky_start
                        end_ms = start_ms + sky_duration
                        if (last_v1_skyarea_end is not None and
                                abs(start_ms - last_v1_skyarea_end) <= 2):
                            group_id = last_v1_skyarea_group
                        else:
                            group_id = next_auto_skyarea_group
                            next_auto_skyarea_group += 1
                        last_v1_skyarea_end = end_ms
                        last_v1_skyarea_group = group_id
                    elif len(sky_numbers) == 9:
                        group_id = explicit_group
                        if not 0 <= group_id <= 0xFFFFFFFF:
                            raise ValueError(
                                f"第 {line_no} 行：skyarea groupId 越界")
                        next_auto_skyarea_group = max(
                            next_auto_skyarea_group, group_id + 1)
                    else:
                        raise ValueError(
                            "skyarea 需要 10 个参数（V1 自动分组）或 11 个参数（V2 显式组号）")
                    if sky_numbers[1] <= 0 or sky_numbers[4] <= 0:
                        raise ValueError(
                            f"第 {line_no} 行：skyarea denominator 必须大于 0")
                    if sky_numbers[6] not in (0, 1, 2) or sky_numbers[7] not in (0, 1, 2):
                        raise ValueError(
                            f"第 {line_no} 行：skyarea ease 必须在 0..2")
                    if sky_duration < 0:
                        raise ValueError(
                            f"第 {line_no} 行：skyarea duration 不能为负数")
                    note = Note(
                        index = note_count,
                        group_id = group_id,
                        note_type = NoteType.SKYAREA,
                        track_id = 4,
                        time_ms = sky_start,
                        end_ms = sky_start + sky_duration,
                        fractions = [
                            [sky_numbers[0], sky_numbers[1]],
                            [sky_numbers[3], sky_numbers[4]],
                            [sky_numbers[2], sky_numbers[1]],
                            [sky_numbers[5], sky_numbers[4]]
                        ],
                        left_ease = sky_numbers[6],
                        right_ease = sky_numbers[7]
                    )
                    note_count += 1
                    parsed_notes.append(note)
                    if note_type == "bombarea":
                        note.is_bombarea = True
                case "trackmove":
                    time_ms = round_ms(note_arguments_list[0])
                    x = float(note_arguments_list[1])
                    y = float(note_arguments_list[2])
                    z = float(note_arguments_list[3])
                    xoz = float(note_arguments_list[4])
                    yoz = float(note_arguments_list[5])
                    xoy = float(note_arguments_list[6])
                    ease = int(note_arguments_list[7])
                    duration = round_ms(note_arguments_list[8])
                    if ease not in (0, 1, 2, 3):
                        raise ValueError(
                            f"第 {line_no} 行：trackmove ease 必须在 0..3")
                    if duration < 0:
                        raise ValueError(
                            f"第 {line_no} 行：trackmove duration 不能为负数")
                    def _q(value: float) -> int:
                        return int(round(value * TRACKMOVE_DEN))
                    yoz_milli = int(round(yoz * 1000)) & 0xFFFFFFFF
                    xoy_milli = int(round(xoy * 1000)) & 0xFFFFFFFF
                    group = yoz_milli | (xoy_milli << 32)
                    if group >= (1 << 63):
                        group -= 1 << 64
                    note = Note(
                        index = note_count,
                        group_id = group,
                        note_type = NoteType.TRACKMOVE,
                        track_id = 4,
                        time_ms = time_ms,
                        end_ms = time_ms + duration,
                        fractions = [
                            [_q(x), TRACKMOVE_DEN],
                            [_q(y), TRACKMOVE_DEN],
                            [_q(z), TRACKMOVE_DEN],
                            [_q(xoz), TRACKMOVE_DEN]
                        ],
                        flags = (TRACKMOVE_MAGIC << 8) | (ease & 0xFF)
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "noinput":
                    # 迁移（2026-10-12）：不再产生 type2 事件。先记下组号，
                    # 待全部 note 解析完，给对应 group 的 skyarea 打上 flags 0x8000。
                    noinput_group = int(note_arguments_list[0])
                    if not 0 <= noinput_group <= 0xFFFFFFFF:
                        raise ValueError(
                            f"第 {line_no} 行：noinput groupId 越界")
                    noinput_groups.add(noinput_group)
                case "skyareapack":
                    raise ValueError(
                        f"第 {line_no} 行：skyareapack 是已永久停用的手动语法；"
                        "相连 SkyArea 由运行期按几何隐式分类")
                case "packpiece":
                    raise ValueError(
                        f"第 {line_no} 行：packpiece 是已永久停用的手动语法；"
                        "相连 SkyArea 由运行期按几何隐式分类")
                case "showtext":
                    # showtext(time_ms, id[, size])：文本内容与样式放在与谱面同级的
                    # <谱名>.text.json（运行时读取）；这里只产生一个 type3 容器音符，
                    # 由 Mod 转谱期剥离并锁存 (time, id)。
                    t = round_ms(note_arguments_list[0])
                    sid = int(note_arguments_list[1])
                    explicit_showtext_ids.add(sid)
                    size_override = int(note_arguments_list[2]) if len(note_arguments_list) > 2 else 0
                    note = Note(
                        index = note_count,
                        group_id = sid,
                        note_type = NoteType.TRACKMOVE,
                        track_id = 4,
                        time_ms = t,
                        end_ms = t,
                        fractions = [[max(0, size_override), 1], [0, 1], [0, 1], [0, 1]],
                        flags = (SHOWTEXT_MAGIC << 8)
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "setcolor":
                    # setcolor(time, group, #RRGGBB[AA])
                    # setcolor(time, group, #RRGGBB[AA], line|skyarea)
                    # setcolor(time, group, #RRGGBB[AA], edgeGlow, line|skyarea)
                    t = round_ms(note_arguments_list[0])
                    sky_group = int(note_arguments_list[1])
                    if not 0 <= sky_group <= 0xFFFFFFFF:
                        raise ValueError(
                            f"第 {line_no} 行：setcolor groupId 越界")
                    hex_text = note_arguments_list[2].strip().lstrip("#")
                    edge_glow = 1
                    mode = SETCOLOR_MODE_SKYAREA

                    def parse_mode(text: str) -> int:
                        value = str(text).strip().lower()
                        if value == "skyarea":
                            return SETCOLOR_MODE_SKYAREA
                        if value == "line":
                            return SETCOLOR_MODE_LINE
                        raise ValueError(
                            "setcolor mode 必须是 line 或 skyarea")

                    if len(note_arguments_list) == 4:
                        fourth = str(note_arguments_list[3]).strip().lower()
                        if fourth in ("line", "skyarea"):
                            mode = parse_mode(fourth)
                        else:
                            edge_text = fourth
                            if edge_text in ("1", "true", "yes", "on"):
                                edge_glow = 1
                            elif edge_text in ("0", "false", "no", "off"):
                                edge_glow = 0
                            else:
                                raise ValueError(
                                    "setcolor 第四个参数必须是 edgeGlow 或 mode")
                    elif len(note_arguments_list) == 5:
                        edge_text = str(note_arguments_list[3]).strip().lower()
                        if edge_text in ("1", "true", "yes", "on"):
                            edge_glow = 1
                        elif edge_text in ("0", "false", "no", "off"):
                            edge_glow = 0
                        else:
                            raise ValueError(
                                "setcolor edgeGlow 必须是 0/1 或 true/false")
                        mode = parse_mode(note_arguments_list[4])
                    if len(hex_text) == 6:
                        hex_text += "FF"
                    if len(hex_text) != 8:
                        raise ValueError("setcolor 颜色必须是 #RRGGBB 或 #RRGGBBAA")
                    try:
                        rgba = int(hex_text, 16)
                    except ValueError as exc:
                        raise ValueError("setcolor 颜色包含非十六进制字符") from exc
                    note = Note(
                        index=note_count,
                        group_id=sky_group,
                        note_type=NoteType.TRACKMOVE,
                        track_id=4,
                        time_ms=t,
                        end_ms=rgba,
                        fractions=[[edge_glow, 1], [0, 1], [0, 1], [0, 1]],
                        flags=(SETCOLOR_MAGIC << 8) | mode,
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "line":
                    # line(time_ms, start_x, start_y, start_x_den,
                    #      start_y_den, end_x, end_y, end_x_den,
                    #      end_y_den, ease, duration, groupId)
                    t = round_ms(note_arguments_list[0])
                    start_x = int(round(float(note_arguments_list[1])))
                    start_y = int(round(float(note_arguments_list[2])))
                    start_x_den = int(note_arguments_list[3])
                    start_y_den = int(note_arguments_list[4])
                    end_x = int(round(float(note_arguments_list[5])))
                    end_y = int(round(float(note_arguments_list[6])))
                    end_x_den = int(note_arguments_list[7])
                    end_y_den = int(note_arguments_list[8])
                    if min(start_x_den, start_y_den,
                           end_x_den, end_y_den) <= 0:
                        raise ValueError(
                            f"第 {line_no} 行：line 坐标分母必须大于 0")
                    ease_text = str(note_arguments_list[9]).strip().lower()
                    duration = round_ms(note_arguments_list[10])
                    if duration < 0:
                        raise ValueError(
                            f"第 {line_no} 行：line duration 不能为负数")
                    if ease_text == "":
                        ease_text = "s"
                    if ease_text not in LINE_EASES:
                        raise ValueError(
                            f"第 {line_no} 行：line ease 必须是 "
                            "s/si/so/sisi/siso/sosi/soso/b")
                    line_group = int(note_arguments_list[11])
                    if not 0 <= line_group <= 0xFFFFFFFF:
                        raise ValueError(
                            f"第 {line_no} 行：line groupId 越界")
                    packed_timing_group = current_group
                    if current_group_noinput:
                        packed_timing_group |= TIMINGGROUP_NOINPUT_BIT
                    if current_group_noclip:
                        packed_timing_group |= TIMINGGROUP_NOCLIP_BIT
                    packed_group = (
                        (packed_timing_group & 0xFFFFFFFF) << 32
                    ) | (line_group & 0xFFFFFFFF)
                    note = Note(
                        index=note_count,
                        group_id=packed_group,
                        note_type=NoteType.TRACKMOVE,
                        track_id=4,
                        time_ms=t,
                        end_ms=t + duration,
                        fractions=[
                            [start_x, start_x_den],
                            [start_y, start_y_den],
                            [end_x, end_x_den],
                            [end_y, end_y_den],
                        ],
                        flags=(LINE_MAGIC << 8) | LINE_EASES[ease_text],
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "scenecontrol":
                    if len(note_arguments_list) < 2:
                        raise ValueError("scenecontrol(timems,mode,*param) 参数不足")
                    t = round_ms(note_arguments_list[0])
                    mode_name = note_arguments_list[1].strip().lower()
                    if mode_name == "scene":
                        action = parse_action(note_arguments_list,
                                              len(scene_actions) + 1,
                                              len(scene_actions))
                        container_note(action["markerTimeMs"], action["id"], 0,
                                       SCENE_FLAGS)
                        scene_actions.append(action)
                        continue
                    if mode_name == "trackdisplay":
                        if len(note_arguments_list) != 4:
                            raise ValueError(
                                "scenecontrol(...,trackdisplay,durationMs,alpha) 需要四个参数"
                            )
                        duration = round_ms(note_arguments_list[2])
                        alpha = int(note_arguments_list[3])
                        if duration < 0 or not 0 <= alpha <= 255:
                            raise ValueError("trackdisplay 的 duration 必须 >=0，alpha 必须在 0..255")
                        note = Note(
                            index=note_count,
                            group_id=0,
                            note_type=NoteType.TRACKMOVE,
                            track_id=4,
                            time_ms=t,
                            end_ms=duration,
                            fractions=[[alpha, 255], [0, 1], [0, 1], [0, 1]],
                            flags=(SCENECONTROL_MAGIC << 8) | SCENE_TRACKDISPLAY,
                        )
                    elif mode_name == "hidegroup":
                        if len(note_arguments_list) != 4:
                            raise ValueError(
                                "scenecontrol(...,hidegroup,groupId,flag) 需要四个参数"
                            )
                        group_id = int(note_arguments_list[2])
                        flag = int(note_arguments_list[3])
                        if group_id < 0 or flag not in (0, 1):
                            raise ValueError("hidegroup 的 groupId 必须 >=0，flag 必须为 0 或 1")
                        note = Note(
                            index=note_count,
                            group_id=group_id,
                            note_type=NoteType.TRACKMOVE,
                            track_id=4,
                            time_ms=t,
                            end_ms=flag,
                            fractions=[[0, 1], [0, 1], [0, 1], [0, 1]],
                            flags=(SCENECONTROL_MAGIC << 8) | SCENE_HIDEGROUP,
                        )
                    elif mode_name == "screen":
                        packed, pairs, duration = parse_special_screen(
                            note_arguments_list, round_ms)
                        note = Note(
                            index=note_count,
                            group_id=packed,
                            note_type=NoteType.TRACKMOVE,
                            track_id=4,
                            time_ms=t,
                            end_ms=duration,
                            fractions=pairs,
                            flags=(SCENECONTROL_MAGIC << 8) | SCENE_SCREEN,
                        )
                    else:
                        raise ValueError(f"未知 scenecontrol mode: {mode_name}")
                    note_count += 1
                    parsed_notes.append(note)
                case "hidegroup":
                    if len(note_arguments_list) != 2:
                        raise ValueError("hidegroup(timems,flag) 需要两个参数")
                    t = round_ms(note_arguments_list[0])
                    flag = int(note_arguments_list[1])
                    if flag not in (0, 1):
                        raise ValueError("hidegroup 的 flag 必须为 0 或 1")
                    note = Note(
                        index=note_count,
                        group_id=current_group,
                        note_type=NoteType.TRACKMOVE,
                        track_id=4,
                        time_ms=t,
                        end_ms=flag,
                        fractions=[[0, 1], [0, 1], [0, 1], [0, 1]],
                        flags=(SCENECONTROL_MAGIC << 8) | SCENE_HIDEGROUP,
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "audiooffset":
                    # 第一遍预扫描已经锁存第一条；后续重复项按用户约定忽略。
                    if not audiooffset_seen:
                        offset_ms = int(round(float(note_arguments_list[0])))
                        container_note(0, offset_ms, 0, AUDIOOFFSET_MAGIC << 8)
                        audiooffset_seen = True
                case "skynote":
                    raise ValueError(
                        "skynote 已停用；当前版本不会为该语句生成或保留原生载荷"
                    )
                case "timinggroup":
                    # 块式：
                    #   timinggroup(n,noinput=true) { ... }
                    #   timinggroup(n,noclip=true) { ... }
                    #   timinggroup(n,anglex(500),angley(100)) { ... }
                    #   timinggroup(name,noinput): ... groupend()
                    # 旧式 timinggroup(n) 仍保留兼容；timinggroup() 复位。
                    if "{" in line or line.rstrip().endswith(":"):
                        if not note_arguments_list:
                            raise ValueError(
                                f"第 {line_no} 行：块状 timinggroup 必须提供 groupId"
                            )
                        raw_group = note_arguments_list[0]
                        try:
                            group_id = int(raw_group)
                        except ValueError:
                            if raw_group not in named_timing_groups:
                                named_timing_groups[raw_group] = next_named_timing_group
                                next_named_timing_group += 1
                            group_id = named_timing_groups[raw_group]
                        if not 1 <= group_id <= TIMINGGROUP_ID_MASK:
                            raise ValueError(
                                f"第 {line_no} 行：timinggroup groupId 越界")
                        declared_block_groups.add(group_id)
                        noinput = False
                        noclip = False
                        angle_x = 0
                        angle_y = 0
                        for raw_option in note_arguments_list[1:]:
                            option = raw_option.replace(" ", "").lower()
                            if option == "noinput":
                                noinput = True
                                continue
                            if option == "noclip":
                                noclip = True
                                noinput = True
                                continue
                            angle_match = re.fullmatch(
                                r"(anglex|angley)\(([+-]?\d+)\)", option)
                            if angle_match:
                                angle_value = int(angle_match.group(2))
                                if not -(1 << 31) <= angle_value < (1 << 31):
                                    raise ValueError(
                                        f"第 {line_no} 行：{angle_match.group(1)} 角度越界")
                                if angle_match.group(1) == "anglex":
                                    angle_x = angle_value
                                else:
                                    angle_y = angle_value
                                continue
                            if "=" in option:
                                key, value = option.split("=", 1)
                                if key not in ("noinput", "noclip") or value not in ("true", "false"):
                                    raise ValueError(
                                        f"第 {line_no} 行：未知 timinggroup 选项 {raw_option}"
                                    )
                                if key == "noclip":
                                    noclip = value == "true"
                                    noinput = noclip
                                else:
                                    noinput = value == "true"
                                continue
                            raise ValueError(
                                f"第 {line_no} 行：未知 timinggroup 选项 {raw_option}")
                        current_group = group_id
                        current_group_noinput = noinput
                        current_group_noclip = noclip
                        current_group_angle_x = angle_x
                        current_group_angle_y = angle_y
                        if angle_x or angle_y:
                            emit_group_angles(group_id, angle_x, angle_y)
                        block_group = group_id
                        block_end_token = "}" if "{" in line else "groupend()"
                    elif note_arguments_list and note_arguments_list[0] != "":
                        current_group = int(note_arguments_list[0])
                        if not 0 <= current_group <= TIMINGGROUP_ID_MASK:
                            raise ValueError(
                                f"第 {line_no} 行：timinggroup groupId 越界")
                        current_group_noinput = False
                        current_group_noclip = False
                        current_group_angle_x = 0
                        current_group_angle_y = 0
                    else:
                        current_group = 0
                        current_group_noinput = False
                        current_group_noclip = False
                        current_group_angle_x = 0
                        current_group_angle_y = 0
                case "timinggroupangles":
                    # Metadata is scoped to the following timinggroup block.
                    # Keep current_group unchanged; the owning block supplies
                    # its own group id when each note is emitted.
                    if len(note_arguments_list) != 3:
                        raise ValueError(
                            f"第 {line_no} 行：timinggroupangles 需要 "
                            "groupId, anglex, angley")
                    group_id = int(note_arguments_list[0])
                    angle_x = int(note_arguments_list[1])
                    angle_y = int(note_arguments_list[2])
                    if not 1 <= group_id <= TIMINGGROUP_ID_MASK:
                        raise ValueError(
                            f"第 {line_no} 行：timinggroupangles groupId 越界")
                    if block_group is None or block_group != group_id:
                        raise ValueError(
                            f"第 {line_no} 行：timinggroupangles({group_id}) "
                            "必须位于对应 timinggroup 块内")
                    current_group_angle_x = angle_x
                    current_group_angle_y = angle_y
                    emit_group_angles(group_id, angle_x, angle_y)
                case "lane":
                    lane_index = int(note_arguments_list[1])
                    enabled = int(note_arguments_list[2])
                    if not 0 <= lane_index <= 5:
                        raise ValueError(
                            f"第 {line_no} 行：lane index 必须在 0..5")
                    if enabled not in (0, 1):
                        raise ValueError(
                            f"第 {line_no} 行：lane enabled 必须为 0 或 1")
                    event = Event(
                        index = event_count,
                        event_type = EventType.LANE,
                        time_ms = round_ms(note_arguments_list[0]),
                        lane_status_flag = lane_index + (256 if enabled else 0)
                    )
                    event_count += 1
                    parsed_events.append(event)
                case "bpm":
                    # 与 demo 文本格式一致（BpmChangeV1/V2/V3 三个变体）：
                    #   bpm(t, bpm)                  beat_unit 缺省 4.0（V1）
                    #   bpm(t, bpm, beat_unit)       mode 缺省 0（V2）
                    #   bpm(t, bpm, beat_unit, mode) mode==0 区段型 / mode!=0 过渡型（V3）
                    # 区段型 → 事件 type1；过渡型 → 事件 type2（与 noinput 共用 type2，
                    # 按 time=0 且 value2=0 区分，且过渡型不参与“剔除/重编号”处理）。
                    time_ms = round_ms(note_arguments_list[0])
                    bpm_value = float(note_arguments_list[1])
                    beat_unit = float(note_arguments_list[2]) if len(note_arguments_list) > 2 else 4.0
                    mode = int(float(note_arguments_list[3])) if len(note_arguments_list) > 3 else 0
                    # 全局 bpm 必须为正；timinggroup 内允许负值（演出），
                    # 由 Mod 在密度侧取绝对值处理。
                    if current_group == 0 and bpm_value == 0:
                        raise ValueError("bpm 数值必须大于 0")
                    if current_group != 0 and bpm_value == 0:
                        raise ValueError("timinggroup 内 bpm 不能为 0")
                    if current_group != 0:
                        # 组内 BPM 是该组的基础视觉流速：相对 chart() 的头部 BPM
                        # 计算基础倍率。它不进入全局 BPM 事件，因此不影响小节线。
                        group_timeline_inputs.append(
                            (time_ms, current_group, "base", bpm_value, source_order)
                        )
                        source_order += 1
                    else:
                        event = Event(
                            index = event_count,
                            event_type = (EventType.BPM if mode == 0 else EventType.BPM_TRANSITION),
                            time_ms = time_ms,
                            bpm = bpm_value,
                            beat_amount = beat_unit,
                            scope_group = current_group,
                        )
                        event_count += 1
                        parsed_events.append(event)
                case "track":
                    if current_group != 0:
                        # 先记录基础 BPM 与 track 两类输入；循环结束后合成每一时刻
                        # 的“有效组流速”，底层仍沿用现有 type5 GroupSpeed 格式。
                        group_timeline_inputs.append(
                            (
                                round_ms(note_arguments_list[0]),
                                current_group,
                                "track",
                                float(note_arguments_list[1]),
                                source_order,
                            )
                        )
                        source_order += 1
                    else:
                        event = Event(
                            index = event_count,
                            event_type = EventType.TRACK,
                            time_ms = round_ms(note_arguments_list[0]),
                            speed_multiplier = float(note_arguments_list[1]),
                            scope_group = current_group,
                        )
                    if event is not None:
                        event_count += 1
                        parsed_events.append(event)
                case _:
                    raise ValueError(
                        f"第 {line_no} 行：未知或不支持的 SPC 语句：{note_type}"
                    )

            # timinggroup 状态跟随语句顺序：当前行生成 note 时记录所属组。
            if note is not None and note.note_type != NoteType.TRACKMOVE:
                note.timing_group = current_group
                if current_group_noinput:
                    note.timing_group |= TIMINGGROUP_NOINPUT_BIT
                if current_group_noclip:
                    note.timing_group |= TIMINGGROUP_NOCLIP_BIT

    if block_group is not None:
        closer = "groupend()" if block_end_token == "groupend()" else "}"
        raise ValueError(f"timinggroup({block_group}) 缺少闭合的 {closer}")

    # L1 硬校验：timinggroup 内的 track()/bpm() 只允许产生 type5，
    # 任何落到全局 type0/1/2 的事件都是越界，直接报错。
    for event in parsed_events:
        if event.event_type in (0, 1, 2) and event.scope_group != 0:
            raise ValueError(
                f"timinggroup({event.scope_group}) 的语句产生了全局事件 "
                f"type={int(event.event_type)} time={event.time_ms}ms"
            )

    # timinggroup 的组内 bpm 与 track 最终合成为现有的 type5 GroupSpeed 事件。
    # 底层 C# 运行期仍只需消费“某一时刻之后的组有效流速”，无需新增事件类型。
    chart_base_bpm = float(chart_json["bpm"] or 0.0)
    if group_timeline_inputs and chart_base_bpm <= 0:
        raise ValueError("timinggroup 使用 bpm 时，chart() 头部 BPM 必须大于 0")
    # 组基线规则（用户定义）：
    # - 组内有 track 语句：时间顺序上的第一条 track 就是该组的起点，
    #   在该时刻之前不允许出现任何音符；出现即报错；
    # - 组内没有 track 语句：t=0 基线流速 1.0，并给出警告。
    first_track: dict[int, int] = {}
    for time_ms, group_id, kind, _value, _order in group_timeline_inputs:
        if kind != "track":
            continue
        if group_id not in first_track or time_ms < first_track[group_id]:
            first_track[group_id] = time_ms
    for group_id in sorted(declared_block_groups):
        if group_id in first_track:
            continue
        if group_id in block_bpm_groups:
            # 组内有 bpm 语句：组时间线已有首个事件，不需要基线。
            continue
        first_note_ms = 0
        for note in parsed_notes:
            if note.timing_group is None:
                continue
            if (note.timing_group & TIMINGGROUP_ID_MASK) != group_id:
                continue
            if first_note_ms == 0 or note.time_ms < first_note_ms:
                first_note_ms = note.time_ms
        _warn(
            f"timinggroup({group_id}) 没有任何 track/bpm 语句，"
            f"按首个音符时间 {first_note_ms}ms 流速 1.0 处理"
        )
        group_timeline_inputs.append(
            (first_note_ms, group_id, "base", chart_base_bpm, -1)
        )
    for note in parsed_notes:
        if note.timing_group is None:
            continue
        group_id = note.timing_group & TIMINGGROUP_ID_MASK
        if group_id == 0:
            continue
        first = first_track.get(group_id)
        if first is not None and note.time_ms < first:
            raise ValueError(
                f"timinggroup({group_id}) 的第一条 track 在 {first}ms，"
                f"但音符出现在 {note.time_ms}ms；"
                f"该时刻之前不允许出现音符"
            )
    conflicts = sorted(sid for sid in explicit_showtext_ids
                       if TEXT_SHOWTEXT_ID_BASE <= sid < TEXT_SHOWTEXT_ID_BASE + text_index)
    if conflicts:
        raise ValueError(f"显式 showtext id 与自动 text() 身份冲突：{conflicts}")
    base_by_group: dict[int, float] = {}
    track_by_group: dict[int, float] = {}
    bpm_by_group: dict[int, float] = {}
    effective_group_speeds: dict[tuple[int, int], float] = {}
    group_bpm_at: dict[tuple[int, int], float] = {}
    for time_ms, group_id, kind, value, _ in sorted(
        group_timeline_inputs, key=lambda item: (item[0], item[4])
    ):
        if kind == "base":
            # 原版语义：组内 bpm 只影响小节线/密度，不参与组流速。
            # 组视觉速度完全由 track() 决定。
            base_by_group[group_id] = 1.0
            bpm_by_group[group_id] = value
        else:
            track_by_group[group_id] = value
        effective = base_by_group.get(group_id, 1.0) * track_by_group.get(group_id, 1.0)
        key = (group_id, time_ms)
        effective_group_speeds[key] = effective
        # 组 BPM 只用于长音符 record 密度；缺省写 0，运行期回落到 chart BPM。
        group_bpm_at[key] = bpm_by_group.get(group_id, 0.0)
    for (group_id, time_ms), effective in effective_group_speeds.items():
        parsed_events.append(
            Event(
                index=event_count,
                event_type=5,
                time_ms=time_ms,
                speed_multiplier=effective,
                speed_group=group_id,
                group_bpm=group_bpm_at.get((group_id, time_ms), 0.0),
            )
        )
        event_count += 1

    # 排列顺序：全部音符严格按时间升序（同时间保持书写顺序）。
    # 游戏运行时按时间顺序消费谱面记录，滚动解码状态也随数组顺序推进；
    # 任何时间乱序都会让乱序点之后的音符解出垃圾字段（In Libertas 导出
    # 时同样会强制按时间排序，这里必须保持一致）。
    ordered_notes = sorted(
        parsed_notes,
        key=lambda note: (note.time_ms, note.end_ms, note.index),
    )
    for new_index, note in enumerate(ordered_notes):
        note.index = new_index

    if any(note.is_bombarea for note in ordered_notes):
        markers: list[tuple[Note, Note]] = []
        for note in ordered_notes:
            if not note.is_bombarea:
                continue
            marker = Note(
                index=0,
                group_id=0,
                note_type=NoteType.TRACKMOVE,
                track_id=4,
                time_ms=note.time_ms,
                end_ms=0,
                fractions=[
                    [0, 1], [0, 1], [0, 1], [0, 1],
                ],
                flags=(BOMBAREA_MAGIC << 8),
            )
            markers.append((marker, note))
            parsed_notes.append(marker)
        ordered_notes = sorted(
            parsed_notes,
            key=lambda note: (note.time_ms, note.end_ms, note.index),
        )
        for new_index, note in enumerate(ordered_notes):
            note.index = new_index
        for marker, target in markers:
            # The descriptor is inserted into the same chronological stream,
            # so its target id is only stable after the final reindex.
            marker.group_id = target.index
            marker.end_ms = target.index

    # noinput 迁移：把 noinput(group) 落到对应 skyarea 自身的 flags 上。
    # flags 里同时合成 ease 位（打包器对“有 flags 的音符”是整体写回 field5）。
    if noinput_groups:
        ease_left = {0: 0x04, 1: 0x08, 2: 0x10}
        ease_right = {0: 0x20, 1: 0x40, 2: 0x80}
        matched_groups = set()
        for note in ordered_notes:
            if note.note_type == NoteType.SKYAREA and note.group_id in noinput_groups:
                bits = (ease_left[int(note.left_ease or 0)]
                        | ease_right[int(note.right_ease or 0)])
                note.flags = (note.flags or 0) | bits | NOINPUT_FLAG
                matched_groups.add(note.group_id)
        for missing in sorted(noinput_groups - matched_groups):
            print_log(f'  [warn] noinput({missing}) does not match any SkyArea (group does not exist)')

    # skyarea 组号规则（官方 303/303 实证，2026-10-15）：组号必须沿时间**非递减**
    # （同一组号 = 接缝续段）。组号回退会让引擎的"最大已开始未消费天域组（FDA）"
    # 比较错乱，导致后续天域出现"过线不消失"等视觉异常（实例：101/102 之后接 9/10）。
    _sky_sorted = sorted(
        (n for n in ordered_notes if n.note_type == NoteType.SKYAREA),
        key = lambda n: (n.time_ms, n.index),
    )
    _sky_bad = [
        (a.time_ms, a.group_id, b.time_ms, b.group_id)
        for a, b in zip(_sky_sorted, _sky_sorted[1:])
        if b.group_id < a.group_id
    ]
    if _sky_bad:
        print_log('  [WARN] SkyArea group IDs are not nondecreasing over time; later SkyArea behavior may be affected:')
        for _t0, _g0, _t1, _g1 in _sky_bad[:8]:
            print_log(f'    {_t0}ms group={_g0} -> {_t1}ms group={_g1}')

    # timinggroup：把组号写进 note.group_id 的高 32 位（低 32 位保留原语义）。
    # trackmove 的 group_id 是坐标数据，不参与计时组。
    for note in ordered_notes:
        if note.timing_group and note.note_type != NoteType.TRACKMOVE:
            packed_group = int(note.timing_group) & 0xFFFFFFFF
            note.group_id = (note.group_id & 0xFFFFFFFF) | (packed_group << 32)

    parsed_events.sort(key = lambda event: (event.time_ms, event.index))
    for new_index, event in enumerate(parsed_events):
        event.index = new_index

    chart_json["notes"] = [note.to_dict() for note in ordered_notes]
    chart_json["events"] = [event.to_dict() for event in parsed_events]
    chart_json["noteCount"] = len(ordered_notes)
    chart_json["eventCount"] = len(parsed_events)
    if scene_actions:
        manifest = (scene_manifest if scene_manifest is not None
                    else read_scene_json(scene_sidecar_path(Path(file_path))))
        chart_json["scene"] = {**validate_source(manifest), "actions": scene_actions}
    try:
        validate_standard_chart(chart_json)
    except ChartValidationError as exc:
        raise ValueError(f"SPC V0 输入域校验失败：{exc}") from exc
    return chart_json

if __name__ == "__main__":
    chart = read_spc_v0_chart("./bird.spc")
    write_json("test.json", chart)
