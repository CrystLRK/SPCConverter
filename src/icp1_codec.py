"""
ICP1 (`.spc`) 谱面解码器 / 转码器（Python 版）。

与 mod 里修好后的 `InFalsusChartExt/Icp1Codec.cs` 完全等价：

注意：本工具的 JSON 是**原始字段转储**（cipher/raw/id/side_type_raw 全保留，追求逐字节恒等回环），
并非工作目录文档里约定的"标准 JSON 格式"；标准格式的互转请用 `tools/spc_json_conv.py`。

    python tools/icp1_codec.py decode <chart.spc> <key> [out.json]
    python tools/icp1_codec.py encode <in.json> <key> <out.spc>
    python tools/icp1_codec.py verify <chart.spc> <key>      # 解码→重编码，逐字节对比

`key` 规则：谱面文件名（含后缀），例如 `test.spc` / `tokonaga3.spc` / `toutaku_subeshi3.spc`。
它同时是滚动状态的种子，必须与游戏侧解析时使用的名字完全一致（含大小写）。

════════════════════════════════════════════════════════════════════
格式要点（都是踩过的坑，改动前务必先读）
════════════════════════════════════════════════════════════════════

1. 文件结构（全部小端）：

       偏移        内容
       0x00        "ICP1" 魔数
       0x04        u16 版本（解码时保留，编码原样写回）
       0x06        u16 头部长度（28）
       0x08        u16 音符长度（80）
       0x0A        u16 事件长度（32）
       0x0C        u32 音符数量
       0x10        u32 事件数量
       0x14        u32 元数据字段 u20（保留）
       0x18        u32 元数据字段 u24（保留）
       0x1C 起     音符数组（80 字节 × N）
       之后        事件数组（32 字节 × M）

2. 每个音符是 80 字节，其中大部分字段被"滚动状态"加了掩码：
   `真实值 = 密文 − gen(state, 音符下标, 字段号)`（按 64/32 位取模）。
   状态只在一个音符完整处理后按参考实现 advance() 前进；**事件不参与状态推进**。

3. 分数对（fraction pair，共 4 对，每对 (分子,分母)）的编码：
      - 0x20..0x2F：4 个"scale"密文（S32），记作 cipher；
      - 0x30..0x4F：8 个"原始对" int32（明文，不加掩码），记作 raw；
      - **真实分数 = cipher × (raw / gcd(raw))**（逐分量）。
   推进滚动状态时必须使用"真实分数"；重新编码时必须把 cipher 与 raw 都**原样写回**。
   官方谱实测 7128 对里 7100 对"真实分数 ≠ 原始对"——用错任何一侧都会让后面所有
   音符解码发散（历史上表现为"官谱整谱只剩一个音符"）。

4. 音符 id 必须原样保留，**不要**改成数组下标：它是运行时记录表的键。

5. 兜底原则：本工具解码→编码应当逐字节可回环（verify 命令自检）。任何解析不明的东西
   一律"原样保留"而不是"重新推导"，这是保住官谱的关键。
"""

import json
import struct
import sys
from pathlib import Path

M64 = (1 << 64) - 1
M32 = (1 << 32) - 1

HEADER_SIZE = 28
NOTE_SIZE = 80
EVENT_SIZE = 32


# ─────────────────────────── 滚动状态机（参考实现逐行移植） ───────────────────────────

def rotl(v: int, s: int) -> int:
    v &= M64
    return ((v << s) | (v >> (64 - s))) & M64


class State:
    """4×64bit + 计数器；counter 的低 2 位选择 advance 的 4 个相位。"""

    __slots__ = ("a", "b", "c", "d", "counter")

    def __init__(self, a, b, c, d, counter):
        self.a, self.b, self.c, self.d, self.counter = a & M64, b & M64, c & M64, d & M64, counter


