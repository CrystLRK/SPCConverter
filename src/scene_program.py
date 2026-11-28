"""Strict, side-effect-free scene protocol shared by import and offline export.

This module never loads Unity assets or executes game code.  Mode 4 markers
identify actions; their rounded time is only a transport hint.  Definitions
and exact times remain in the matching scene program.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import struct
from pathlib import Path
from typing import Any

SCENE_FLAGS = (0x53434E << 8) | 4
SCHEMA_VERSION = 1
COMPILER_VERSION = "scene-2"
TABLES = ("resources", "instances", "poses", "cameraPoses", "curves", "signals", "rules")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
EASES = {"linear", "sinein", "sineout", "sineinout", "cubicin", "cubicout", "cubicinout"}
SCREEN_ARITIES = {"blur": 1, "crt": 1, "chromatic": 4, "glass": 5, "analogglitch": 5, "clear": 0}
VALUE_TYPES = {"float32", "float64", "int32", "bool", "vec4", "color", "recordEvent"}
KERNEL_TYPES = {"cast.float32": (1, "float32"), "cast.float64": (1, "float64"),
                "native107.r2": (1, "vec4"), "native107.smoothstep": (1, "float32"),
                "compose.vec4": (4, "vec4")}
MAX_ACTIONS = 1_000_000
MAX_DEFINITIONS = 65_536
MAX_JSON_BYTES = 64 * 1024 * 1024


class SceneError(ValueError):
    """The scene input cannot be used without changing its meaning."""


def _fail(where: str, reason: str) -> None:
    raise SceneError(f"scene {where}: {reason}")


def _object(value: Any, where: str, allowed: set[str] | None = None) -> dict:
    if not isinstance(value, dict):
        _fail(where, "必须为对象")
    if allowed is not None and value.keys() - allowed:
        _fail(where, "未知字段 " + ", ".join(sorted(value.keys() - allowed)))
    return value


def _id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _fail(where, "标识符须为ASCII字母/数字/下划线/点/连字符，且不以数字开头")
    return value


def _number(value: Any, where: str, minimum: float | None = None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(where, "必须为有限数值")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (minimum is not None and value < minimum):
        _fail(where, "数值越界或非有限")
    return value


def _integer(value: Any, where: str, minimum: int = 0, maximum: int = 0x7FFFFFFF) -> int:
    _number(value, where)
    if int(value) != value or not minimum <= value <= maximum:
        _fail(where, f"必须为{minimum}..{maximum}内整数")
    return int(value)


def marker_time(value: Any) -> int:
    number = _number(value, "timeMs", 0)
    result = int(number + 0.5)
    if result > 0xFFFFFFFF:
        _fail("timeMs", "取整后超出V1 uint32")
    return result


def canonical_bytes(value: Any) -> bytes:
    """Sorted UTF-8 JSON, finite numbers, preserved numeric token precision.

    Consumers verify the same sorted representation, retaining each incoming
    number token; they must not round all numbers through an integer or float32.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest().upper()


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            _fail("JSON", f"重复键 {key}")
        obj[key] = value
    return obj


def read_json(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) > MAX_JSON_BYTES:
        _fail(str(path), "JSON超出64MiB上限")
    def invalid_constant(value: str) -> None:
        _fail(str(path), f"非有限JSON常量 {value}")
    try:
        root = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_pairs,
                          parse_constant=invalid_constant)
        canonical_bytes(root)  # Also rejects unpaired Unicode surrogates.
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SceneError(f"scene JSON无法读取 {path}: {exc}") from exc
    return _object(root, str(path))


def sidecar_path(chart: Path) -> Path:
    return chart.with_name(chart.stem + ".scene.json")


def load_catalog(target: dict, explicit: Path | None = None) -> dict:
    version = _id(target.get("catalogVersion"), "catalogVersion")
    catalog_path = explicit or Path(__file__).resolve().parents[1] / "data" / "scene" / (version + ".json")
    return read_json(catalog_path)


