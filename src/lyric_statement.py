from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STATEMENT_NAME = "text"
SIDECAR_SUFFIX = ".lyrics.json"
DEFAULT_LAST_HOLD_MS = 6000.0
TEXT_SHOWTEXT_ID_BASE = 1_000_000


class LyricValidationError(ValueError):
    pass


@dataclass
class LyricStatement:
    time_ms: float
    text: str
    options: dict[str, str] = field(default_factory=dict)
    order: int = 0
    end_ms: float | None = None

    def option(self, key: str) -> str | None:
        return self.options.get(key)

    def option_number(self, key: str, fallback: float) -> float:
        raw = self.option(key)
        if raw is None or not raw.strip():
            return fallback
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return fallback
        return value if math.isfinite(value) else fallback


def strip_comment(raw: str) -> str:
    """Remove // comments without touching quoted lyric text."""
    in_quote = False
    quote = '"'
    index = 0
    while index < len(raw):
        char = raw[index]
        if in_quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                in_quote = False
            index += 1
            continue
        if char in ('"', "'"):
            in_quote = True
            quote = char
            index += 1
            continue
        if char == "/" and index + 1 < len(raw) and raw[index + 1] == "/":
            return raw[:index]
        index += 1
    return raw


def split_arguments(body: str) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    in_quote = False
    quote = '"'
    index = 0
    while index < len(body):
        char = body[index]
        if in_quote:
            if char == "\\" and index + 1 < len(body):
                current.append(body[index + 1])
                index += 2
                continue
            if char == quote:
                in_quote = False
                current.append(char)
                index += 1
                continue
            current.append(char)
            index += 1
            continue
        if char in ('"', "'"):
            in_quote = True
            quote = char
            current.append(char)
            index += 1
            continue
        if char in (",", "\uFF0C"):
            result.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    result.append("".join(current).strip())
    while result and not result[-1]:
        result.pop()
    return result


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def parse_time(raw: str) -> float | None:
    value = unquote(raw).strip()
    if not value:
        return None
    if value.lower().endswith("ms"):
        value = value[:-2].strip()
    if ":" in value:
        parts = value.split(":")
        if len(parts) not in (2, 3):
            return None
        try:
            minutes = float(parts[0])
            seconds = float(parts[1])
            millis = float(parts[2]) if len(parts) == 3 else 0.0
        except ValueError:
            return None
        return minutes * 60000.0 + seconds * 1000.0 + millis
    try:
        return float(value)
    except ValueError:
        return None


def _split_option(piece: str) -> tuple[str, str] | None:
    if not piece:
        return None
    equals = piece.find("=")
    if equals <= 0:
        return None
    key = piece[:equals]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
        return None
    return key, unquote(piece[equals + 1:])


def _is_number(value: str) -> bool:
    try:
        return math.isfinite(float(value.strip()))
    except (TypeError, ValueError):
        return False


def parse_statement(raw: str, order: int = 0) -> LyricStatement | None:
    line = strip_comment(raw).strip()
    if not line:
        return None
    open_index = -1
    for index, char in enumerate(line):
        if char in ("(", "\uFF08"):
            open_index = index
            break
    if open_index <= 0:
        return None
    name = line[:open_index].strip()
    if name.lower() != STATEMENT_NAME:
        return None
    close_index = -1
    for index in range(len(line) - 1, open_index, -1):
        if line[index] in (")", "\uFF09"):
            close_index = index
            break
    if close_index < open_index:
        return None
    body = line[open_index + 1:close_index]
    args = split_arguments(body)
    if len(args) < 2:
        return None
    time_ms = parse_time(args[0])
    if time_ms is None or not math.isfinite(time_ms):
        return None
    text = unquote(args[1])
    options: dict[str, str] = {}
    extra: list[str] = []
    pos_open = False
    for piece in args[2:]:
        option = _split_option(piece)
        if option is not None:
            key, value = option
            options[key.lower()] = value
            pos_open = key.lower() == "pos"
            continue
        if pos_open and _is_number(piece):
            options["pos"] = options.get("pos", "") + "," + piece.strip()
            continue
        pos_open = False
        extra.append(piece)
    if extra:
        separator = "\uFF0C" if "\uFF0C" in body else ","
        text = separator.join([text, *extra])
    if not text:
        return None
    return LyricStatement(
        time_ms=time_ms,
        text=text,
        options=options,
        order=order,
    )


def extract_from_content(content: str) -> list[LyricStatement]:
    result: list[LyricStatement] = []
    for raw in content.splitlines():
        statement = parse_statement(raw, len(result))
        if statement is not None:
            result.append(statement)
    result.sort(key=lambda item: (item.time_ms, item.order))
    hold_ms = DEFAULT_LAST_HOLD_MS
    for index, statement in enumerate(result):
        end_ms = statement.time_ms + hold_ms
        for following in result[index + 1:]:
            if following.time_ms > statement.time_ms:
                end_ms = following.time_ms
                break
        explicit_end = statement.option_number("end", -1.0)
        if explicit_end > statement.time_ms:
            end_ms = explicit_end
        statement.end_ms = end_ms
    return result


def read_text(path: Path) -> str:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LyricValidationError(f"无法解码歌词源文件：{path}")


def extract_from_file(path: Path) -> list[LyricStatement]:
    return extract_from_content(read_text(path))