def advance(state: State, first: int, second: int, third: int) -> None:
    """一次状态推进（与 In Libertas chart_document.cpp 完全一致）。"""
    first &= M64
    second &= M64
    third &= M64
    v = state.counter & 3
    if v == 0:
        state.a = (state.a + first + rotl(state.d, 7)) & M64
        state.b ^= rotl((second + state.a) & M64, 13)
        state.c = (state.c - (state.b ^ third)) & M64
        state.d = state.a ^ rotl((state.c + state.d) & M64, 27)
    elif v == 1:
        state.b = (state.b + third + rotl(state.a, 9)) & M64
        state.c ^= rotl((first + state.b) & M64, 19)
        state.d = (state.d - (state.c ^ second)) & M64
        state.a = state.b ^ rotl((state.a + state.d) & M64, 31)
    elif v == 2:
        state.c = (state.c + second + rotl(state.b, 15)) & M64
        state.d ^= rotl((third + state.c) & M64, 23)
        state.a = (state.a - (state.d ^ first)) & M64
        state.b = state.c ^ rotl((state.a + state.b) & M64, 39)
    else:
        old_c = state.c
        state.d = (state.d + first + rotl(state.c, 21)) & M64
        state.a ^= rotl((second + state.d) & M64, 29)
        state.b = (state.b - (third ^ state.a)) & M64
        state.c = state.d ^ rotl((state.b + old_c) & M64, 43)
    state.counter = (state.counter + 1) & M32


def seed_state(name: str, note_count: int) -> State:
    """用谱面名 + 音符数量生成初始状态（与游戏侧种子完全一致）。"""
    s = State(
        (note_count - 0x3B314601E57A13AD) & M64,
        ((note_count & 0xFFFFFFFF) << 32) ^ 0x9E3779B185EBCA87,
        (1 - 0x2E4AB5CD2E6D12FD) & M64,
        1 ^ 0x94D049BB133111EB,
        0,
    )
    for index, ch in enumerate(name):
        character = ((index & 0xFFFFFFFF) << 32) | (ord(ch) & 0xFFFFFFFF)
        mode_mixed = character ^ 1
        count_mixed = (character + note_count) & M64
        advance(s, character, count_mixed, mode_mixed)
    advance(s, note_count, 1, len(name))
    return s


def mask_for_field(state: State, note_index: int, field_index: int) -> int:
    """给定 (音符下标, 字段号) 返回该字段的掩码：密文 = 明文 + 掩码（取模）。"""
    packed = ((note_index & 0xFFFFFFFF) << 32) | (field_index & 0xFFFFFFFF)
    v = field_index & 3
    if v == 0:
        return state.d ^ ((state.a + rotl(state.c ^ packed, 11)) & M64)
    if v == 1:
        return state.a ^ ((state.b - rotl((packed + state.d) & M64, 17)) & M64)
    if v == 2:
        return state.b ^ ((state.c + rotl(state.a ^ packed, 29)) & M64)
    return state.d ^ rotl((packed + state.b + state.c) & M64, 37)


def positive_gcd(a: int, b: int) -> int:
    """正整数 gcd；(0,0) 约定为 1（与参考实现一致）。"""
    a, b = abs(a), abs(b)
    if a == 0 and b == 0:
        return 1
    while b:
        a, b = b, a % b
    return a


def pair_gcd(n: int, d: int) -> int:
    """两个 u32 位型按 S32 解释后的 gcd（用于记录态推进）。"""
    return positive_gcd(s32(n), s32(d))


def s32(v: int) -> int:
    v &= M32
    return v - (1 << 32) if v >> 31 else v