def is_marker(note: dict) -> bool:
    return int(note.get("type", 0)) == 3 and int(note.get("flags", 0)) & 0xFFFFFFFF == SCENE_FLAGS


def marker_for(action: dict, index: int) -> dict:
    return {"index": index, "line": action["id"], "type": 3, "track": 4,
            "timeMs": marker_time(action["timeMs"]), "endMs": 0,
            "fractions": [[0, 1], [0, 1], [0, 1], [0, 1]], "flags": SCENE_FLAGS}


def _typed_value(value: Any, value_type: str, where: str) -> None:
    if value_type == "bool":
        if not isinstance(value, bool):
            _fail(where, "需要bool值")
    elif value_type in ("vec4", "color"):
        if not isinstance(value, list) or len(value) != 4:
            _fail(where, "需要四个分量")
        for item in value:
            _number(item, where)
    elif value_type == "int32":
        _integer(value, where, -0x80000000)
    else:
        _number(value, where)
        if value_type == "float32":
            try:
                if not math.isfinite(struct.unpack("<f", struct.pack("<f", value))[0]):
                    _fail(where, "float32溢出")
            except (OverflowError, struct.error):
                _fail(where, "float32溢出")


def validate_graph(graph: Any, signals: dict, where: str) -> None:
    graph = _object(graph, where, {"valueType", "unit", "nodes", "output"})
    if graph.get("valueType") not in VALUE_TYPES - {"recordEvent"}:
        _fail(where, "未知表达式值类型")
    if not isinstance(graph.get("unit"), str) or not graph["unit"]:
        _fail(where, "须声明单位")
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not 0 < len(nodes) <= MAX_DEFINITIONS:
        _fail(where, "nodes须为有界非空拓扑数组")
    prior: dict[str, dict] = {}
    arities = {"sum": 2, "product": 2, "subtract": 2, "divide": 2,
               "compare": 2, "select": 3, "clamp": 3, "lerp": 3,
               "sin": 1, "cos": 1, "floor": 1, "ceil": 1, "frac": 1,
               "negate": 1, "not": 1, "and": 2, "or": 2, "ease": 1}
    for node in nodes:
        node = _object(node, where, {"id", "op", "valueType", "unit", "value",
                                    "signalId", "field", "inputs", "params", "kernel"})
        node_id = _id(node.get("id"), where)
        if node_id in prior or node.get("valueType") not in VALUE_TYPES:
            _fail(where, "重复节点或未知类型")
        op = node.get("op")
        inputs = node.get("inputs", [])
        if not isinstance(inputs, list) or any(i not in prior for i in inputs):
            _fail(where, "节点引用不存在/未先定义，或含循环")
        if op == "const":
            if inputs:
                _fail(where, "const不接受输入")
            _typed_value(node.get("value"), node["valueType"], where)
        elif op == "signal":
            signal = signals.get(node.get("signalId"))
            if signal is None or inputs:
                _fail(where, "信号引用不存在或signal含输入")
            if node.get("field") is None and node["valueType"] != signal["valueType"]:
                _fail(where, "信号类型不匹配")
            if node.get("field") is not None:
                fields = signal.get("fields", {})
                if node["field"] not in fields or fields[node["field"]] != node["valueType"]:
                    _fail(where, "事件字段没有已验证类型声明")
        elif op == "kernel":
            kernel = _id(node.get("kernel"), where)
            if KERNEL_TYPES.get(kernel) != (len(inputs), node["valueType"]):
                _fail(where, "纯数学核未支持或参数/类型不符")
            if node.get("params", {}) != {}:
                _fail(where, "纯数学核不接受未声明参数")
            kinds = [prior[i]["valueType"] for i in inputs]
            if kernel == "native107.r2" and kinds != ["int32"]:
                _fail(where, "R2需要原始int32帧计数")
            if kernel == "native107.smoothstep" and kinds != ["float32"]:
                _fail(where, "原生smoothstep需要float32")
            if kernel == "compose.vec4" and kinds != ["float32"] * 4:
                _fail(where, "vec4组装需要四个float32")
            if kernel.startswith("cast.") and kinds[0] not in ("float32", "float64", "int32"):
                _fail(where, "数值转换不能接收bool/事件/向量")
        elif op in arities:
            if len(inputs) != arities[op]:
                _fail(where, f"{op}输入数不符")
            if op in ("compare", "not", "and", "or") and node["valueType"] != "bool":
                _fail(where, "逻辑/比较输出须为bool")
            if op in ("not", "and", "or") and any(prior[i]["valueType"] != "bool" for i in inputs):
                _fail(where, "逻辑操作需要bool输入")
            if op == "select" and (prior[inputs[0]]["valueType"] != "bool" or
                    any(prior[i]["valueType"] != node["valueType"] for i in inputs[1:])):
                _fail(where, "select类型不一致")
            if op == "compare":
                params = _object(node.get("params", {}), where, {"operator"})
                if params.get("operator") not in ("eq", "ne", "lt", "le", "gt", "ge"):
                    _fail(where, "比较须明确operator")
                kinds = [prior[i]["valueType"] for i in inputs]
                if kinds[0] != kinds[1] or kinds[0] not in ("float32", "float64", "int32", "bool"):
                    _fail(where, "比较需要相同标量类型")
                if kinds[0] == "bool" and params["operator"] not in ("eq", "ne"):
                    _fail(where, "bool只允许相等比较")
            if op == "ease" and node.get("params", {}).get("ease") not in EASES:
                _fail(where, "未知ease")
            if op not in ("compare", "not", "and", "or", "select"):
                kind = node["valueType"]
                kinds = [prior[i]["valueType"] for i in inputs]
                numeric = ("float32", "float64", "int32", "vec4", "color")
                if kind not in numeric:
                    _fail(where, "数学操作不能产生bool/事件")
                if op == "lerp":
                    valid = kinds[:2] == [kind, kind] and kinds[2] in ("float32", "float64")
                else:
                    valid = all(k == kind for k in kinds)
                if not valid:
                    _fail(where, "数学操作类型不一致，须显式转换")
                if op in ("sin", "cos", "floor", "ceil", "frac", "ease") and kind not in ("float32", "float64"):
                    _fail(where, "该数学操作需要浮点标量")
        else:
            _fail(where, f"不支持的表达式op {op}")
        prior[node_id] = node
    output = prior.get(graph.get("output"))
    if output is None or output["valueType"] != graph["valueType"]:
        _fail(where, "表达式输出不存在或类型不匹配")


