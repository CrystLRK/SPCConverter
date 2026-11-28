"""
spc 三格式互转：明文 spc（V0 文本） ↔ 标准 JSON ↔ 打包 spc（ICP1 二进制）。

“标准 JSON”= 工作目录 `infalsus谱面格式_spcV1.txt` 里约定的结构，也是
`spc_v0_chart_decoder.py` 产出、`chart_packer.py` 消费的结构：

    {
      "format": "ICP1", "version": 1,
      "bpm": 140.0, "meter": 4.0,
      "noteCount": N, "eventCount": M,
      "notes": [ {index, line, type, track, timeMs, endMs, fractions,
                  [flick] | [leftEase, rightEase] , [flags]} , ...],
      "events": [ {id, type, timeMs, [value | bpm+speed | flags]} , ...]
    }

命令：
    python tools/spc_json_conv.py v0text2json <in.spc> [out.json]         明文 → 标准 JSON
    python tools/spc_json_conv.py json2v0text <in.json> [out.spc]         标准 JSON → 明文
    python tools/spc_json_conv.py json2v1bin  <in.json> <key> [out.spc]   标准 JSON → 二进制
    python tools/spc_json_conv.py v1bin2json  <in.spc> <key> [out.json]   二进制 → 标准 JSON
    python tools/spc_json_conv.py roundtrip   <in.spc> <key>              自检：二进制→JSON→二进制，逐字节对比

`key` 必须是**目标/源 .spc 的文件名（含后缀、大小写敏感）**，如 `toutaku_subeshi3.spc`——
它同时是二进制滚动加密的种子，必须与游戏侧解析名一致。

实现说明（三段各自的真源）：
  - 明文 ↔ JSON：复用 `spc_v0_chart_decoder.read_spc_v0_chart`（明文 → JSON）与
    本文件里的 `serialize_v0()`（JSON → 明文语句）。
  - JSON → 二进制：复用 `chart_packer.pack_chart`（与既有打包流程完全一致）。
  - 二进制 → JSON：复用 `tools/icp1_codec.py` 的（已修好的、可逐字节回环的）解码核心，
    再映射回标准 JSON：fractions 输出**还原后的真实分数**；note 的 id/line 按明文管线的
    约定重排为位置（我们的谱面本来就 id=位置，故 roundtrip 仍逐字节一致）。

事件 type2 语义（同一 type 码）：
  - 原生 BPM 过渡（demo 的 BpmChangeV3）：time/拍数正常携带，JSON 字段 bpm+speed，
    明文 bpm(t, bpm, 拍数, mode≠0)（mode 在导出时归一化为 1，非 0 语义等价）；
  - 旧版 Mod noinput（仅兼容读取）：type2 事件、time=0 且 value2=0、value=天域组号；
    `raw_to_standard()` 会把它迁移为新表示（下方），不再输出该事件。
新表示 noinput：`noinput(group)` → 对应 group 的 skyarea 音符带上 flags（ease 位 | 0x8000），
文件中不再出现 type2 事件；打包器把 flags 整体写回 field5。
因此对 2026-10-12 之前打包的 noinput 谱面，`roundtrip` 会报非 0 字节差异（属预期的迁移），
重新打包即得到新格式；`spc_conv_verify.py` 的语义比较不受影响（两侧都会归一化）。
"""

from ascii_log import print_log

import json
import sys
from pathlib import Path

# 让脚本无论从哪个目录运行都能找到工作目录根部的两个既有模块，
# 以及 tools/ 下的 icp1_codec。
_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from spc_v0_chart_decoder import read_spc_v0_chart  # noqa: E402  明文 → JSON（既有实现）
from chart_packer import pack_chart                 # noqa: E402  JSON → 二进制（既有实现）
from chart_validation import validate_standard_chart  # noqa: E402
from icp1_codec import decode as raw_decode         # noqa: E402  二进制 → 原始字段
from icp1_codec import scale_pair                   # noqa: E402  分数还原：真实值 = 密文 × (raw/gcd)