def sidecar_candidates(chart_path: Path) -> tuple[Path, Path]:
    return (
        chart_path.with_suffix(SIDECAR_SUFFIX),
        chart_path.with_name(chart_path.name + SIDECAR_SUFFIX),
    )


def build_payload(statements: list[LyricStatement]) -> dict[str, Any]:
    return {
        "version": 1,
        "holdMs": DEFAULT_LAST_HOLD_MS,
        "lyrics": [
            {
                "time": item.time_ms,
                "end": item.end_ms,
                "text": item.text,
                "order": item.order,
                "options": item.options,
            }
            for item in statements
        ],
    }


def build_sidecar_bytes(source: Path) -> bytes | None:
    """Build a sidecar from a v0 text chart; return None when it has no text()."""
    statements = extract_from_file(source)
    if not statements:
        return None
    return json.dumps(
        build_payload(statements),
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def build_showtext_styles(source: Path) -> list[dict[str, Any]]:
    """Translate text(...) into showtext sidecar entries.

    The game only sees showtext markers and a .text.json sidecar; the
    Studio+ text statement is a converter-side authoring form.
    """
    statements = extract_from_file(source)
    styles: list[dict[str, Any]] = []
    for index, statement in enumerate(statements):
        style: dict[str, Any] = {
            "id": TEXT_SHOWTEXT_ID_BASE + index,
            "text": statement.text,
            "duration": max(
                0.0,
                float(statement.end_ms or statement.time_ms) -
                float(statement.time_ms),
            ),
            "fadeIn": max(0.0, statement.option_number("entry", 0.0)),
            "fadeOut": max(0.0, statement.option_number("fade", 180.0)),
            "size": max(1, int(round(statement.option_number("size", 40.0)))),
            # FastText font units are much larger than 1080p pixels.
            # Keep converted lyrics close to showtext's default visual size.
            "scale": 0.3,
            "color": statement.option("fill") or "#FFFFFFE6",
            "fill2": (
                statement.option("fill2")
                or statement.option("fill")
                or "#FFFFFFE6"
            ),
            "spacing": statement.option_number("spacing", 2.0),
            "amp": statement.option_number("amp", 1.5),
            "hz": statement.option_number("hz", 0.5),
            "rot": statement.option_number("rot", 0.8),
            "stagger": statement.option_number("stagger", 90.0),
            "pop": max(1.0, statement.option_number("pop", 1.28)),
            "shadowpx": max(0.0, statement.option_number("shadowpx", 3.0)),
        }
        shadow = statement.option("shadow") or "#000000A6"
        style["shadow"] = style["shadowpx"] > 0.0
        style["shadowColor"] = shadow
        style["shadowOffsetY"] = -style["shadowpx"]
        side = (statement.option("side") or "left").strip().lower()
        style["side"] = side
        x = 0.16 if side == "left" else 0.84 if side == "right" else 0.5
        y = 0.5
        pos = statement.option("pos")
        if pos:
            parts = [part.strip() for part in pos.split(",")]
            if len(parts) >= 2:
                try:
                    px = float(parts[0])
                    py = float(parts[1])
                    pw = float(parts[2]) if len(parts) > 2 else 0.0
                    ph = float(parts[3]) if len(parts) > 3 else 0.0
                    x = (px + pw * 0.5) / 1920.0
                    y = (py + ph * 0.5) / 1080.0
                    style["pos"] = pos
                except ValueError:
                    pass
        x_override = statement.option("x")
        if x_override is not None:
            try:
                x = float(x_override) / 1920.0
            except ValueError:
                pass
        y_override = statement.option("y")
        if y_override is not None:
            try:
                value = float(y_override)
                y = value / 1080.0 if value > 1.5 else value
            except ValueError:
                pass
        style["x"] = x
        style["y"] = y
        styles.append(style)
    return styles


def find_sidecar(chart_path: Path) -> Path | None:
    for candidate in sidecar_candidates(chart_path):
        if candidate.is_file():
            return candidate
    return None


def validate_sidecar(chart_path: Path) -> None:
    found = find_sidecar(chart_path)
    if found is None:
        return
    try:
        root = json.loads(found.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise LyricValidationError(
            f"歌词配置不是有效 JSON：{found} ({exc})"
        ) from exc
    if not isinstance(root, dict):
        raise LyricValidationError(f"歌词配置根节点必须是对象：{found}")
    lyrics = root.get("lyrics")
    if not isinstance(lyrics, list):
        raise LyricValidationError(f"歌词配置缺少 lyrics 数组：{found}")
    for index, item in enumerate(lyrics):
        if not isinstance(item, dict):
            raise LyricValidationError(
                f"歌词配置 lyrics[{index}] 必须是对象：{found}"
            )
        for key in ("time", "end"):
            value = item.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise LyricValidationError(
                    f"歌词配置 lyrics[{index}].{key} 必须是有限数值：{found}"
                )
        if float(item["end"]) < float(item["time"]):
            raise LyricValidationError(
                f"歌词配置 lyrics[{index}].end 早于 time：{found}"
            )
        if not isinstance(item.get("text"), str):
            raise LyricValidationError(
                f"歌词配置 lyrics[{index}].text 必须是字符串：{found}"
            )
        options = item.get("options", {})
        if not isinstance(options, dict):
            raise LyricValidationError(
                f"歌词配置 lyrics[{index}].options 必须是对象：{found}"
            )
        for key, value in options.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise LyricValidationError(
                    f"歌词配置 lyrics[{index}].options 必须是字符串键值：{found}"
                )