def validate_action(action: dict, definitions: dict | None = None) -> dict:
    action = _object(action, "action", {"id", "timeMs", "markerTimeMs", "sourceOrder",
                                       "domain", "operation", "args"})
    result = copy.deepcopy(action)
    result["id"] = _integer(action.get("id"), "action.id", 1)
    result["sourceOrder"] = _integer(action.get("sourceOrder"), "action.sourceOrder")
    result["markerTimeMs"] = marker_time(action.get("timeMs"))
    if "markerTimeMs" in action and action["markerTimeMs"] != result["markerTimeMs"]:
        _fail("action", "精确时间与marker提示不一致")
    domain, operation, args = action.get("domain"), action.get("operation"), action.get("args")
    if not isinstance(args, list):
        _fail("action.args", "需要数组")
    arities = {("material", "bind"): 2, ("material", "float"): 5,
               ("material", "vec4"): 8, ("material", "color"): 8,
               ("material", "keyword"): 3, ("material", "curve"): 3,
               ("material", "unbind"): 2, ("video", "play"): 5,
               ("video", "pause"): 1, ("video", "resume"): 1,
               ("video", "seek"): 2, ("video", "stop"): 1,
               ("object", "visible"): 2, ("object", "pose"): 4,
               ("object", "color"): 7, ("camera", "pose"): 4,
               ("rule", "enable"): 1, ("rule", "disable"): 1,
               ("instance", "prepare"): 1, ("instance", "clear"): 2,
               ("instance", "release"): 1}
    if (domain, operation) == ("screen", "set"):
        if len(args) < 3 or args[1] not in SCREEN_ARITIES or len(args) != SCREEN_ARITIES[args[1]] + 3:
            _fail("screen.set", "效果或参数个数错误")
        for value in args[2:]:
            _number(value, "screen参数")
        _number(args[-1], "durationMs", 0)
    elif (domain, operation) not in arities or len(args) != arities[domain, operation]:
        _fail("action", f"不支持的操作/参数个数 {domain}/{operation}")
    _id(args[0], "action.target")
    if definitions is not None:
        table = "rules" if domain == "rule" else "instances"
        if args[0] not in definitions[table]:
            _fail("action.target", "定义不存在")
    if domain == "material":
        _id(args[1], "material属性/资源")
        if operation in ("float", "vec4", "color"):
            for value in args[2:-1]:
                _number(value, "material参数")
            _number(args[-2], "durationMs", 0)
        if operation == "keyword":
            _integer(args[2], "keyword.flag", 0, 1)
        if operation == "bind" and definitions is not None:
            if args[1] not in definitions["resources"]:
                _fail("material.bind", "素材不存在")
        if operation == "curve":
            _id(args[2], "curveId")
            if definitions is not None and args[2] not in definitions["curves"]:
                _fail("material.curve", "曲线不存在")
    if (domain in ("material", "object") and operation in ("float", "vec4", "color")) or operation == "pose":
        if args[-1] not in EASES:
            _fail("action.ease", "未知ease")
        _number(args[-2], "durationMs", 0)
        if operation == "color":
            offset = 2 if domain == "material" else 1
            for value in args[offset:offset + 4]:
                _number(value, "rgba")
            if not 0 <= args[offset + 3] <= 1:
                _fail("alpha", "需要0..1")
    if operation == "pose":
        _id(args[1], "poseId")
        table = "cameraPoses" if domain == "camera" else "poses"
        if definitions is not None and args[1] not in definitions[table]:
            _fail("pose", "姿态定义不存在")
    if (domain, operation) == ("object", "visible"):
        _integer(args[1], "visible.flag", 0, 1)
    if domain == "video":
        if operation == "play":
            _id(args[1], "video资源")
            _number(args[2], "clipStartMs", 0)
            if args[3] not in ("once", "loop") or args[4] not in ("muted", "embedded", "paired"):
                _fail("video.play", "循环/音频模式错误")
            if definitions is not None:
                resource = definitions["resources"].get(args[1], {})
                if resource.get("kind") != "nativeVideo":
                    _fail("video.play", "需要视频资源")
                if args[4] == "paired" and not resource.get("pairedAudio"):
                    _fail("video.play", "paired缺配对音频")
        elif operation == "seek":
            _number(args[1], "clipPositionMs", 0)
    if (domain, operation) == ("instance", "clear"):
        _number(args[1], "durationMs", 0)
    result["timeMs"] = float(result["timeMs"])
    result["args"] = [float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                      else value for value in args]
    return result