ARC_MAGIC = 0x415243
FLICK3D_MAGIC = 0x464C33
TIMINGGROUP_ANGLE_MAGIC = 0x544741
ARC_NATIVE_FLAG = 0x02000000
FLICK3D_NATIVE_FLAG = 0x01000000


# ─────────────────────────── JSON → 明文 spc（V0 文本） ───────────────────────────

def _num(v) -> str:
    """数字的文本形式：浮点用 repr（可无损往返），整数原样。"""
    if isinstance(v, int):
        return str(v)
    return repr(float(v))


def serialize_v0(obj: dict) -> str:
    """JSON/V1 share one lossless semantic V0 serializer (including groups/markers)."""
    from spc_semantic import serialize_v0_semantic
    return serialize_v0_semantic(obj)


def raw_to_standard(raw: dict) -> dict:
    """原始字段（icp1_codec.decode 的输出）→ 标准 JSON。

    - fractions：用 scale_pair 还原为真实分数（与明文管线里 fractions 的含义一致）。
    - line(group)：tap/hold/flick 按明文管线约定重排为位置；skyarea/trackmove 保留原 group。
    - flags：tap/hold 仅在有位时输出；skyarea 输出 leftEase/rightEase（额外的位用 flags 兜底）；
      flick 输出 flick 字节（其余位用 flags 兜底）；type3 原样输出 flags（含 magic）。
    - 事件 type2：旧版 noinput（time=0 且 value2=0）迁移为对应 skyarea 的 flags 0x8000，不再输出事件；
      原生 BPM 过渡（BpmChangeV3）保留为 bpm+speed 事件。
    """
    bpm = _f32(raw["u20"])
    meter = _f32(raw["u24"])
    notes_out = []
    for idx, n in enumerate(raw["notes"]):
        t = (n["side_type_raw"] >> 8) & 0xFF
        side = n["side_type_raw"] & 0xFF
        frac = [list(scale_pair(n["cipher"][p], n["pairs"][2 * p], n["pairs"][2 * p + 1]))
                for p in range(4)]
        flags = int(n["flags"])
        raw_group = int(n["group"])
        timing_group_packed = (raw_group & ((1 << 64) - 1)) >> 32
        note = {
            "index": idx,
            "line": (raw_group if timing_group_packed else idx)
                    if t in (1, 2, 4) else raw_group,
            "type": t,
            "track": side,
            "timeMs": int(n["start_ms"]),
            "endMs": int(n["end_ms"]),
            "fractions": frac,
        }
        if t == 4:  # flick：方向在 flags 的 8..15 位
            note["flick"] = (flags >> 8) & 0xFF
            if flags & ~(0xFF << 8):
                note["flags"] = flags
        elif t == 5:  # skyarea：ease 位 0x04/0x08/0x10 与 0x20/0x40/0x80
            note["leftEase"] = {0x04: 0, 0x08: 1, 0x10: 2}.get(flags & 0x1C, 0)
            note["rightEase"] = {0x20: 0, 0x40: 1, 0x80: 2}.get(flags & 0xE0, 0)
            if flags & ~0xFC:
                note["flags"] = flags
        else:
            if flags:
                note["flags"] = flags
        notes_out.append(note)

    # 事件映射。type2 的两种语义在此归一化：
    #  - 旧版 noinput（time=0 且 value2=0）：不再输出事件，改为把对应 skyarea 打上 flags 0x8000；
    #  - 其余 type2 = 原生 BPM 过渡（demo BpmChangeV3：value1=BPM、value2=拍数 float），保留为事件。
    noinput_groups = set()
    events_out = []
    for e in raw["events"]:
        t = int(e["type"])
        if t == 2 and int(e["time_ms"]) == 0 and int(e["value2"]) == 0:
            noinput_groups.add(int(round(_f64(e["value1"]))))
            continue
        item = {"id": len(events_out), "type": t, "timeMs": int(e["time_ms"])}
        if t == 2:
            item["bpm"] = _f64(e["value1"])
            item["speed"] = _f32(e["value2"] & 0xFFFFFFFF)
        elif t == 0:       # track 倍率（double 存在 value1）
            item["value"] = _f64(e["value1"])
        elif t == 1:       # bpm 与 小节线密度
            item["bpm"] = _f64(e["value1"])
            item["speed"] = _f32(e["value2"] & 0xFFFFFFFF)
        elif t == 3:       # 轨道开关
            item["flags"] = int(e["value1"])
        elif t == 5:
            # timinggroup 组内有效流速；value2 低 32 位是组号，
            # 高 32 位是组 BPM（float，0 表示未指定）。
            item["value"] = _f64(e["value1"])
            item["speed"] = _f32(e["value2"] & 0xFFFFFFFF)
            item["groupBpm"] = _f32(
                (int(e["value2"]) >> 32) & 0xFFFFFFFF
            )
        else:
            item["value"] = _f64(e["value1"])
        events_out.append(item)

    # 旧版 noinput 事件 → 新表示：对应 skyarea 的 flags（ease 位 | 0x8000，整体写回 field5）
    if noinput_groups:
        ease_left = {0: 0x04, 1: 0x08, 2: 0x10}
        ease_right = {0: 0x20, 1: 0x40, 2: 0x80}
        for note in notes_out:
            if note["type"] == 5 and int(note["line"]) in noinput_groups:
                bits = (ease_left[int(note.get("leftEase", 0))]
                        | ease_right[int(note.get("rightEase", 0))])
                note["flags"] = int(note.get("flags", 0)) | bits | 0x8000

    return {
        "format": "ICP1",
        "version": int(raw["version"]),
        "bpm": bpm,
        "meter": meter,
        "noteCount": len(notes_out),
        "eventCount": len(events_out),
        "notes": notes_out,
        "events": events_out,
    }


