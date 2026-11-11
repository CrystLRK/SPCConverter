import json
import re
from pathlib import Path
from dataclasses import dataclass
from enum import IntEnum

from chart_validation import ChartValidationError, validate_standard_chart


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
SCENE_TRACKDISPLAY = 1         # scenecontrol 子模式：轨道透明度
SCENE_HIDEGROUP = 2            # scenecontrol 子模式：隐藏 timinggroup 音符视觉
TIMINGGROUP_NOINPUT_BIT = 0x80000000  # timinggroup 高 32 位中的 noinput 标记
TIMINGGROUP_NOCLIP_BIT = 0x40000000   # 过判定线后仍保持视觉的 noinput 变体
TIMINGGROUP_ID_MASK = 0x1FFFFFFF
CONTAINER_FLAG_MAGIC_MASK = 0xFFFFFF00  # 容器 magic 放在 flags 的 8..31 位
CONTAINER_FLAG_MODE_MASK = 0xFF         # 容器低 8 位留给子模式


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


def read_spc_v0_chart(file_path):
    content = read_file(file_path)
    pattern = re.compile(r"(\w+)\(([^)]*)\)")
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
    block_group: int | None = None     # 非 None 表示当前处于新式 timinggroup {...} 块
    block_end_token: str | None = None
    declared_block_groups: set[int] = set()
    block_bpm_groups: set[int] = set()
    named_timing_groups: dict[str, int] = {}
    group_timeline_inputs: list[tuple[int, int, str, float, int]] = []
    source_order = 0
    audiooffset_seen = False
    next_auto_skyarea_group = 0
    last_v1_skyarea_end: int | None = None
    last_v1_skyarea_group: int | None = None

    # 新式 timinggroup 的块级规则要求能区分空行/注释和真正的第一条语句。
    def active_line(raw: str) -> str:
        text = raw.strip()
        if not text or text.startswith("#") or text.startswith("//"):
            return ""
        return text.split("//", 1)[0].strip()

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

    first_active = next(
        (line for raw in content.splitlines()
         if (line := active_line(raw))),
        "",
    )
    if not first_active.startswith("chart("):
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
            continue
        resolve_stage1 = pattern.search(line)
        if resolve_stage1:
            note_type = resolve_stage1.group(1).lower()
            note_arguments_list = [x.strip() for x in resolve_stage1.group(2).split(",")]
            note = None
            event = None

            argument_counts = {
                "chart": (2, 2),
                "tap": (3, 3),
                "bomb": (3, 3),
                "hold": (4, 4),
                "flick": (5, 5),
                "skyarea": (10, 11),
                "trackmove": (9, 9),
                "noinput": (1, 1),
                "showtext": (2, 3),
                "audiooffset": (1, 1),
                "setcolor": (3, 4),
                "scenecontrol": (2, 4),
                "hidegroup": (2, 2),
                "timinggroup": (0, 2),
                "lane": (3, 3),
                "bpm": (2, 4),
                "track": (2, 2),
            }
            if note_type in argument_counts:
                minimum, maximum = argument_counts[note_type]
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
                    if flick_den <= 0:
                        raise ValueError(
                            f"第 {line_no} 行：flick denominator 必须大于 0")
                    if flick_direction not in (4, 16):
                        raise ValueError(
                            f"第 {line_no} 行：flick direction 只能是右 4 或左 16")
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
                        flick = flick_direction
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "skyarea":
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
                    # setcolor(time, skyarea_group, #RRGGBB[AA])
                    if len(note_arguments_list) not in (3, 4):
                        raise ValueError(
                            "setcolor(timems,groupId,hex[,edgeGlow]) 需要三个或四个参数")
                    t = round_ms(note_arguments_list[0])
                    sky_group = int(note_arguments_list[1])
                    if not 0 <= sky_group <= 0xFFFFFFFF:
                        raise ValueError(
                            f"第 {line_no} 行：setcolor groupId 越界")
                    hex_text = note_arguments_list[2].strip().lstrip("#")
                    edge_glow = 1
                    if len(note_arguments_list) == 4:
                        edge_text = str(note_arguments_list[3]).strip().lower()
                        if edge_text in ("1", "true", "yes", "on"):
                            edge_glow = 1
                        elif edge_text in ("0", "false", "no", "off"):
                            edge_glow = 0
                        else:
                            raise ValueError(
                                "setcolor edgeGlow 必须是 0/1 或 true/false")
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
                        flags=SETCOLOR_MAGIC << 8,
                    )
                    note_count += 1
                    parsed_notes.append(note)
                case "scenecontrol":
                    if len(note_arguments_list) < 2:
                        raise ValueError("scenecontrol(timems,mode,*param) 参数不足")
                    t = round_ms(note_arguments_list[0])
                    mode_name = note_arguments_list[1].strip().lower()
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
                        if len(note_arguments_list) > 1:
                            option = note_arguments_list[1].replace(" ", "").lower()
                            if option == "noinput":
                                noinput = True
                            elif option == "noclip":
                                noclip = True
                                noinput = True
                            elif "=" not in option:
                                raise ValueError(
                                    f"第 {line_no} 行：timinggroup 第二参数必须写成 "
                                    "noinput=true/false 或 noclip=true/false"
                                )
                            else:
                                key, value = option.split("=", 1)
                                if key not in ("noinput", "noclip") or value not in ("true", "false"):
                                    raise ValueError(
                                        f"第 {line_no} 行：未知 timinggroup 选项 {note_arguments_list[1]}"
                                    )
                                if key == "noclip":
                                    noclip = value == "true"
                                    noinput = noclip
                                else:
                                    noinput = value == "true"
                        if len(note_arguments_list) > 2:
                            raise ValueError(
                                "timinggroup 最多接受 groupId 与一个模式参数；"
                                "noclip 已自动包含 noinput"
                            )
                        current_group = group_id
                        current_group_noinput = noinput
                        current_group_noclip = noclip
                        block_group = group_id
                        block_end_token = "}" if "{" in line else "groupend()"
                    elif note_arguments_list and note_arguments_list[0] != "":
                        current_group = int(note_arguments_list[0])
                        if not 0 <= current_group <= TIMINGGROUP_ID_MASK:
                            raise ValueError(
                                f"第 {line_no} 行：timinggroup groupId 越界")
                        current_group_noinput = False
                        current_group_noclip = False
                    else:
                        current_group = 0
                        current_group_noinput = False
                        current_group_noclip = False
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
                    if bpm_value <= 0:
                        raise ValueError("bpm 数值必须大于 0")
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
                            beat_amount = beat_unit
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
                            speed_multiplier = float(note_arguments_list[1])
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

    # timinggroup 的组内 bpm 与 track 最终合成为现有的 type5 GroupSpeed 事件。
    # 底层 C# 运行期仍只需消费“某一时刻之后的组有效流速”，无需新增事件类型。
    chart_base_bpm = float(chart_json["bpm"] or 0.0)
    if group_timeline_inputs and chart_base_bpm <= 0:
        raise ValueError("timinggroup 使用 bpm 时，chart() 头部 BPM 必须大于 0")
    # A block without its own bpm uses the chart's initial bpm as its baseline.
    for group_id in sorted(declared_block_groups - block_bpm_groups):
        group_timeline_inputs.append(
            (0, group_id, "base", chart_base_bpm, -1)
        )
    base_by_group: dict[int, float] = {}
    track_by_group: dict[int, float] = {}
    effective_group_speeds: dict[tuple[int, int], float] = {}
    for time_ms, group_id, kind, value, _ in sorted(
        group_timeline_inputs, key=lambda item: (item[0], item[4])
    ):
        if kind == "base":
            base_by_group[group_id] = value / chart_base_bpm
        else:
            track_by_group[group_id] = value
        effective = base_by_group.get(group_id, 1.0) * track_by_group.get(group_id, 1.0)
        effective_group_speeds[(group_id, time_ms)] = effective
    for (group_id, time_ms), effective in effective_group_speeds.items():
        parsed_events.append(
            Event(
                index=event_count,
                event_type=5,
                time_ms=time_ms,
                speed_multiplier=effective,
                speed_group=group_id,
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
            print(f"  [warn] noinput({missing}) 未匹配到任何 skyarea（该组号不存在）")

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
        print("  [warn] skyarea 组号未沿时间非递减（官方谱规格；可能导致后续天域异常）：")
        for _t0, _g0, _t1, _g1 in _sky_bad[:8]:
            print(f"    {_t0}ms 组{_g0} → {_t1}ms 组{_g1}")

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
    try:
        validate_standard_chart(chart_json)
    except ChartValidationError as exc:
        raise ValueError(f"SPC V0 输入域校验失败：{exc}") from exc
    return chart_json

if __name__ == "__main__":
    chart = read_spc_v0_chart("./bird.spc")
    write_json("test.json", chart)
        
