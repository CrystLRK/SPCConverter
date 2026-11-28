"""ASCII-only diagnostic output; never transform files, chart data or paths."""
from __future__ import annotations

import sys
from typing import TextIO


def ascii_text(value: object) -> str:
    text = str(value)
    return text if text.isascii() else text.encode("ascii", "backslashreplace").decode("ascii")


def print_log(*values: object, sep: str = " ", end: str = "\n",
              file: TextIO | None = None, flush: bool = False) -> None:
    print(*(ascii_text(value) for value in values), sep=ascii_text(sep),
          end=ascii_text(end), file=sys.stdout if file is None else file, flush=flush)