def _f32(u32: int) -> float:
    import struct
    return struct.unpack(
        "<f", struct.pack("<I", u32 & 0xFFFFFFFF))[0]


def _f64(u64: int) -> float:
    import struct
    return struct.unpack(
        "<d", struct.pack("<Q", u64 & ((1 << 64) - 1)))[0]


# ─────────────────────────── CLI ───────────────────────────

def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(p: Path, obj: dict) -> None:
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "v0text2json":
        src = Path(sys.argv[2])
        dst = Path(sys.argv[3]) if len(sys.argv) > 3 else src.with_suffix(".json")
        obj = read_spc_v0_chart(str(src))
        _write_json(dst, obj)
        print_log(f"v0text2json: {src} -> {dst}  (notes={obj['noteCount']}, events={obj['eventCount']})")
    elif cmd == "json2v0text":
        src = Path(sys.argv[2])
        dst = Path(sys.argv[3]) if len(sys.argv) > 3 else src.with_suffix(".spc")
        obj = _read_json(src)
        dst.write_text(serialize_v0(obj), encoding="utf-8")
        print_log(f"json2v0text: {src} -> {dst}")
    elif cmd == "json2v1bin":
        src = Path(sys.argv[2])
        key = sys.argv[3]
        dst = Path(sys.argv[4]) if len(sys.argv) > 4 else src.with_suffix(".spc")
        obj = _read_json(src)
        dst.write_bytes(pack_chart(obj, key))
        print_log(f"json2v1bin: {src} -> {dst}  (key='{key}')")
    elif cmd == "v1bin2json":
        src = Path(sys.argv[2])
        key = sys.argv[3]
        dst = Path(sys.argv[4]) if len(sys.argv) > 4 else src.with_suffix(".json")
        raw = raw_decode(src.read_bytes(), key)
        standard = raw_to_standard(raw)
        validate_standard_chart(standard)
        _write_json(dst, standard)
        print_log(f"v1bin2json: {src} -> {dst}  (key='{key}', notes={len(raw['notes'])}, events={len(raw['events'])})")
    elif cmd == "roundtrip":
        src = Path(sys.argv[2])
        key = sys.argv[3]
        data = src.read_bytes()
        raw = raw_decode(data, key)
        out = pack_chart(raw_to_standard(raw), key)
        diffs = sum(1 for a, b in zip(data, out) if a != b) + abs(len(data) - len(out))
        print_log(f"roundtrip {src.name} (key='{key}'): orig={len(data)} re-packed={len(out)} byteDiffs={diffs}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
