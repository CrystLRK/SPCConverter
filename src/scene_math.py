"""Finite typed scene expressions; no eval, Unity, clock sampling or game writes.

Nodes are evaluated in their declared precision.  A missing signal is an error,
never an invented zero or a synthetic judgment.  Native kernels are an explicit
allowlist with independently recorded provenance in the version data catalog.
"""
from __future__ import annotations

import math
import struct
from typing import Any, Mapping

from scene_program import KERNEL_TYPES, SceneError, validate_graph


def float32(value: float) -> float:
    try:
        result = struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error) as exc:
        raise SceneError("scene float32溢出") from exc
    if not math.isfinite(result):
        raise SceneError("scene 非有限float32结果")
    return result


def typed(value: Any, kind: str) -> Any:
    if kind == "bool":
        if type(value) is not bool:
            raise SceneError("scene bool输入类型不匹配")
        return value
    if kind in ("vec4", "color"):
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise SceneError("scene 需要四分量输入")
        return tuple(float32(v) for v in value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SceneError("scene 需要有限数值")
    if kind == "float32":
        return float32(value)
    if kind == "float64":
        return float(value)
    if kind == "int32" and int(value) == value and -0x80000000 <= value <= 0x7FFFFFFF:
        return int(value)
    raise SceneError("scene 数值类型或int32范围不符")


def r2(frame_count: int) -> tuple[float, float, float, float]:
    frame_count = typed(frame_count, "int32")
    # Current IDA 0x180693CC0: double products -> double Floor -> cvtpd2ps.
    x = float(frame_count) * 0.7548776662466927
    y = float(frame_count) * 0.5698402909980532
    return float32(x - math.floor(x)), float32(y - math.floor(y)), 0.0, 0.0


def ease_value(name: str, value: float) -> float:
    u = min(1.0, max(0.0, value))
    if name == "linear":
        return u
    if name == "sinein":
        return 1.0 - math.cos(u * math.pi / 2.0)
    if name == "sineout":
        return math.sin(u * math.pi / 2.0)
    if name == "sineinout":
        return (1.0 - math.cos(math.pi * u)) / 2.0
    if name == "cubicin":
        return u * u * u
    if name == "cubicout":
        return 1.0 - (1.0 - u) ** 3
    if name == "cubicinout":
        return 4.0 * u ** 3 if u < 0.5 else 1.0 - ((2.0 - 2.0 * u) ** 3) / 2.0
    raise SceneError("scene 未支持ease " + str(name))


def evaluate_graph(graph: dict, signals: Mapping[str, Any], declarations: dict) -> Any:
    """Evaluate one already bounded DAG; inputs are one immutable signal snapshot."""
    validate_graph(graph, declarations, "evaluate")
    values: dict[str, Any] = {}
    for node in graph["nodes"]:
        op, kind = node["op"], node["valueType"]
        args = [values[i] for i in node.get("inputs", [])]
        if op == "const":
            value = node["value"]
        elif op == "signal":
            signal_id = node["signalId"]
            if signal_id not in signals:
                raise SceneError("scene 信号不可用 " + signal_id)
            value = signals[signal_id]
            if "field" in node:
                if not isinstance(value, Mapping) or node["field"] not in value:
                    raise SceneError("scene 事件字段不可用 " + node["field"])
                value = value[node["field"]]
        elif op == "kernel":
            kernel = node["kernel"]
            if kernel not in KERNEL_TYPES or KERNEL_TYPES[kernel] != (len(args), kind):
                raise SceneError("scene 未支持或类型不符的纯数学核 " + kernel)
            if node.get("params"):
                raise SceneError("scene 纯数学核不接受未声明参数")
            if kernel == "native107.r2":
                value = r2(args[0])
            elif kernel == "native107.smoothstep":
                v = float32(args[0])
                # Exact source grouping: (3 - (v+v)) * (v*v), all float32.
                value = float32(float32(3.0 - float32(v + v)) * float32(v * v))
            elif kernel == "compose.vec4":
                value = args
            else:
                value = args[0]
        elif op == "compare":
            a, b = args
            operator = node["params"]["operator"]
            value = {"eq": lambda: a == b, "ne": lambda: a != b,
                     "lt": lambda: a < b, "le": lambda: a <= b,
                     "gt": lambda: a > b, "ge": lambda: a >= b}[operator]()
        elif op == "select":
            value = args[1] if args[0] else args[2]
        elif op == "not":
            value = not args[0]
        elif op in ("and", "or"):
            value = (args[0] and args[1]) if op == "and" else (args[0] or args[1])
        elif op == "ease":
            value = ease_value(node["params"]["ease"], args[0])
        else:
            def scalar(*a: float) -> float:
                if op == "sum":
                    return a[0] + a[1]
                if op == "subtract":
                    return a[0] - a[1]
                if op == "product":
                    return a[0] * a[1]
                if op == "divide":
                    if a[1] == 0:
                        raise SceneError("scene 表达式除零")
                    return a[0] / a[1]
                if op == "clamp":
                    if a[1] > a[2]:
                        raise SceneError("scene clamp边界倒置")
                    return min(a[2], max(a[1], a[0]))
                if op == "lerp":
                    cast = float32 if kind in ("float32", "vec4", "color") else float
                    return cast(cast(a[1] - a[0]) * a[2]) + a[0]
                if op == "sin":
                    return math.sin(a[0])
                if op == "cos":
                    return math.cos(a[0])
                if op == "floor":
                    return math.floor(a[0])
                if op == "ceil":
                    return math.ceil(a[0])
                if op == "frac":
                    return a[0] - math.floor(a[0])
                if op == "negate":
                    return -a[0]
                raise SceneError("scene 未支持op " + op)
            if kind in ("vec4", "color"):
                value = [scalar(*(a[i] if isinstance(a, (tuple, list)) else a for a in args)) for i in range(4)]
            else:
                value = scalar(*args)
        values[node["id"]] = typed(value, kind)
    return values[graph["output"]]


def evaluate_curve(curve: dict, local_ms: float, signals: Mapping[str, Any], declarations: dict) -> Any:
    """No sampling export: retain keys/expressions and evaluate at requested time."""
    typed(local_ms, "float64")
    if curve["kind"] == "expression":
        return evaluate_graph({k: curve[k] for k in ("valueType", "unit", "nodes", "output")}, signals, declarations)
    keys = curve["keys"]
    t = local_ms
    policy = curve["before"] if t < keys[0]["timeMs"] else curve["after"]
    if t < keys[0]["timeMs"] or t > keys[-1]["timeMs"]:
        if policy == "zero":
            return typed([0.0] * 4 if curve["valueType"] in ("vec4", "color") else 0, curve["valueType"])
        if policy == "loop":
            t = keys[0]["timeMs"] + (t - keys[0]["timeMs"]) % curve["periodMs"]
    if t <= keys[0]["timeMs"]:
        return typed(keys[0]["value"], curve["valueType"])
    for left, right in zip(keys, keys[1:]):
        if t <= right["timeMs"]:
            u = ease_value(curve["ease"], (t - left["timeMs"]) / (right["timeMs"] - left["timeMs"]))
            a, b = left["value"], right["value"]
            if isinstance(a, list):
                result = [x + (y - x) * u for x, y in zip(a, b)]
            else:
                result = a + (b - a) * u
            return typed(result, curve["valueType"])
    return typed(keys[-1]["value"], curve["valueType"])