def parse_action(parts: list[str], action_id: int, source_order: int) -> dict:
    if len(parts) < 5 or parts[1] != "scene":
        _fail("语句", "需要scene命名空间、domain、operation和参数")
    def atom(value: str) -> Any:
        if IDENTIFIER.fullmatch(value):
            return value
        try:
            return float(value)
        except ValueError as exc:
            raise SceneError(f"scene 非法参数 {value}") from exc
    try:
        time = float(parts[0])
    except ValueError as exc:
        raise SceneError("scene time不是数值") from exc
    return validate_action({"id": action_id, "timeMs": time, "sourceOrder": source_order,
                            "domain": parts[2], "operation": parts[3], "args": [atom(p) for p in parts[4:]]})


def validate_source(root: Any) -> dict:
    root = _object(root, "source", {"schemaVersion", "kind", "target", "source", *TABLES})
    if root.get("schemaVersion") != SCHEMA_VERSION or root.get("kind") != "source":
        _fail("source", "需要schemaVersion=1/kind=source")
    target = _object(root.get("target"), "target", {"gameAssemblySha256", "catalogVersion", "requiredCapabilities"})
    if not isinstance(target.get("gameAssemblySha256"), str) or not SHA256.fullmatch(target["gameAssemblySha256"]):
        _fail("target", "需要明确游戏DLL SHA256")
    _id(target.get("catalogVersion"), "catalogVersion")
    for capability in target.get("requiredCapabilities", []):
        _id(capability, "requiredCapabilities")
    result = copy.deepcopy(root)
    for table in TABLES:
        entries = _object(result.setdefault(table, {}), table)
        if len(entries) > MAX_DEFINITIONS:
            _fail(table, "定义数量超出上限")
        for key in entries:
            _id(key, table)
    resources, signals = result["resources"], result["signals"]
    for resource_id, resource in resources.items():
        _object(resource, resource_id, {"kind", "lookup", "expectedIdentity", "pairedAudio", "description"})
        kind = resource.get("kind")
        lookup_fields = {"nativeAsset": {"addressablesKey"}, "nativePrefab": {"addressablesKey"},
                         "nativeVideo": {"videoFileName"}, "materialMember": {"container", "member"}}
        if kind not in lookup_fields:
            _fail(resource_id, "未知资源类型")
        lookup = _object(resource.get("lookup"), resource_id + ".lookup", lookup_fields[kind])
        if set(lookup) != lookup_fields[kind] or any(not isinstance(v, str) or not v for v in lookup.values()):
            _fail(resource_id, "lookup字段缺失/无效")
        if kind == "materialMember" and lookup["container"] not in resources:
            _fail(resource_id, "容器不存在")
        if resource.get("pairedAudio") is not None and resource["pairedAudio"] not in resources:
            _fail(resource_id, "配对音频引用不存在")
        identity = resource.get("expectedIdentity")
        if identity is not None:
            _object(identity, resource_id + ".expectedIdentity")
            for key, value in identity.items():
                if key.lower().endswith("pathid") and (not isinstance(value, str) or not re.fullmatch(r"-?\d+", value)):
                    _fail(resource_id, "64位PathID必须使用十进制字符串")
    visiting, visited = set(), set()
    def visit(key: str) -> None:
        if key in visiting:
            _fail(key, "资源引用环")
        if key in visited:
            return
        visiting.add(key)
        lookup = resources[key]["lookup"]
        for dependency in (lookup.get("container"), resources[key].get("pairedAudio")):
            if dependency:
                visit(dependency)
        visiting.remove(key)
        visited.add(key)
    for resource_id in resources:
        visit(resource_id)
    for instance_id, instance in result["instances"].items():
        _object(instance, instance_id, {"kind", "resource", "rendererBackend", "initial", "order", "renderContract", "description"})
        if instance.get("kind") not in ("background", "object", "material", "video", "screen", "camera"):
            _fail(instance_id, "未知实例类型")
        if instance.get("rendererBackend") not in ("PrefabRenderer", "GraphPass"):
            _fail(instance_id, "必须明确一个绘制后端")
        if instance.get("resource") is not None and instance["resource"] not in resources:
            _fail(instance_id, "实例资源不存在")
        if instance["kind"] not in ("camera", "screen") and instance.get("resource") is None:
            _fail(instance_id, "实例缺资源")
        initial = _object(instance.get("initial"), instance_id + ".initial", {"material", "visible", "playing", "pose", "properties", "keywords"})
        if "material" in initial and initial["material"] not in resources:
            _fail(instance_id, "初始材质不存在")
        for flag in ("visible", "playing"):
            if flag in initial and not isinstance(initial[flag], bool):
                _fail(instance_id, "初始开关须为bool")
        _integer(instance.get("order"), instance_id + ".order", -0x80000000)
    for table in ("poses", "cameraPoses"):
        for pose_id, pose in result[table].items():
            _object(pose, pose_id, {"position", "quaternion", "scale", "space", "positionUnit", "projection", "fov", "orthographicSize"})
            for field, length in (("position", 3), ("quaternion", 4), ("scale", 3)):
                value = pose.get(field)
                if not isinstance(value, list) or len(value) != length:
                    _fail(pose_id, "需要完整position/quaternion/scale")
                for number in value:
                    _number(number, pose_id)
            if pose.get("space") not in ("local", "scene") or not any(pose["quaternion"]):
                _fail(pose_id, "姿态空间/四元数无效")
            if table == "cameraPoses":
                if pose.get("projection") not in ("perspective", "orthographic"):
                    _fail(pose_id, "须明确projection")
                _number(pose.get("fov"), pose_id, 0)
                _number(pose.get("orthographicSize"), pose_id, 0)
    for signal_id, signal in signals.items():
        _object(signal, signal_id, {"source", "valueType", "unit", "scope", "fields", "contract", "required", "description"})
        if signal.get("valueType") not in VALUE_TYPES:
            _fail(signal_id, "未知信号类型")
        known_types = {"chart.timeMs": "float64", "engine.frameCount": "int32", "session.recordFinalized": "recordEvent"}
        source = signal.get("source")
        if source in known_types:
            if signal["valueType"] != known_types[source]:
                _fail(signal_id, "信号值类型不符")
        elif source != "source-specific" or not signal.get("contract"):
            _fail(signal_id, "来源未明确或缺只读契约")
    for curve_id, curve in result["curves"].items():
        _object(curve, curve_id, {"kind", "valueType", "unit", "keys", "ease", "before", "after", "periodMs", "nodes", "output", "clock", "durationMs"})
        if curve.get("valueType") not in VALUE_TYPES - {"recordEvent", "bool"}:
            _fail(curve_id, "不支持的曲线值类型")
        if curve.get("before") not in ("hold", "zero", "loop") or curve.get("after") not in ("hold", "zero", "loop"):
            _fail(curve_id, "须明确外插策略")
        if "loop" in (curve["before"], curve["after"]):
            if _number(curve.get("periodMs"), curve_id, 0) == 0:
                _fail(curve_id, "循环周期必须大于0")
        if curve.get("kind") == "keyframes":
            keys = curve.get("keys")
            if not isinstance(keys, list) or not 0 < len(keys) <= MAX_DEFINITIONS or curve.get("ease") not in EASES:
                _fail(curve_id, "关键帧/ease无效")
            last = -1
            for key in keys:
                _object(key, curve_id, {"timeMs", "value"})
                time = _number(key.get("timeMs"), curve_id, 0)
                if time <= last:
                    _fail(curve_id, "关键帧时间须严格递增")
                _typed_value(key.get("value"), curve["valueType"], curve_id)
                last = time
        elif curve.get("kind") == "expression":
            validate_graph({k: curve[k] for k in ("valueType", "unit", "nodes", "output") if k in curve}, signals, curve_id)
        else:
            _fail(curve_id, "未知曲线类型")
    for rule_id, rule in result["rules"].items():
        _object(rule, rule_id, {"trigger", "signal", "condition", "priority", "actions", "description"})
        if rule.get("trigger") not in ("rise", "fall", "change", "onceWhenTrue", "perEvent"):
            _fail(rule_id, "未知触发语义")
        validate_graph(rule.get("condition"), signals, rule_id)
        if rule["condition"]["valueType"] != "bool":
            _fail(rule_id, "条件须为bool")
        _integer(rule.get("priority"), rule_id, -0x80000000)
        if rule["trigger"] == "perEvent" and signals.get(rule.get("signal"), {}).get("valueType") != "recordEvent":
            _fail(rule_id, "perEvent须引用明确事件信号")
        actions = rule.get("actions")
        if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
            _fail(rule_id, "动作模板须为有界数组")
        for index, template in enumerate(actions):
            _object(template, rule_id, {"delayMs", "domain", "operation", "args"})
            delay = _number(template.get("delayMs"), rule_id, 0)
            if template.get("domain") == "rule":
                _fail(rule_id, "规则模板不能递归修改规则")
            validate_action({"id": index + 1, "timeMs": delay, "sourceOrder": index,
                             **{k: template.get(k) for k in ("domain", "operation", "args")}}, result)
    canonical_bytes(result)
    return result


