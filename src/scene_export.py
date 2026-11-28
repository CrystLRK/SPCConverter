"""Offline export of audited, version-bound source data into editable actions.

The game installation is read only.  No script is executed, no native controller
is started, and an unresolved source is never assigned an invented timestamp.
"""
from __future__ import annotations

from ascii_log import print_log

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from scene_program import (SceneError, TABLES, compile_program, fingerprint,
                           read_json, serialize_action, validate_action,
                           validate_source, source_from_program)


STATUS = {"exported", "unresolvedTime", "unsupported", "resourceFailed", "excludedGameplay"}
SAFE_BRANCH = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
SHA = re.compile(r"^[0-9a-fA-F]{64}$")
DATA_ROOT = Path(__file__).resolve().parents[1] / "data/scene"


def publish_directory(stage: Path, output: Path) -> None:
    """Only retry Windows sharing/access failures, with a fixed finite budget.

    An independent real probe confirmed directory rename is supported here;
    indexers can briefly hold a new directory. Never rerun extraction or
    overwrite a destination while waiting for the existing staging set.
    """
    for attempt, delay in enumerate((0.0, 0.1, 0.25, 0.5, 1.0)):
        if delay:
            time.sleep(delay)
        if output.exists():
            raise FileExistsError("scene 发布目标已存在：" + str(output))
        try:
            stage.rename(output)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 4 or not stage.is_dir():
                raise


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")


def validate_installation(game_root: Path, catalog: dict, resources: list[dict]) -> dict:
    """Hash the current DLL, Addressables catalog and exact referenced bundles."""
    game_root = game_root.resolve(strict=True)
    actual = file_hash(game_root / "GameAssembly.dll")
    if actual != str(catalog.get("gameAssemblySha256", "")).upper():
        raise SceneError("scene 当前游戏DLL与离线数据目录不匹配")
    aa = game_root / "infalsus_Data/StreamingAssets/aa"
    catalog_hash = file_hash(aa / "catalog.bin")
    if catalog_hash != str(catalog.get("addressablesCatalogSha256", "")).upper():
        raise SceneError("scene 当前Addressables目录身份不匹配")
    expected: dict[str, str] = {}
    for resource in resources:
        identity = resource.get("identity", resource.get("expectedIdentity", {}))
        identities = [identity, *identity.get("dependencyBundles", [])]
        for item in identities:
            name, sha = item.get("bundle"), item.get("bundleSha256")
            if not isinstance(name, str) or not re.fullmatch(r"[0-9a-fA-F]{32}\.bundle", name) or not SHA.fullmatch(str(sha)):
                raise SceneError("scene 资源缺少可核对的原包身份")
            if name in expected and expected[name] != sha.upper():
                raise SceneError("scene 同一原包出现竞争身份 " + name)
            expected[name] = sha.upper()
    verified = []
    for name, sha in sorted(expected.items()):
        if file_hash(aa / "StandaloneWindows64" / name) != sha:
            raise SceneError("scene 原版素材包不匹配 " + name)
        verified.append({"bundle": name, "sha256": sha})
    return {"gameAssemblySha256": actual, "addressablesCatalogSha256": catalog_hash,
            "verifiedBundles": verified}


def normalize_branch(branch: dict, catalog: dict) -> tuple[dict, list[dict], list[dict]]:
    branch_id = branch.get("id")
    if not isinstance(branch_id, str) or not SAFE_BRANCH.fullmatch(branch_id) or branch_id in (".", ".."):
        raise SceneError("scene 非法导出分支ID")
    source = validate_source(branch.get("definitions"))
    actions = branch.get("actions")
    if not isinstance(actions, list) or len(actions) > 1_000_000:
        raise SceneError("scene 导出动作列表缺失/超限")
    result = []
    ids, orders = set(), set()
    for action in actions:
        normalized = validate_action(action, source)
        if normalized["id"] in ids or normalized["sourceOrder"] in orders:
            raise SceneError("scene 导出动作ID/顺序重复")
        ids.add(normalized["id"])
        orders.add(normalized["sourceOrder"])
        result.append(normalized)
    coverage = branch.get("coverage")
    if not isinstance(coverage, list) or len(coverage) > 1_000_000:
        raise SceneError("scene 缺少有界来源覆盖清单")
    claimed, source_ids = set(), set()
    for item in coverage:
        if not isinstance(item, dict) or item.get("status") not in STATUS:
            raise SceneError("scene 未知覆盖状态")
        source_id = item.get("sourceId")
        if not isinstance(source_id, str) or not source_id or source_id in source_ids:
            raise SceneError("scene 来源ID缺失/重复")
        source_ids.add(source_id)
        if not item.get("source") or not item.get("reason"):
            raise SceneError("scene 覆盖项须保留来源和结论理由")
        if item["status"] == "exported":
            action_ids = item.get("actionIds")
            if not isinstance(action_ids, list) or not action_ids or not set(action_ids) <= ids:
                raise SceneError("scene 已导出来源未指向真实动作")
            claimed.update(action_ids)
        elif item.get("actionIds"):
            raise SceneError("scene 未定/排除来源不得包含可执行动作")
        if item["status"] == "unresolvedTime" and item.get("timeMs", "missing") is not None:
            raise SceneError("scene 未定时间必须显式null")
    if claimed != ids:
        raise SceneError("scene 存在没有来源证据的导出动作")
    # Reuse actual compiler identity checks without publishing a binary chart.
    program = compile_program({**source, "actions": result}, b"", "effects.spc", catalog)
    normalized_source = source_from_program(program)
    if "source" in source:
        normalized_source["source"] = source["source"]
    return normalized_source, sorted(result, key=lambda a: (a["timeMs"], a["sourceOrder"])), copy.deepcopy(coverage)


