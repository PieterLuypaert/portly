"""What every part of portly shares: running a tool, text that fits the terminal, colour and time.

Imported by name, except run and CACHE: every module calls base.run and reads base.CACHE, so a
test that replaces run or moves CACHE reaches all of portly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unicodedata
from datetime import datetime, timedelta
from typing import List, Optional


HOME = os.path.expanduser("~")


# portly's own files: the output of what it starts, and the repos it saw.
CACHE = os.path.join(HOME, ".cache", "portly")


def run(cmd: List[str]) -> str:
    """Stdout of cmd. lsof exits 1 on any missing pid and git outside a repo,
    so the exit code is no sign of failure here; a missing tool gives "".
    Bytes that are no UTF-8, such as in any process's arguments, become �."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=10).stdout
    except FileNotFoundError:
        return ""


def tilde(path: str) -> str:
    if path == HOME or path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


def short_duration(seconds: int) -> str:
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def etime_seconds(etime: str) -> Optional[int]:
    """ps etime ([[dd-]hh:]mm:ss) in seconds."""
    days, _, clock = etime.strip().rpartition("-")
    parts = clock.split(":")
    if not (days or "0").isdigit() or not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return int(days or 0) * 86400 + seconds


def short_age(etime: str) -> str:
    """ps etime as <1m, 12m, 3h or 6d."""
    seconds = etime_seconds(etime)
    return "" if seconds is None else short_duration(seconds)


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def started_at(seconds_ago: int, now: Optional[datetime] = None) -> str:
    """When something started: today 09:02, yesterday 14:10 or Fri 9 Oct 09:02."""
    now = now or datetime.now()
    start = now - timedelta(seconds=seconds_ago)
    clock = start.strftime("%H:%M")
    if start.date() == now.date():
        return f"today {clock}"
    if start.date() == (now - timedelta(days=1)).date():
        return f"yesterday {clock}"
    return f"{DAYS[start.weekday()]} {start.day} {MONTHS[start.month - 1]} {clock}"


class Paint:
    """ANSI colour when on, plain text when off."""

    def __init__(self, on: bool):
        self.on = on

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on and code and text.strip() else text

    def link(self, url: str) -> str:
        """A clickable url (OSC 8) in terminals that know them."""
        return f"\033]8;;{url}\033\\{self(LINK, url)}\033]8;;\033\\" if self.on else url


BOLD, DIM, WORKTREE, GONE, FREE, ACCENT, LINK = "1", "90", "33", "31", "32", "1;38;5;208", "4;36"


def cells(text: str) -> int:
    """The terminal cells text takes: two for a wide character, such as an
    emoji or a CJK one, none for a combining mark."""
    if text.isascii():
        return len(text)
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
               for char in text)


def cut(text: str, room: int) -> str:
    """The longest start of text that fits in room cells."""
    if text.isascii():
        return text[: max(room, 0)]
    used = 0
    for index, char in enumerate(text):
        used += cells(char)
        if used > room:
            return text[:index]
    return text


def clip(text: str, width: int) -> str:
    """text in width cells, with … where it was cut."""
    return text if cells(text) <= width else cut(text, width - 1) + "…"


def fit(widths: List[int], mins: List[int], room: int) -> List[int]:
    """Narrow the widest column, a character at a time, until all fit in room."""
    widths = list(widths)
    while sum(widths) > room:
        i = max(range(len(widths)), key=lambda i: widths[i] - mins[i])
        if widths[i] <= mins[i]:
            break
        widths[i] -= 1
    return widths


def terminal_width() -> int:
    """The terminal's width; a pty that reports none still gets room to work in."""
    columns = shutil.get_terminal_size((120, 24)).columns
    return columns if columns >= 40 else 120


def plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