def scale_pair(scale_raw: int, num_raw: int, den_raw: int):
    """还原一对分数：真实值 = scale × (num/g, den/g)。"""
    scale = s32(scale_raw)
    num = s32(num_raw)
    den = s32(den_raw)
    div = positive_gcd(num, den)
    return scale * (num // div), scale * (den // div)


def pack_pair(low: int, high: int) -> int:
    """把两个 S32 装进一个 u64（低位在前），用于状态推进的分数参数。"""
    return (low & M32) | ((high & M32) << 32)


def finalize_record_state(state: State, note_index: int, field_zero: int, g0: int, g1: int, g2: int, g3: int) -> None:
    """音符处理完成后的"记录态"混合（用原始对的 gcd）。"""
    repeated_index = (note_index & M32) | ((note_index & M32) << 32)
    folded = (repeated_index + ((state.a + rotl(state.b, 9)) ^ (state.d + rotl(state.c, 23)))) & M64
    fold = (folded ^ (folded >> 32)) & M32
    fourth = (((g3 - 1) & M64) ^ (fold >> 24)) & M64
    third = (rotl((((g2 - 1) & M64) ^ ((fold >> 16) & 0xFF)), 29) ^ rotl(fourth, 47)) & M64
    combined = ((((g0 - 1) & M64) ^ (fold & 0xFF))
                ^ rotl((((g1 - 1) & M64) ^ ((fold >> 8) & 0xFF)) & M64, 13)
                ^ third) & M64
    mix = ((field_zero & M32) ^ (note_index & M32) ^ (combined & M32)
           ^ ((combined >> 32) & M32) ^ ((field_zero >> 32) & M32))
    repeated = (mix & M32) | ((mix & M32) << 32)
    state.a ^= repeated
    state.b = (state.b + rotl(repeated, 17)) & M64
    state.c = (state.c - rotl(repeated, 31)) & M64
    state.d ^= rotl(repeated, 47)


def advance_note(state: State, note_id: int, group: int, start: int, end: int, side_type_raw: int,
                 flags: int, scaled: list, raw: list, note_index: int) -> None:
    """一个音符之后的状态推进：
    - 前两次 advance 用 id/组/时间 与 side/type/flags；
    - 分数相关三次用**还原后的分数**（scaled）；
    - 记录态用**原始对**（raw）的 gcd。
    """
    advance(state, note_id, group, ((end & M32) << 32) | (start & M32))
    advance(state, side_type_raw, flags, pack_pair(scaled[0], scaled[1]))
    advance(state, pack_pair(scaled[2], scaled[3]), pack_pair(scaled[4], scaled[5]), pack_pair(scaled[6], scaled[7]))
    finalize_record_state(state, note_index, note_id,
                          pair_gcd(raw[0], raw[1]), pair_gcd(raw[2], raw[3]),
                          pair_gcd(raw[4], raw[5]), pair_gcd(raw[6], raw[7]))


# ─────────────────────────────────── 解码 ───────────────────────────────────

def decode(data: bytes, key: str) -> dict:
    """ICP1 二进制 → JSON 可序列化的字典（所有可回环字段原样保留）。"""
    if len(data) < HEADER_SIZE or data[0:4] != b"ICP1":
        raise ValueError("不是 ICP1 谱面（魔数不符）")
    version, header_len, note_len, event_len = struct.unpack_from("<HHHH", data, 4)
    note_count, event_count, u20, u24 = struct.unpack_from("<IIII", data, 12)
    if header_len != HEADER_SIZE or note_len != NOTE_SIZE or event_len != EVENT_SIZE:
        raise ValueError("头部尺寸字段异常")
    if HEADER_SIZE + note_count * NOTE_SIZE + event_count * EVENT_SIZE != len(data):
        raise ValueError("文件长度与头部的音符/事件数量不符")

    state = seed_state(key, note_count)
    notes = []
    for i in range(note_count):
        off = HEADER_SIZE + i * NOTE_SIZE

        # 带掩码的字段：密文 − gen(state, i, k)
        note_id = (struct.unpack_from("<Q", data, off)[0] - mask_for_field(state, i, 0)) & M64
        group = (struct.unpack_from("<Q", data, off + 8)[0] - mask_for_field(state, i, 1)) & M64
        start = (struct.unpack_from("<I", data, off + 0x10)[0] - mask_for_field(state, i, 2)) & M32
        end = (struct.unpack_from("<I", data, off + 0x14)[0] - mask_for_field(state, i, 3)) & M32
        side_type_raw = (struct.unpack_from("<I", data, off + 0x18)[0] - mask_for_field(state, i, 4)) & M32
        flags = (struct.unpack_from("<I", data, off + 0x1C)[0] - mask_for_field(state, i, 5)) & M32
        cipher = [(struct.unpack_from("<I", data, off + 0x20 + 4 * p)[0] - mask_for_field(state, i, 6 + p)) & M32
                  for p in range(4)]

        # 明文部分（不加掩码）：8 个原始对 int32
        raw = [s32(struct.unpack_from("<I", data, off + 0x30 + 4 * p)[0]) for p in range(8)]

        # 还原分数（只读参考；重编码用 cipher+raw，不用它）
        fraction = [list(scale_pair(cipher[p], raw[2 * p], raw[2 * p + 1])) for p in range(4)]
        scaled = [v for pair in fraction for v in pair]  # 状态推进要用"还原值"

        notes.append({
            "id": note_id,
            "group": group,
            "start_ms": start,
            "end_ms": end,
            "side": side_type_raw & 0xFF,
            "type": (side_type_raw >> 8) & 0xFF,
            "flags": flags,
            "side_type_raw": side_type_raw,          # 原样保留（高位若被使用也不丢）
            "cipher": [s32(c) for c in cipher],      # 原始密文（S32），重编码原样写回
            "pairs": raw,                            # 原始对（S32），重编码原样写回
            "fraction": fraction,                    # 还原值，仅供参考
            "_scaled": scaled if scaled != raw else None,  # 与 raw 不同才保留（供阅读）
        })

        advance_note(state, note_id, group, start, end, side_type_raw, flags, scaled, raw, i)

    event_base = HEADER_SIZE + note_count * NOTE_SIZE
    events = []
    for i in range(event_count):
        off = event_base + i * EVENT_SIZE
        events.append({
            "id": struct.unpack_from("<Q", data, off)[0],
            "time_ms": struct.unpack_from("<I", data, off + 8)[0],
            "type": struct.unpack_from("<I", data, off + 12)[0],
            "value1": struct.unpack_from("<Q", data, off + 16)[0],
            "value2": struct.unpack_from("<Q", data, off + 24)[0],
        })

    return {
        "key": key,
        "version": version,
        "u20": u20,
        "u24": u24,
        "notes": notes,
        "events": events,
    }


# ─────────────────────────────────── 转码（编码） ───────────────────────────────────

def encode(obj: dict, key: str) -> bytes:
    """JSON 字典 → ICP1 二进制（与 decode 严格互逆，逐字节可回环）。"""
    notes = obj["notes"]
    events = obj.get("events", [])
    note_count = len(notes)
    event_count = len(events)
    out = bytearray(HEADER_SIZE + note_count * NOTE_SIZE + event_count * EVENT_SIZE)

    struct.pack_into("<4s", out, 0, b"ICP1")
    struct.pack_into("<HHHH", out, 4, int(obj.get("version", 1)), HEADER_SIZE, NOTE_SIZE, EVENT_SIZE)
    struct.pack_into("<IIII", out, 12, note_count, event_count, int(obj.get("u20", 0)), int(obj.get("u24", 0)))

    state = seed_state(key, note_count)
    for i, note in enumerate(notes):
        off = HEADER_SIZE + i * NOTE_SIZE

        note_id = int(note["id"]) & M64
        group = int(note["group"]) & M64
        start = int(note["start_ms"]) & M32
        end = int(note["end_ms"]) & M32
        side_type_raw = int(note.get("side_type_raw", (int(note.get("side", 0)) & 0xFF) | ((int(note.get("type", 0)) & 0xFF) << 8))) & M32
        flags = int(note["flags"]) & M32
        cipher = [int(v) & M32 for v in note["cipher"]]
        raw = [int(v) for v in note["pairs"]]

        # 密文 = 明文 + 掩码（取模）；写回时全部用原始 cipher/raw，保证恒等回环
        struct.pack_into("<Q", out, off, (note_id + mask_for_field(state, i, 0)) & M64)
        struct.pack_into("<Q", out, off + 8, (group + mask_for_field(state, i, 1)) & M64)
        struct.pack_into("<I", out, off + 0x10, (start + mask_for_field(state, i, 2)) & M32)
        struct.pack_into("<I", out, off + 0x14, (end + mask_for_field(state, i, 3)) & M32)
        struct.pack_into("<I", out, off + 0x18, (side_type_raw + mask_for_field(state, i, 4)) & M32)
        struct.pack_into("<I", out, off + 0x1C, (flags + mask_for_field(state, i, 5)) & M32)
        for p in range(4):
            struct.pack_into("<I", out, off + 0x20 + 4 * p, (cipher[p] + mask_for_field(state, i, 6 + p)) & M32)
        for p in range(8):
            struct.pack_into("<I", out, off + 0x30 + 4 * p, raw[p] & M32)

        # 状态推进必须与解码使用同一套输入（还原分数 + 原始对 gcd + 原始 id/下标）
        scaled = [v for p in range(4) for v in scale_pair(cipher[p], raw[2 * p], raw[2 * p + 1])]
        advance_note(state, note_id, group, start, end, side_type_raw, flags, scaled, raw, i)

    event_base = HEADER_SIZE + note_count * NOTE_SIZE
    for i, ev in enumerate(events):
        off = event_base + i * EVENT_SIZE
        struct.pack_into("<Q", out, off, int(ev["id"]) & M64)
        struct.pack_into("<I", out, off + 8, int(ev["time_ms"]) & M32)
        struct.pack_into("<I", out, off + 12, int(ev["type"]) & M32)
        struct.pack_into("<Q", out, off + 16, int(ev["value1"]) & M64)
        struct.pack_into("<Q", out, off + 24, int(ev["value2"]) & M64)

    return bytes(out)


# ─────────────────────────────────── CLI ───────────────────────────────────

def _print_summary(obj: dict) -> None:
    kinds = {}
    bombs = 0
    for n in obj["notes"]:
        kinds[n["type"]] = kinds.get(n["type"], 0) + 1
        if n["type"] in (1, 5) and (n["flags"] & 0x8000):
            bombs += 1
    evt = {}
    for e in obj["events"]:
        evt[e["type"]] = evt.get(e["type"], 0) + 1
    print(f"  notes={len(obj['notes'])} types={kinds} flag0x8000={bombs} events={len(obj['events'])} evtTypes={evt}")


def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "decode":
        src = Path(sys.argv[2])
        key = sys.argv[3]
        dst = Path(sys.argv[4]) if len(sys.argv) > 4 else src.with_suffix(".json")
        obj = decode(src.read_bytes(), key)
        dst.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"decoded {src.name} (key='{key}') -> {dst}")
        _print_summary(obj)
    elif cmd == "encode":
        src = Path(sys.argv[2])
        key = sys.argv[3]
        dst = Path(sys.argv[4]) if len(sys.argv) > 4 else src.with_suffix(".spc")
        obj = json.loads(src.read_text(encoding="utf-8"))
        dst.write_bytes(encode(obj, key))
        print(f"encoded {src.name} (key='{key}') -> {dst}")
    elif cmd == "verify":
        src = Path(sys.argv[2])
        key = sys.argv[3]
        data = src.read_bytes()
        obj = decode(data, key)
        out = encode(obj, key)
        diffs = sum(1 for a, b in zip(data, out) if a != b) + abs(len(data) - len(out))
        first = next((i for i, (a, b) in enumerate(zip(data, out)) if a != b), -1)
        print(f"verify {src.name} (key='{key}'): orig={len(data)} re-enc={len(out)} "
              f"byteDiffs={diffs} firstDiff={first}")
        _print_summary(obj)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
