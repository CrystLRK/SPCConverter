from __future__ import annotations

import copy
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROFILE_FILENAME = "config_profiles.json"
CURRENT_CONFIG_FILE = "config.json"
PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_DROP = object()
_last_error: str | None = None


def _current_template() -> dict[str, Any]:
    return {
        "schemaVersion": "${schema_version}",
        "id": "${song_id}",
        "title": "${song_title}",
        "artist": "${artist}",
        "chartDesigner": "${chart_designer}",
        "jacketDesigner": "${jacket_designer}",
        "audio": "${audio_file}",
        "previewStart": "${preview_start}",
        "previewEnd": "${preview_end}",
        "preview": "${preview}",
        "background": "${background}",
        "jacketLarge": "${jacket_large_file}",
        "jacketSmall": "${jacket_small_file}",
        "difficulties": "${difficulties}",
    }


def _if_template() -> dict[str, Any]:
    return {
        "id": "${song_id}",
        "name": "${song_title}",
        "composer": "${artist}",
        "song": "${audio_file}",
        "illust": "${jacket_designer}",
        "jacket": "${jacket_small_file}",
        "background": [
            "${background_file_0}",
            "${background_file_1}",
            "${background_file_2}",
            "${background_file_3}",
        ],
        "preview_seconds": ["${preview_start}", "${preview_end}"],
        "charter": [
            "${chart_designer_0}",
            "${chart_designer_1}",
            "${chart_designer_2}",
            "${chart_designer_3}",
        ],
        "chart": [
            "${chart_file_0}",
            "${chart_file_1}",
            "${chart_file_2}",
            "${chart_file_3}",
        ],
        "lv": ["${rating_0}", "${rating_1}", "${rating_2}", "${rating_3}"],
    }


def builtin_profiles() -> dict[str, "ProfileDefinition"]:
    current = _current_template()
    return {
        "current_loader": ProfileDefinition(
            "current_loader",
            "当前 In Falsus Chart Ext（schemaVersion 1）",
            CURRENT_CONFIG_FILE,
            current,
        ),
        "legacy_collection": ProfileDefinition(
            "legacy_collection",
            "旧版单 collection / 单 song 配置",
            "config.json",
            {
                "enabled": True,
                "collections": [{
                    "packId": 100,
                    "slug": "custom-charts",
                    "title": "Custom Charts",
                    "stylePackSlug": "act-4",
                    "songs": [copy.deepcopy(current)],
                }],
            },
        ),
        "in_falsus_chart_loader": ProfileDefinition(
            "in_falsus_chart_loader",
            "外部兼容：InFalsusChartLoader（if）",
            "if",
            _if_template(),
        ),
        "simple_example": ProfileDefinition(
            "simple_example",
            "最小示例：song + 四档 chart",
            "config.json",
            {
                "song": "${audio_file}",
                "chart": [
                    "${chart_file_0}",
                    "${chart_file_1}",
                    "${chart_file_2}",
                    "${chart_file_3}",
                ],
            },
        ),
    }


@dataclass
class ProfileDefinition:
    key: str
    description: str
    config_file: str
    template: dict[str, Any]


def profile_label_map(
    profiles: dict[str, ProfileDefinition],
) -> dict[str, str]:
    """Return unique GUI/package labels without losing profile keys."""
    result: dict[str, str] = {}
    used: set[str] = set()
    for key, profile in profiles.items():
        base = profile.description.strip() or key
        label = base
        if label in used:
            label = f"{base} [{key}]"
        suffix = 2
        while label in used:
            label = f"{base} [{key}#{suffix}]"
            suffix += 1
        used.add(label)
        result[label] = key
    return result


def _program_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def find_profile_file(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit.resolve() if explicit.is_file() else None
    candidates = [
        _program_dir() / PROFILE_FILENAME,
        _program_dir().parent / PROFILE_FILENAME,
        Path.cwd() / PROFILE_FILENAME,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def load_profiles(explicit: Path | None = None) -> dict[str, ProfileDefinition]:
    """Load named profile templates from config_profiles.json.

    If the file is absent or invalid, built-in profiles remain available.
    A malformed file is reported by the GUI by falling back to the built-ins.
    """
    global _last_error
    _last_error = None
    profiles = builtin_profiles()
    path = find_profile_file(explicit)
    if path is None:
        return profiles
    try:
        root = json.loads(path.read_text(encoding="utf-8-sig"))
        raw_profiles = root.get("profiles") if isinstance(root, dict) else None
        if not isinstance(raw_profiles, dict):
            raw_profiles = root if isinstance(root, dict) else {}
        for name, value in raw_profiles.items():
            if not isinstance(value, dict):
                continue
            template = value.get("template")
            if not isinstance(template, dict):
                # Also accept the compact form:
                # "配置文件1": { "song": "${audio_file}", "chart": [...] }
                # where the profile value itself is the target config template.
                template = {
                    k: v for k, v in value.items()
                    if k not in ("description", "configFile", "config_file", "file")
                }
                if not template:
                    continue
            file_name = value.get("configFile") or value.get("config_file") or value.get("file") or "config.json"
            if not isinstance(file_name, str) or not file_name.strip():
                file_name = "config.json"
            profiles[str(name)] = ProfileDefinition(
                str(name),
                str(value.get("description") or ""),
                file_name.strip(),
                template,
            )
    except Exception as exc:
        _last_error = f"{path}: {exc}"
        return profiles
    return profiles


def last_profile_error() -> str | None:
    """Return the error from the most recent load_profiles call, if any."""
    return _last_error


def _render_string(value: str, context: dict[str, Any]) -> Any:
    match = PLACEHOLDER.fullmatch(value.strip())
    if match:
        name = match.group(1)
        if name not in context:
            raise ValueError(f"未知配置模板占位符：${{{name}}}")
        return context[name]

    def replace(item: re.Match[str]) -> str:
        name = item.group(1)
        if name not in context:
            raise ValueError(f"未知配置模板占位符：${{{name}}}")
        raw = context[name]
        return "" if raw is None else str(raw)

    return PLACEHOLDER.sub(replace, value)


def render_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_string(value, context)
    if isinstance(value, list):
        rendered: list[Any] = []
        for item in value:
            item_value = render_value(item, context)
            if item_value is not _DROP:
                rendered.append(item_value)
        return rendered
    if isinstance(value, dict):
        if "$when" in value:
            condition = render_value(value["$when"], context)
            if condition is _DROP or not condition:
                return _DROP
            value = {k: v for k, v in value.items() if k != "$when"}
        rendered: dict[str, Any] = {}
        for key, item in value.items():
            item_value = render_value(item, context)
            if item_value is not _DROP:
                rendered[key] = item_value
        return rendered
    return copy.deepcopy(value)


def render_template(template: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    rendered = render_value(template, context)
    if not isinstance(rendered, dict):
        raise ValueError("配置模板根节点必须是对象")
    return rendered