def source_from_program(program: dict) -> dict:
    definitions = copy.deepcopy(program["definitions"])
    rules = _object(definitions.get("rules"), "compiled.rules")
    ordered = []
    for name, rule in rules.items():
        _object(rule, "compiled.rule")
        order = _integer(rule.pop("sourceOrder", None), "rule.sourceOrder")
        ordered.append((order, name, rule))
    if sorted(order for order, _, _ in ordered) != list(range(len(ordered))):
        _fail("compiled.rules", "声明顺序须唯一且连续")
    definitions["rules"] = {name: rule for _, name, rule in sorted(ordered)}
    return {"schemaVersion": SCHEMA_VERSION, "kind": "source", "target": copy.deepcopy(program["target"]), **definitions}


def editable_source(scene: dict) -> dict:
    """Remove the standard-chart action attachment, never accept it in a sidecar."""
    return validate_source({k: v for k, v in scene.items() if k != "actions"})


def validate_chart_scene(chart: dict, required: bool = True) -> dict | None:
    markers = [note for note in chart.get("notes", []) if is_marker(note)]
    scene = chart.get("scene")
    if not markers:
        if scene and scene.get("actions", scene.get("compiledActions", [])):
            _fail("chart", "动作存在但缺mode4 marker")
        return None
    if scene is None:
        if not required:
            return None
        _fail("chart", "mode4必须携带匹配scene清单")
    _object(scene, "chart.scene")
    definitions = editable_source(scene) if scene.get("kind") == "source" else validate_source(source_from_program(scene))
    actions = scene.get("actions", scene.get("compiledActions", []))
    if not isinstance(actions, list) or not 0 < len(actions) <= MAX_ACTIONS:
        _fail("chart", "动作列表缺失/超限")
    by_id: dict = {}
    orders = set()
    for action in actions:
        action = validate_action(action, definitions)
        if action["id"] in by_id or action["sourceOrder"] in orders:
            _fail("chart", "重复actionId/sourceOrder")
        by_id[action["id"]] = action
        orders.add(action["sourceOrder"])
    marker_ids = set()
    for note in markers:
        action_id = _integer(note.get("line"), "marker.actionId", 1)
        action = by_id.get(action_id)
        if action_id in marker_ids or action is None:
            _fail("marker", "重复marker或无对应动作")
        expected = marker_for(action, int(note.get("index", 0)))
        for key in ("type", "track", "timeMs", "endMs", "fractions"):
            if note.get(key) != expected[key]:
                _fail("marker", f"载荷字段不一致 {key}")
        marker_ids.add(action_id)
    if marker_ids != set(by_id):
        _fail("marker", "动作缺marker")
    return scene


