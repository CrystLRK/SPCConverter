"""Namespaced special-screen contracts; old screen IDs are never reassigned."""
from __future__ import annotations

from fractions import Fraction
import json
import math
from pathlib import Path
import struct


CATALOG = json.loads((Path(__file__).resolve().parents[1] /
                      "data/special-screen/infalsus-107.json").read_text(encoding="utf-8"))
BY_NAME = {item["name"]: item for item in CATALOG["effects"]}
BY_ID = {item["id"]: item for item in CATALOG["effects"]}
LEGACY = frozenset(CATALOG["legacyRejected"])
CLEAR = 10
SET = 0
STOP = 1
MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF


def parameter_pair(value):
    """Store an exact float32 value in the existing signed rational fields."""
    if isinstance(value, bool):
        raise ValueError("screen 参数不能是布尔值")
    try:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("screen 参数必须为有限数")
        rounded = struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error) as exc:
        raise ValueError("screen 参数超出 float32 范围") from exc
    if not math.isfinite(rounded):
        raise ValueError("screen 参数超出 float32 范围")
    pair = Fraction.from_float(rounded)
    if not (-0x80000000 <= pair.numerator <= 0x7FFFFFFF and pair.denominator <= 0x7FFFFFFF):
        candidate = pair.limit_denominator(1_000_000)
        if (not -0x80000000 <= candidate.numerator <= 0x7FFFFFFF or
                struct.unpack("<f", struct.pack("<f", float(candidate)))[0] != rounded):
            raise ValueError("screen 参数不能由当前 SPC 分数字段无损承载")
        pair = candidate
    return [pair.numerator, pair.denominator]


def parse_arguments(args, round_ms):
    """Return (packed ID/operation, four parameter pairs, transition ms)."""
    if len(args) < 4:
        raise ValueError("screen 需要来源前缀模式、参数和过渡时间")
    name = args[2].strip().lower()
    if name in LEGACY:
        raise ValueError(f"旧 screen 模式 {name} 已暂时弃用，请迁移为带来源前缀的新模式；不自动近似替换")
    if name == "clear":
        if len(args) != 4 or round_ms(args[3]) != 0:
            raise ValueError("新 screen clear 仅支持即时清除：scenecontrol(t,screen,clear,0)")
        return CLEAR, [[0, 1] for _ in range(4)], 0
    operation = SET
    if name == "stop":
        if len(args) != 4:
            raise ValueError("停止语法为 scenecontrol(t,screen,stop,来源.模式)")
        name = args[3].strip().lower()
        operation = STOP
    spec = BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"未知或未支持的专属 screen 模式：{name}")
    if operation == STOP:
        return spec["id"] | (STOP << 32), [[0, 1] for _ in range(4)], 0
    count = len(spec["parameters"])
    if len(args) != count + 4:
        raise ValueError(f"screen {name} 需要 {count} 个效果参数和末尾过渡时间")
    duration = round_ms(args[-1])
    if duration < 0:
        raise ValueError("screen 过渡时间不能为负数")
    validate_parameters(spec, [float(v) for v in args[3:-1]], duration)
    pairs = [parameter_pair(value) for value in args[3:-1]]
    pairs.extend([[0, 1] for _ in range(4 - len(pairs))])
    return spec["id"], pairs, duration


def validate_payload(packed, duration, values):
    packed = int(packed) & MASK64
    effect, operation = packed & MASK32, packed >> 32
    if effect == CLEAR:
        if operation or duration != 0 or any(values):
            raise ValueError("新 screen clear 必须为即时、空参数载荷")
        return
    spec = BY_ID.get(effect)
    if spec is None:
        if 1 <= effect <= 9:
            raise ValueError("旧九种 screen 已暂时弃用，请从源谱迁移，旧编号不会解释为新效果")
        raise ValueError(f"不支持的专属 screen effect ID：{effect}")
    if operation not in (SET, STOP) or duration < 0:
        raise ValueError("专属 screen 操作或过渡时间无效")
    count = len(spec["parameters"]) if operation == SET else 0
    if operation == STOP and duration != 0:
        raise ValueError("screen stop 必须即时停止")
    if any(values[count:]):
        raise ValueError("专属 screen 载荷包含多余参数")
    if operation == SET:
        validate_parameters(spec, values[:count], duration)
    for value in values[:count]:
        parameter_pair(value)


def serialize_payload(time_ms, packed, duration, values, number):
    validate_payload(packed, duration, values)
    effect = int(packed) & MASK32
    operation = (int(packed) & MASK64) >> 32
    if effect == CLEAR:
        return f"scenecontrol({time_ms},screen,clear,0)"
    spec = BY_ID[effect]
    if operation == STOP:
        return f"scenecontrol({time_ms},screen,stop,{spec['name']})"
    tail = [_parameter_text(v) for v in values[:len(spec["parameters"])]]
    tail.append(number(duration))
    return f"scenecontrol({time_ms},screen,{spec['name']},{','.join(tail)})"


def _parameter_text(value):
    """Shortest decimal that retains the exact shader float32 input."""
    bits = struct.pack("<f", value)
    rounded = struct.unpack("<f", bits)[0]
    for precision in range(1, 10):
        text = format(rounded, f".{precision}g")
        if struct.pack("<f", float(text)) == bits:
            return text
    raise ValueError("screen 参数不能无损反转为 float32 文本")


def validate_parameters(spec, values, duration):
    for p, value in zip(spec["parameters"], values):
        parameter_pair(value)
        rounded = struct.unpack("<f", struct.pack("<f", value))[0]
        lower = struct.unpack("<f", struct.pack("<f", p.get("min", -math.inf)))[0]
        upper = struct.unpack("<f", struct.pack("<f", p.get("max", math.inf)))[0]
        if rounded < lower or rounded > upper:
            raise ValueError(f"screen {spec['name']} 参数 {p['property']} 越界")
        if p.get("discrete") and (value != int(value) or duration != 0):
            raise ValueError(f"screen {spec['name']} 离散参数必须是整数且过渡时间为0")
