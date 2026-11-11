from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GAME_APP_ID = "3971950"
DEFAULT_PACK_FOLDER = "User Charts"
DEFAULT_PACK_ID = "user-charts"
DEFAULT_PACK_TITLE = "User Charts"
DEFAULT_PACK_STYLE = "act-4"

_VDF_PATH = re.compile(r'"path"\s+"([^"]+)"')
_VDF_INSTALL_DIR = re.compile(r'"installdir"\s+"([^"]+)"')


@dataclass(frozen=True)
class GameInstall:
    app_id: str
    install_dir: Path
    charts_root: Path

    @property
    def label(self) -> str:
        return f"In Falsus - {self.install_dir}"


@dataclass(frozen=True)
class PackDirectory:
    folder: Path
    pack_id: str
    title: str
    artist: str
    description: str
    style_pack_slug: str
    sort_order: int

    @property
    def label(self) -> str:
        return f"{self.title} [{self.folder.name}]"


@dataclass(frozen=True)
class InvalidPackDirectory:
    folder: Path
    reason: str


@dataclass(frozen=True)
class PackScan:
    packs: tuple[PackDirectory, ...]
    invalid: tuple[InvalidPackDirectory, ...]


def _unescape_vdf(value: str) -> str:
    return value.replace("\\\\", "\\").replace('\\"', '"')


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        try:
            import winreg

            locations = (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            )
            for hive, key_name, value_name in locations:
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    roots.append(Path(value.strip()))
        except ImportError:
            pass
    for value in (
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
    ):
        if value:
            roots.append(Path(value) / "Steam")

    result: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in seen and resolved.is_dir():
            seen.add(resolved)
            result.append(resolved)
    return result


def _library_roots(steam_root: Path) -> list[Path]:
    roots = [steam_root]
    library_file = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        text = library_file.read_text(encoding="utf-8-sig")
    except OSError:
        text = ""
    for raw in _VDF_PATH.findall(text):
        roots.append(Path(_unescape_vdf(raw)))

    result: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in seen and resolved.is_dir():
            seen.add(resolved)
            result.append(resolved)
    return result


def _install_from_library(library_root: Path) -> GameInstall | None:
    steamapps = library_root / "steamapps"
    manifest = steamapps / f"appmanifest_{GAME_APP_ID}.acf"
    try:
        text = manifest.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    match = _VDF_INSTALL_DIR.search(text)
    if match is None:
        return None
    install_dir = steamapps / "common" / _unescape_vdf(match.group(1))
    if not (install_dir / "infalsus.exe").is_file():
        return None
    if not (install_dir / "GameAssembly.dll").is_file():
        return None
    try:
        install_dir = install_dir.resolve()
    except OSError:
        return None
    return GameInstall(
        GAME_APP_ID,
        install_dir,
        install_dir / "CustomCharts",
    )


def discover_game_installs() -> tuple[GameInstall, ...]:
    """Discover formal In Falsus installs from Steam library manifests."""
    result: list[GameInstall] = []
    seen: set[Path] = set()
    for steam_root in _steam_roots():
        for library_root in _library_roots(steam_root):
            install = _install_from_library(library_root)
            if install is None or install.install_dir in seen:
                continue
            seen.add(install.install_dir)
            result.append(install)
    result.sort(key=lambda item: str(item.install_dir).casefold())
    return tuple(result)


def _settings_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "InFalsusChartExt" / "SPCConverter" / "settings.json"


def load_preferred_game_root(explicit: Path | None = None) -> Path | None:
    path = _settings_path(explicit)
    try:
        root = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None
    value = root.get("preferredGameRoot") if isinstance(root, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Path(value).resolve()
    except OSError:
        return Path(value)


def save_preferred_game_root(
    game_root: Path,
    explicit: Path | None = None,
) -> None:
    path = _settings_path(explicit)
    path.parent.mkdir(parents=True, exist_ok=True)
    root: dict[str, Any] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(existing, dict):
            root.update(existing)
    except (OSError, ValueError, TypeError):
        pass
    root["preferredGameRoot"] = str(game_root.resolve())
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".settings-",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(root, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def read_pack_directory(folder: Path) -> PackDirectory:
    folder = folder.resolve()
    path = folder / "pack_config.json"
    try:
        root = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"缺少 pack_config.json：{folder}") from exc
    except Exception as exc:
        raise ValueError(f"pack_config.json 无法解析：{path}：{exc}") from exc
    if not isinstance(root, dict):
        raise ValueError(f"pack_config.json 根节点必须是对象：{path}")
    if root.get("schemaVersion") != 1:
        raise ValueError(f"pack schemaVersion 必须为 1：{path}")
    pack_id = root.get("id")
    title = root.get("title")
    if not isinstance(pack_id, str) or not pack_id.strip():
        raise ValueError(f"pack id 不能为空：{path}")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"pack title 不能为空：{path}")
    style = root.get("stylePackSlug")
    if not isinstance(style, str) or not style.strip():
        style = DEFAULT_PACK_STYLE
    try:
        sort_order = int(root.get("sortOrder", 0))
    except (TypeError, ValueError):
        sort_order = 0
    return PackDirectory(
        folder=folder,
        pack_id=pack_id.strip(),
        title=title.strip(),
        artist=str(root.get("artist") or ""),
        description=str(root.get("description") or ""),
        style_pack_slug=style.strip(),
        sort_order=sort_order,
    )


def scan_pack_directories(root: Path) -> PackScan:
    root = root.resolve()
    invalid: list[InvalidPackDirectory] = []
    parsed: list[PackDirectory] = []
    if root.is_dir():
        for folder in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if not folder.is_dir():
                continue
            try:
                parsed.append(read_pack_directory(folder))
            except ValueError as exc:
                invalid.append(InvalidPackDirectory(folder, str(exc)))

    by_id: dict[str, list[PackDirectory]] = {}
    for pack in parsed:
        by_id.setdefault(pack.pack_id.casefold(), []).append(pack)
    valid: list[PackDirectory] = []
    for group in by_id.values():
        if len(group) == 1:
            valid.append(group[0])
            continue
        for pack in group:
            invalid.append(
                InvalidPackDirectory(
                    pack.folder,
                    f"曲包 id 重复：{pack.pack_id}",
                )
            )
    valid.sort(key=lambda item: (item.sort_order, item.title.casefold(), item.folder.name.casefold()))
    invalid.sort(key=lambda item: str(item.folder).casefold())
    return PackScan(tuple(valid), tuple(invalid))