def serialize_action(action: dict) -> str:
    values = [action["timeMs"], "scene", action["domain"], action["operation"], *action["args"]]
    def text(value: Any) -> str:
        if isinstance(value, str):
            return value
        _number(value, "action")
        return str(int(value)) if int(value) == value else repr(value)
    return "scenecontrol(" + ",".join(text(v) for v in values) + ")"


def compile_program(scene: dict, payload: bytes, parser_key: str, catalog: dict) -> dict:
    source = editable_source(scene)
    if catalog.get("catalogVersion") != source["target"]["catalogVersion"] or catalog.get("gameAssemblySha256", "").upper() != source["target"]["gameAssemblySha256"].upper():
        _fail("catalog", "游戏版本或目录版本不匹配")
    definitions = {k: source[k] for k in TABLES}
    # Canonical JSON sorts keys: protect author rule order as explicit hashed data.
    for source_order, rule in enumerate(definitions["rules"].values()):
        rule["sourceOrder"] = source_order
    for resource_id, resource in definitions["resources"].items():
        if resource["kind"] == "materialMember":
            continue
        candidates = [r for r in catalog.get("resources", []) if r.get("kind") == resource["kind"] and r.get("lookup") == resource["lookup"]]
        if len(candidates) != 1:
            _fail(resource_id, "素材在匹配目录中不唯一/缺失")
        identity = candidates[0]["identity"]
        if "expectedIdentity" in resource and resource["expectedIdentity"] != identity:
            _fail(resource_id, "素材身份不符")
        resource["expectedIdentity"] = copy.deepcopy(identity)
    for resource_id, resource in definitions["resources"].items():
        if resource["kind"] != "materialMember":
            continue
        parent = definitions["resources"][resource["lookup"]["container"]]
        member = resource["lookup"]["member"]
        matches = [r for r in catalog.get("members", []) if r.get("containerLookup") == parent["lookup"] and r.get("member") == member]
        if len(matches) != 1:
            _fail(resource_id, "材质成员身份缺失/不唯一")
        identity = matches[0]["identity"]
        if "expectedIdentity" in resource and resource["expectedIdentity"] != identity:
            _fail(resource_id, "材质成员身份不符")
        resource["expectedIdentity"] = copy.deepcopy(identity)
    actions = [validate_action(a, definitions) for a in scene.get("actions", [])]
    result = {"schemaVersion": SCHEMA_VERSION, "kind": "compiled", "compilerVersion": COMPILER_VERSION,
              "target": source["target"], "bindings": {"finalParserKey": parser_key,
              "chartPayloadSha256": hashlib.sha256(payload).hexdigest().upper(), "definitionsSha256": fingerprint(definitions)},
              "definitions": definitions, "compiledActions": actions}
    result["buildId"] = fingerprint(result)
    result["programSha256"] = fingerprint(result)
    return result