def export_scenes(game_root: Path, output: Path, *, song: str | None = None,
                  catalog_path: Path | None = None, performances_path: Path | None = None) -> dict:
    catalog = read_json(catalog_path or DATA_ROOT / "infalsus-107-scene-1.json")
    data = read_json(performances_path or DATA_ROOT / "infalsus-107-performances-1.json")
    if data.get("schemaVersion") != 1 or data.get("target") != {
            "gameAssemblySha256": catalog.get("gameAssemblySha256"), "catalogVersion": catalog.get("catalogVersion")}:
        raise SceneError("scene 演出目录与资源目录的版本身份不匹配")
    branches = data.get("branches")
    if not isinstance(branches, list) or not branches or len(branches) > 65536:
        raise SceneError("scene 演出目录缺分支或超过上限")
    branch_ids = [b.get("id") for b in branches]
    if len(branch_ids) != len(set(branch_ids)):
        raise SceneError("scene 演出分支ID重复")
    selected = [b for b in branches if song is None or b.get("songId") == song]
    if not selected:
        raise SceneError("scene 演出目录中没有所选歌曲 " + str(song))
    normalized = [(branch, *normalize_branch(branch, catalog)) for branch in selected]
    needed = []
    for _, source, _, _ in normalized:
        needed.extend(source["resources"].values())
    installation = validate_installation(game_root, catalog, needed)
    output = output.absolute()
    game_root = game_root.resolve()
    if output.resolve().is_relative_to(game_root):
        raise SceneError("scene 导出目录不能位于游戏安装目录中")
    if output.exists():
        raise FileExistsError("scene 输出目录已存在，须使用新目录：" + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.with_name(output.name + ".stage-" + uuid.uuid4().hex)
    stage.mkdir()
    report = {"schemaVersion": 1, "status": "partial", "target": data["target"],
              "dataSha256": fingerprint(data), "installation": installation, "branches": []}
    try:
        for branch, source, actions, coverage in normalized:
            destination = stage / branch["id"]
            destination.mkdir()
            text = "// 原曲可编辑动作片段。请结合coverage.json和unresolved.json确认覆盖范围。\n"
            text += "\n".join(serialize_action(action) for action in actions) + "\n"
            (destination / "effects.scene.txt").write_text(text, encoding="utf-8")
            _write_json(destination / "effects.scene.json", source)
            counts = {status: sum(c["status"] == status for c in coverage) for status in sorted(STATUS)}
            _write_json(destination / "coverage.json", {"counts": counts, "sources": coverage,
                        "complete": bool(coverage) and all(c["status"] in ("exported", "excludedGameplay") for c in coverage)})
            _write_json(destination / "unresolved.json", [c for c in coverage if c["status"] not in ("exported", "excludedGameplay")])
            _write_json(destination / "provenance.json", {"target": data["target"], "branch": branch["id"],
                        "songId": branch["songId"], "origin": branch.get("origin", {}),
                        "sources": data.get("sources", []), "dataSha256": report["dataSha256"]})
            files = {p.name: file_hash(p) for p in destination.iterdir()}
            report["branches"].append({"id": branch["id"], "songId": branch["songId"],
                                       "actions": len(actions), "counts": counts, "files": files})
        report["status"] = "complete" if all(all(c["status"] in ("exported", "excludedGameplay") for c in records)
            and records for _, _, _, records in normalized) else "partial"
        _write_json(stage / "export.json", report)
        # Publish only a complete set of artifacts. No overwrite or data deletion.
        publish_directory(stage, output)
    except Exception as exc:
        _write_json(stage / "export.failed.json", {"error": str(exc), "published": False})
        raise
    return report


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出经过版本核对的可编辑scenecontrol语句；未知来源单列")
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--song", help="精确原歌曲ID；不指定则导出所有已登记分支")
    parser.add_argument("--scene-catalog", type=Path)
    parser.add_argument("--scene-performances", type=Path)
    args = parser.parse_args(argv)
    try:
        report = export_scenes(args.game_root, args.output, song=args.song,
                               catalog_path=args.scene_catalog, performances_path=args.scene_performances)
        print_log(f"Scene export: {len(report['branches'])} branch(es), status={report['status']}: {args.output}")
        if report["status"] != "complete":
            print_log('Unresolved or unsupported sources remain; see coverage/unresolved for each branch. This is not a complete original performance export.')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print_log('Scene export failed: ' + str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
