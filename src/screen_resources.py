"""Chart-owned screen image manifests, validated before atomic publication."""
from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
import re


SCHEMA = "ifce.screen-resources.1"
SLOTS = {"memoire.bright": "secondImage", "latentduality.hacker_transition": "secondImage",
         "common.filmgrain": "grainTexture"}
LIMIT = 32 * 1024 * 1024


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("screen资源清单有重复键：" + key)
        result[key] = value
    return result


def read_manifest(path):
    root = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique)
    if not isinstance(root, dict) or set(root) != {"schema", "images", "effects"} or root["schema"] != SCHEMA:
        raise ValueError("screen资源清单结构或版本错误")
    if not isinstance(root["images"], dict) or not isinstance(root["effects"], dict):
        raise ValueError("screen资源清单images/effects必须是对象")
    base = Path(path).resolve().parent
    if len(root["images"]) > 32:
        raise ValueError("screen图片项超过32项")
    encoded = 0
    for name, value in root["images"].items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("screen图片ID须为字母数字下划线或短横线：" + name)
        if not isinstance(value, dict) or set(value) != {"path"} or not isinstance(value["path"], str):
            raise ValueError("screen图片项结构错误：" + name)
        text = value["path"].replace("\\", "/")
        if not text or PureWindowsPath(text).drive or text.startswith("/") or any(
                part in ("", ".", "..") for part in text.split("/")):
            raise ValueError("screen图片路径必须在本曲包内：" + text)
        target = base / text
        if not target.resolve().is_relative_to(base):
            raise ValueError("screen图片路径越界：" + text)
        for part in [target, *target.parents]:
            if part == base:
                break
            if part.is_symlink():
                raise ValueError("screen图片路径不能包含符号链接：" + text)
        if target.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ValueError("screen图片仅支持PNG/JPEG：" + text)
        if target.is_file() and target.stat().st_size > LIMIT:
            raise ValueError("screen图片超出32MiB：" + text)
        if target.is_file():
            encoded += target.stat().st_size
            if encoded > 128 * 1024 * 1024:
                raise ValueError("screen编码图片总量超过128MiB")
    for name, value in root["effects"].items():
        if name not in SLOTS or not isinstance(value, dict) or set(value) != {SLOTS[name]}:
            raise ValueError("screen未知效果或资源槽：" + name)
        reference = value[SLOTS[name]]
        if not isinstance(reference, str) or reference not in root["images"]:
            raise ValueError("screen未知图片引用：" + name)
    return root


def _publication_target(output, relative):
    output = Path(output)
    relative = Path(relative)
    base = output.parent.resolve()
    target = output.parent / relative
    if relative.is_absolute() or ".." in relative.parts or not target.resolve().is_relative_to(base):
        raise ValueError("screen发布目标越界：" + str(relative))
    for part in [target, *target.parents]:
        if part == output.parent:
            break
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("screen发布目标不能包含链接：" + str(relative))
    if target.exists() and not target.is_file():
        raise ValueError("screen发布文件目标不是普通文件：" + str(relative))
    return target


def publication_files(source, output, copy_sidecars=True):
    """Final chart-owned files; None is an explicit transactional withdrawal."""
    output = Path(output)
    manifest = Path(source).with_suffix(".screen.json")
    existing_manifest = output.with_suffix(".screen.json")
    asset_dir = output.stem + ".screen-assets"
    previous = set()
    if existing_manifest.is_file():
        old = read_manifest(existing_manifest)
        for name, value in old["images"].items():
            relative = Path(value["path"].replace("\\", "/"))
            # Old hand-authored/shared pictures are not converter-owned.
            if (relative.parent == Path(asset_dir) and
                    relative.name == name + relative.suffix.lower()):
                _publication_target(output, relative)
                previous.add(relative)
    if not manifest.is_file():
        result = {relative: None for relative in sorted(previous)}
        if existing_manifest.is_file():
            result[Path(existing_manifest.name)] = None
        return list(result.items())
    if not copy_sidecars:
        raise ValueError("screen资源清单不能用no-sidecars省略")
    root = read_manifest(manifest)
    result = {relative: None for relative in sorted(previous)}
    for name, value in root["images"].items():
        original = manifest.parent / value["path"].replace("\\", "/")
        relative = Path(asset_dir) / (name + original.suffix.lower())
        _publication_target(output, relative)
        value["path"] = relative.as_posix()
        # Missing sources actively withdraw stale same-name output pictures;
        # the manifest still names the missing slot for the runtime warning.
        result[relative] = original.read_bytes() if original.is_file() else None
    result[Path(output.with_suffix(".screen.json").name)] = (
        json.dumps(root, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return list(result.items())