def verify_program(program: dict, payload: bytes, parser_key: str) -> None:
    _object(program, "compiled", {"schemaVersion", "kind", "compilerVersion", "buildId", "target", "bindings",
                                  "definitions", "compiledActions", "programSha256", "source", "diagnostics"})
    if program.get("kind") != "compiled" or program.get("schemaVersion") != SCHEMA_VERSION or program.get("compilerVersion") != COMPILER_VERSION:
        _fail("compiled", "不支持的编译格式/版本")
    body = {k: v for k, v in program.items() if k not in ("programSha256", "source", "diagnostics")}
    if fingerprint(body) != program.get("programSha256"):
        _fail("compiled", "程序指纹不符")
    bindings = _object(program.get("bindings"), "bindings", {"finalParserKey", "chartPayloadSha256", "definitionsSha256"})
    if bindings.get("finalParserKey") != parser_key or bindings.get("chartPayloadSha256") != hashlib.sha256(payload).hexdigest().upper():
        _fail("bindings", "SPC文件名/种子或载荷不匹配")
    if bindings.get("definitionsSha256") != fingerprint(program.get("definitions")):
        _fail("definitions", "定义指纹不符")
    validate_source(source_from_program(program))


def attach_bound_program(chart: dict, source: Path, payload: bytes, parser_key: str) -> None:
    """Attach a verified program only when this binary actually contains mode 4."""
    if not any(is_marker(note) for note in chart.get("notes", [])):
        return
    program = read_json(sidecar_path(source))
    verify_program(program, payload, parser_key)
    chart["scene"] = program
    validate_chart_scene(chart)
