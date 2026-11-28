"""Export existing screen syntax, without a new game runtime or guessed times.

Standard library only. A version-bound catalog names source scripts; the tool
reads the installed SAM files itself. Unknown song time remains null. Authors
can explicitly place selected effects on their own chart, then compile text.
"""
from __future__ import annotations

from ascii_log import print_log

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import tempfile
import uuid

KEY = bytes((0xF0, 0x16, 0x28, 0x4B, 0x7D, 0x9E, 0xC3, 0xA5))
ARITY = {"blur": 1, "crt": 1, "chromatic": 4, "glass": 5, "analogglitch": 5, "clear": 0}
SCHEMA = 1
DATA = Path(__file__).resolve().parents[1] / "data/legacy-screen/infalsus-107.json"


class ScreenExportError(ValueError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest().upper()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScreenExportError("JSON重复键：" + key)
        result[key] = value
    return result


def read_json(path: Path):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ScreenExportError("JSON超过64MiB")
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ScreenExportError("非有限JSON：" + value)))


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")


def finite(value, label, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ScreenExportError(label + "必须为有限数值")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ScreenExportError(label + "越界")
    return value


def code_lines(text):
    """Keep source line numbers; do not read multiline dialogue as commands."""
    quote, block = None, False
    for number, raw in enumerate(text.splitlines(), 1):
        clean, i = [], 0
        while i < len(raw):
            char, pair = raw[i], raw[i:i + 2]
            if block:
                if pair == "*/": block, i = False, i + 2
                else: i += 1
            elif quote:
                if char == "\\": i += 2
                elif char == quote: quote, i = None, i + 1
                else: i += 1
            elif pair == "//":
                break
            elif pair == "/*":
                block, i = True, i + 2
            elif char in ('"', '`'):
                quote = char
                clean.append(" ")
                i += 1
            else:
                clean.append(char)
                i += 1
        yield number, "".join(clean).strip(), raw.strip()


def runtime_notes(effect, params):
    notes = []
    if effect != "clear":
        notes.append("当前screen已弃用旧五类名称；来源参数仅保留，不存在已证明等价的新分支映射，不能直接编译。")
    if effect == "blur":
        notes.append("旧screen的blur是0..1强度并乘8、使用低分辨率；原剧情直接传BlurStrength且使用高分辨率，源数值仅作模板，不能标等价复现。")
    if params and not 0 <= params[0] <= 1:
        notes.append("旧运行时把首个强度钳制到0..1；本条原始值超出该范围，不能无损重现。")
    if effect in ("glass", "analogglitch"):
        notes.append("旧运行时ScreenAux4未写入，载荷参数到原生setter还有顺序/缺项差异；本批只导源参数，不改谱面参数掩盖此缺口。")
    if effect not in ("clear",):
        notes.append("原剧情图层/初态及与其它效果的叠加可能不同；模板不是完整原曲演出保证。")
    return notes


def extract_script(text: str, source: dict, songs: list) -> tuple[list, list]:
    events, unsupported = [], []
    for line, clean, raw in code_lines(text):
        match = re.fullmatch(r"effect\s+([A-Za-z_]+)\s+(.+)", clean)
        if not match:
            continue
        name = match[1]
        effect = "analogglitch" if name == "analog_glitch" else name
        base = {"id": "e_" + source["guid"] + "_" + str(line),
                "source": {"script": source["path"], "line": line, "command": raw,
                           "guid": source["guid"], "samSha256": source["sha256"]},
                "songAssociations": songs, "sourcePhase": "associatedStoryNotProvenGameplay"}
        if effect not in ARITY:
            unsupported.append({**base, "effect": effect, "reason": "现有screen未支持此命令"})
            continue
        try:
            values = [finite(float(token), "源参数") for token in match[2].split()]
            if len(values) != ARITY[effect] + 1:
                raise ScreenExportError("源参数数量不符")
            duration = finite(values[-1] * 1000, "durationMs", 0, 2147483647)
        except (ValueError, OverflowError) as exc:
            unsupported.append({**base, "effect": effect, "reason": str(exc)})
            continue
        events.append({**base, "effect": effect, "params": values[:-1], "durationMs": duration,
                       "enabled": False, "timeMs": None, "timeBasis": "unresolved",
                       "timingReason": "剧情含对白/并行/交互等待，尚未证明到歌曲毫秒轴的映射；不累加delay猜时刻。",
                       "runtimeNotes": runtime_notes(effect, values[:-1])})
    return events, unsupported


def statement(event, authored_time):
    effect, values = event["effect"], event["params"]
    if effect not in ARITY or not isinstance(values, list) or len(values) != ARITY[effect]:
        raise ScreenExportError("未知screen或参数数量错误")
    numbers = [finite(v, "screen参数") for v in values]
    numbers.append(finite(event["durationMs"], "durationMs", 0, 2147483647))
    if effect != "clear":
        raise ScreenExportError(f"旧screen {effect} 没有已证明等价的命名分支映射；请按screen作者手册迁移，不生成被解析器拒绝的旧语句")
    if numbers[-1] != 0:
        raise ScreenExportError("当前screen clear仅支持即时清除，不能无损编译原定时clear")
    return "scenecontrol(" + str(authored_time) + ",screen,clear,0)"


def _outside(output, game):
    if output == game or output.is_relative_to(game):
        raise ScreenExportError("导出目标不能写入游戏安装目录")


def _publish(stage, output):
    for attempt, delay in enumerate((0, .1, .25, .5, 1)):
        if delay: time.sleep(delay)
        if output.exists(): raise FileExistsError(str(output))
        try:
            stage.rename(output)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 4: raise


def export_screen(game_root: Path, output: Path, *, catalog_path=DATA, song=None):
    game, output = game_root.resolve(strict=True), output.resolve()
    _outside(output, game)
    if output.exists(): raise FileExistsError("保留旧输出，请指定新目录：" + str(output))
    catalog = read_json(catalog_path)
    if catalog.get("schemaVersion") != SCHEMA or catalog.get("kind") != "legacyScreenSources":
        raise ScreenExportError("不支持的旧screen来源目录")
    checked = []
    for entry in catalog["requiredFiles"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts: raise ScreenExportError("来源路径越界")
        path = (game / relative).resolve(strict=True)
        if not path.is_relative_to(game) or digest(path) != entry["sha256"].upper():
            raise ScreenExportError("游戏来源版本不匹配：" + entry["path"])
        checked.append(entry["path"])
    mappings = catalog["storyMappings"]
    if song is not None and song not in {p["songId"] for p in mappings}:
        raise ScreenExportError("未知或没有剧情映射的原歌曲ID：" + song)
    selected = {p["script"] for p in mappings if p["songId"] == song} if song else None
    all_events, unknown_commands, scripts = [], [], []
    for source in catalog["scripts"]:
        if selected is not None and source["path"] not in selected: continue
        if not re.fullmatch("[0-9a-fA-F]{32}", source["guid"]): raise ScreenExportError("非法SAM GUID")
        path = (game / "infalsus_Data/StreamingAssets/sam" / source["guid"]).resolve(strict=True)
        if not path.is_relative_to(game) or digest(path) != source["sha256"].upper():
            raise ScreenExportError("原脚本版本不匹配：" + source["path"])
        raw = path.read_bytes()
        text = bytes(v ^ KEY[i & 7] for i, v in enumerate(raw)).decode("utf-8-sig")
        refs = [{k: p[k] for k in ("songId", "storyU", "playType")} for p in mappings if p["script"] == source["path"]]
        events, unsupported = extract_script(text, source, refs)
        all_events.extend(events); unknown_commands.extend(unsupported)
        scripts.append({"script": source["path"], "events": len(events), "unsupported": len(unsupported), "songAssociations": refs})
    if selected is not None and selected - {p["script"] for p in scripts}:
        raise ScreenExportError("剧情映射缺源脚本")
    stage = output.with_name(output.name + ".stage-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    edit = {"schemaVersion": SCHEMA, "kind": "legacyScreenEdit", "readOnlyGameRoot": str(game),
            "sourceCatalogSha256": digest(catalog_path), "originalSongTimeKnown": False, "events": all_events}
    write_json(stage / "effects.screen.json", edit)
    templates = ["// 非可执行模板：歌曲时刻未定。请在JSON中选择条目、填写作者timeMs与timeBasis=author后编译。",
                 "// 这里只导真实源参数，不保证当前旧运行时能完整重现；逐条限制见JSON与未支持清单。"]
    for event in all_events:
        source = event["source"]
        templates.append("// " + event["id"] + "  " + source["script"] + ":" + str(source["line"]))
        try:
            templates.append("// " + statement(event, "<timeMs>"))
        except ScreenExportError as exc:
            templates.append("// 未支持当前语法：" + str(exc))
            templates.append("// 原来源参数：" + event["effect"] + " " + repr(event["params"]) + " durationMs=" + str(event["durationMs"]))
    (stage / "effects.screen.templates.txt").write_text("\n".join(templates) + "\n", encoding="utf-8")
    report = {"status": "parametersExportedSongTimesUnresolved", "sourceCommandCount": len(all_events),
              "executableStatementCount": 0, "byEffect": dict(Counter(e["effect"] for e in all_events)),
              "scriptCount": len(scripts), "scripts": scripts, "unknownCommands": unknown_commands,
              "verifiedInstallationFiles": checked, "songFilter": song,
              "songAssociationMeaning": "对应剧情使用，不代表歌曲内使用；所有原歌曲timeMs仍未定。"}
    report["effectScriptCount"] = sum(row["events"] > 0 for row in scripts)
    report["associatedCommandCount"] = sum(bool(e["songAssociations"]) for e in all_events)
    report["unassociatedCommandCount"] = len(all_events) - report["associatedCommandCount"]
    report["associatedSongCount"] = len({p["songId"] for e in all_events for p in e["songAssociations"]})
    write_json(stage / "coverage.json", report)
    write_json(stage / "unsupported.json", catalog["unsupported"])
    write_json(stage / "songs.json", mappings)
    index = ["# 歌曲关联剧情效果索引", "", "只证明此曲关联剧情使用，不代表局内使用；没有歌曲时间证据的条目仍是null。每个剧情分支单独保存，没有默认选分支。", "",
             "| 歌曲ID | 剧情分支 | 源参数数量 | 已有screen类别 |", "|---|---|---|---|"]
    for mapping in mappings:
        if song and mapping["songId"] != song: continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", mapping["songId"]): raise ScreenExportError("歌曲ID不能作为输出目录名")
        branch = [e for e in all_events if any(p["storyU"] == mapping["storyU"] and p["songId"] == mapping["songId"] for p in e["songAssociations"])]
        folder = stage / "by-song" / mapping["songId"]
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / ("story_" + str(mapping["storyU"]) + ".screen.json"), {**edit, "events": branch})
        counts = Counter(e["effect"] for e in branch)
        index.append("| " + mapping["songId"] + " | " + str(mapping["storyU"]) + " | " + str(len(branch)) + " | " +
                     (", ".join(name + "×" + str(n) for name, n in sorted(counts.items())) or "所映射脚本未发现五类effect命令") + " |")
    (stage / "歌曲效果索引.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    (stage / "未支持效果与曲目.md").write_text(unsupported_report(catalog, all_events), encoding="utf-8")
    _publish(stage, output)
    return report


def unsupported_report(catalog, events):
    out = ["# 未支持效果与曲目", "", "剧情关联只证明同段剧情出现，不能冒称歌曲内使用。专属材质参数为真实标识；中文视觉含义未经实机逐项核对，不按名字推断效果。", "",
           "| 旧screen缺口 | 具体限制 | 对应曲目证据 |", "|---|---|---|"]
    for row in catalog["unsupported"]["legacy"]:
        affected = [e for e in events if e["effect"] == row["effect"] and
                    (row.get("eventFilter") != "outOfRange" or not 0 <= e["params"][0] <= 1)]
        songs = sorted({p["songId"] for e in affected for p in e["songAssociations"]})
        out.append("| " + row["effect"] + ("（" + row["label"] + "）" if row.get("label") else "") + " | " + row["reason"] + " | " + (", ".join(songs) or "当前来源未确认使用曲目") + " |")
    out.extend(["", "## 专属演出：旧screen不能表达", "", "| 曲目/归属 | 资源与控制器 | 已证实使用的材质参数 |", "|---|---|---|"])
    for row in catalog["unsupported"]["specialMaterials"]:
        out.append("| " + (row["songId"] or "映射未确认（" + row["asset"] + "）") + " | " + row["asset"] + " / " + row["controller"] + " | " + ", ".join(row["properties"]) + " |")
    out.extend(["", "## 视频与条件", "", "下表为VideoFiles资源和已核对入口的文件名匹配；不等同已确认局内播放时刻。旧screen没有视频播放能力。", "",
                "| 曲目 | 原版视频资源 |", "|---|---|"])
    for row in catalog["unsupported"]["songVideos"]:
        out.append("| " + row["songId"] + " | " + row["file"] + " |")
    out.extend(["", "固定旧screen语句不能保留原帧噪声、动态条件、判断回调、对象/镜头轨迹或材质反馈历史。未知的具体条件与时刻保留未知，不改写成按时间猜测的事件。", "",
                "所有原歌曲时间尚未证明。JSON的timeMs默认null，模板全部为注释；填入自制谱作者指定时间后，才能编译出可执行语句。", ""])
    return "\n".join(out)


def compile_screen(source: Path, output: Path):
    edit = read_json(source)
    if edit.get("schemaVersion") != SCHEMA or edit.get("kind") != "legacyScreenEdit":
        raise ScreenExportError("不是可编辑旧screen清单")
    output = output.resolve()
    _outside(output, Path(edit["readOnlyGameRoot"]).resolve())
    if output.exists() or output == source.resolve(): raise FileExistsError("不覆盖已有文件：" + str(output))
    selected, ids = [], set()
    for order, event in enumerate(edit["events"]):
        if not isinstance(event.get("id"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", event["id"]):
            raise ScreenExportError("事件ID必须为单行标识符")
        if event["id"] in ids: raise ScreenExportError("事件ID重复")
        ids.add(event["id"])
        if type(event.get("enabled")) is not bool: raise ScreenExportError("enabled须为bool")
        if not event["enabled"]: continue
        authored = finite(event.get("timeMs"), "启用条目的timeMs", 0, 2147483647)
        if event.get("timeBasis") != "author":
            raise ScreenExportError("本批只有作者指定时间；请设timeBasis=author，不能伪称原歌曲精确时间")
        selected.append((authored, order, event))
    if not selected: raise ScreenExportError("没有启用且具有明确作者时间的条目；未生成空语句文件")
    selected.sort(key=lambda item: (item[0], item[1]))
    lines, warnings = ["// 自制谱作者指定时间；来源歌曲时刻未确认。"], []
    for authored, _, event in selected:
        line = statement(event, format(authored, ".17g"))
        lines.extend(["// " + event["id"], line])
        warnings.extend(event["id"] + ": " + note for note in runtime_notes(event["effect"], event["params"]))
    from spc_v0_chart_decoder import read_spc_v0_chart
    with tempfile.TemporaryDirectory(prefix="ifce-screen-syntax-") as folder:
        probe = Path(folder) / "syntax.spc"
        probe.write_text("chart(120,4)\n" + "\n".join(lines) + "\n", encoding="utf-8")
        read_spc_v0_chart(str(probe), warn=lambda _: None)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name("." + output.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if output.exists(): raise FileExistsError(str(output))
    os.rename(temporary, output)
    return {"statements": len(selected), "timingBasis": "author", "warnings": warnings, "sha256": digest(output)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="现有screen语句离线导出；不改Mod，不推测原歌曲时刻")
    modes = parser.add_subparsers(dest="mode", required=True)
    export = modes.add_parser("export", help="提取源参数、时间模板与未支持曲目清单")
    export.add_argument("--game-root", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--catalog", type=Path, default=DATA)
    export.add_argument("--song", help="精确原歌曲ID；省略导出全部源脚本")
    compile_mode = modes.add_parser("compile", help="把作者填写时间并启用的条目转成已有screen语句")
    compile_mode.add_argument("--input", type=Path, required=True)
    compile_mode.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.mode == "export":
            result = export_screen(args.game_root, args.output, catalog_path=args.catalog, song=args.song)
            print_log('[OK] Source effect commands=' + str(result['sourceCommandCount']) + ', confirmed original timestamps=0; templates/JSON/unsupported list: ' + str(args.output))
        else:
            result = compile_screen(args.input, args.output)
            print_log('[OK] Authored screen statements=' + str(result['statements']) + ': ' + str(args.output))
            for note in sorted(set(result["warnings"])): print_log('[INFO] ' + note)
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        print_log('[ERROR] ' + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
