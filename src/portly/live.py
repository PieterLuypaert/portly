"""portly live: the curses view, the scanner that reads every port in the background, and its keys.
"""
from __future__ import annotations

import curses
import os
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import scan, wt, actions, layout
from .base import ACCENT, BOLD, DIM, FREE, GONE, LINK, SPINNER, WORKTREE, cells, clip, cut, plural, tilde
from .model import Listener, RowKey, name_of, row_key, runs


class DownClock:
    """When each port stopped listening, as the live view's scans saw it: a
    port that listened at one scan and not at the next went down then. It
    keeps that time while the port stays down, also through scans that show
    no row for it, such as while the rest of its stack restarts."""

    def __init__(self):
        self.listening: Set[int] = set()
        self.went_down: Dict[int, float] = {}

    def update(self, rows: List[Listener], now: Optional[float] = None) -> None:
        """Note this scan, and give its down rows their time; 0 when the port was down from the first scan on."""
        now = time.time() if now is None else now
        listening = {item.port for item in rows if runs(item)}
        for port in self.listening - listening:
            self.went_down.setdefault(port, now)
        for port in listening:
            self.went_down.pop(port, None)
        self.listening = listening
        for item in rows:
            if item.is_down:
                item.down_since = self.went_down.get(item.port, 0.0)


class Scanner(threading.Thread):
    """Reads every port in the background, every few seconds or on demand."""

    def __init__(self, interval: float = 2.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.rows: Optional[List[Listener]] = None
        self.version = 0
        self.scanned_at = 0.0
        self.error = ""
        self.clock = DownClock()
        self.seen: wt.Seen = {}  # how each stack port it saw run was started, to start it once it is down

    def run(self) -> None:
        while True:
            problems: List[str] = []
            try:
                rows = wt.with_idle_worktrees(wt.with_down_ports(scan.one_per_port(scan.read_listeners(deep=True))), problems.append)
                self.clock.update(rows)
                wt.add_start_plans(rows, self.seen)
            except Exception as error:  # the view stays up and shows what went wrong
                with self.lock:
                    self.error = f"scan failed: {error}"
            else:
                with self.lock:  # a problem keeping the known repos shows where a failed scan does
                    self.rows, self.version, self.error = rows, self.version + 1, "; ".join(problems)
                    self.scanned_at = time.monotonic()
            self.wake.wait(self.interval)
            self.wake.clear()

    def refresh(self) -> None:
        self.wake.set()

    def latest(self) -> Tuple[int, Optional[List[Listener]], str, float]:
        with self.lock:
            return self.version, self.rows, self.error, self.scanned_at


TONES = ["", BOLD, DIM, WORKTREE, GONE, FREE, ACCENT, LINK]


class Styles:
    """Curses attributes per tone, plain and on the selected row."""

    def __init__(self):
        self.normal = {tone: 0 for tone in TONES}
        self.selected = {tone: curses.A_REVERSE for tone in TONES}
        self.bar = curses.A_REVERSE | curses.A_BOLD
        extra = {BOLD: curses.A_BOLD, ACCENT: curses.A_BOLD, LINK: curses.A_UNDERLINE}
        if not curses.has_colors():
            self.normal.update(extra)
            return
        curses.start_color()
        curses.use_default_colors()
        rich = curses.COLORS >= 256
        fg = {"": -1, BOLD: -1, DIM: 245 if rich else curses.COLOR_WHITE, WORKTREE: curses.COLOR_YELLOW,
              GONE: curses.COLOR_RED, FREE: curses.COLOR_GREEN, ACCENT: 208 if rich else curses.COLOR_YELLOW,
              LINK: curses.COLOR_CYAN}
        if not rich:
            extra[DIM] = curses.A_DIM
        pair = 1
        for tone in TONES:
            curses.init_pair(pair, fg[tone], -1)
            self.normal[tone] = curses.color_pair(pair) | extra.get(tone, 0)
            curses.init_pair(pair + 1, fg[tone], 237 if rich else -1)
            self.selected[tone] = curses.color_pair(pair + 1) | extra.get(tone, 0) | (0 if rich else curses.A_REVERSE)
            pair += 2
        curses.init_pair(pair, curses.COLOR_BLACK, 208 if rich else curses.COLOR_YELLOW)
        self.bar = curses.color_pair(pair) | curses.A_BOLD


MOUSE_ON, MOUSE_OFF = "\033[?1000h\033[?1006h", "\033[?1000l\033[?1006l"


def terminal_write(text: str) -> None:
    sys.__stdout__.write(text)
    sys.__stdout__.flush()


class KeyReader:
    """Reads keys with curses' own key translation off and decodes escape codes
    itself: arrows in both cursor modes (a terminal or multiplexer that ignores
    application mode sends codes ncurses does not expect), page and home keys,
    and mouse reports in SGR and the old X10 form, which macOS's old ncurses
    cannot read (no SGR, no wheel-down). Keys come back as names: "up",
    "enter", "esc", a plain character, ("click", y) or ("wheel", +1 or -1)."""

    SS3 = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
    TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "5": "pgup", "6": "pgdn", "3": "delete"}

    def __init__(self, screen):
        self.screen = screen

    def char(self, wait_ms: int):
        self.screen.timeout(wait_ms)
        try:
            return self.screen.get_wch()
        except curses.error:
            return None  # nothing arrived within wait_ms

    def read(self, wait_ms: int = 100):
        key = self.char(wait_ms)
        if key is None:
            return None
        if key == curses.KEY_RESIZE:
            return "resize"
        if isinstance(key, int):
            return None
        if key == "\x1b":
            return self.escape()
        if key in ("\n", "\r"):
            return "enter"
        if key in ("\x7f", "\b"):
            return "backspace"
        return key

    def escape(self):
        first = self.char(30)
        if first is None:
            return "esc"
        if first == "O":
            return self.SS3.get(self.char(30))
        if first != "[":
            return None  # alt with a key: not used
        body = ""
        while True:
            key = self.char(30)
            if not isinstance(key, str):
                return None
            if body == "" and key == "M":
                return self.x10_mouse()
            body += key
            if "@" <= key <= "~" and not (body.startswith("<") and key not in "Mm"):
                break
        if body.startswith("<"):
            return self.sgr_mouse(body)
        final, params = body[-1], body[:-1].split(";")
        if final == "~":
            return self.TILDE.get(params[0])
        return self.SS3.get(final)

    @staticmethod
    def mouse(button: int, y: int, pressed: bool):
        if button & 64:
            return ("wheel", 1 if button & 1 else -1)
        if pressed and button & 3 == 0:
            return ("click", y)
        return None

    def sgr_mouse(self, body: str):
        match = re.fullmatch(r"<(\d+);(\d+);(\d+)([Mm])", body)
        if not match:
            return None
        return self.mouse(int(match.group(1)), int(match.group(3)) - 1, match.group(4) == "M")

    def x10_mouse(self):
        codes = [self.char(30) for _ in range(3)]
        if not all(isinstance(code, str) for code in codes):
            return None
        button, _, y = (ord(code) - 32 for code in codes)
        return self.mouse(button, y - 1, button & 3 != 3)


class LiveView:
    """portly live: the grouped list on top, the selected port below, keys at the bottom."""

    # The key row: only the keys that do something for the selected row.
    KEYS = [("↑↓", "select"), ("o", "open"), ("l", "logs"), ("e", "edit"), ("r", "restart"), ("k", "stop"),
            ("u", "start"), ("RKU", "stack"), ("?", "keys")]
    # What ? lists: every key.
    ALL_KEYS = [
        ("↑↓ pgup pgdn home end", "select a port; a click selects one, the wheel scrolls"),
        ("o enter", "open its url in the browser"),
        ("c", "copy its url"),
        ("l", "follow its output"),
        ("e", "open its checkout in your editor: $PORTLY_EDITOR, else $VISUAL or $EDITOR"),
        ("r", "restart it the way it was started"),
        ("k", "stop it"),
        ("u", "start a stack's port that is down, the way wt started it, or an idle worktree's stack"),
        ("R K", "restart or stop every port of its stack or project"),
        ("U", "start every port of its stack that is down, or an idle worktree's stack; what runs keeps running"),
        ("/", "filter by project, branch, folder or tool; enter keeps it"),
        ("esc", "clear the filter"),
        ("a", "show apps and helper processes too, or hide them again"),
        ("?", "these keys"),
        ("q", "quit"),
    ]

    def __init__(self, screen, scanner: Scanner, terms: List[str], show_all: bool):
        self.screen, self.scanner = screen, scanner
        self.filter, self.show_all, self.editing = " ".join(terms), show_all, False
        self.all_rows: List[Listener] = []
        self.groups: List[layout.Group] = []
        self.order: List[RowKey] = []  # rows in the order they are listed, by row_key
        self.selected: Optional[RowKey] = None
        self.last_index = 0
        self.scroll = 0
        self.version, self.error, self.scanned_at = 0, "", 0.0
        self.confirm: Optional[Tuple[str, List[Listener], str]] = None  # question, targets, stop or restart
        self.busy, self.busy_lock = "", threading.Lock()
        self.message, self.message_tone, self.message_at = "", "", 0.0
        self.row_at: Dict[int, RowKey] = {}  # screen line to row, for the mouse
        self.showing_keys = False  # ? shows every key over the list, until the next key

    # --- state

    def pull(self) -> None:
        version, rows, self.error, self.scanned_at = self.scanner.latest()
        if rows is not None and version != self.version:
            self.version, self.all_rows = version, rows
            self.rebuild()

    def rebuild(self, from_top: bool = False) -> None:
        """Regroup after a scan or a new filter. A port that left keeps the cursor
        where it was; a new filter starts at the top."""
        rows = layout.visible(self.all_rows, self.filter.split(), self.show_all)
        self.groups = layout.group_rows(rows)
        self.order = [row_key(item) for group in self.groups for item in group.rows]
        if self.selected not in self.order:
            index = 0 if from_top else min(self.last_index, len(self.order) - 1)
            self.selected = self.order[index] if self.order else None
        if self.selected is not None:
            self.last_index = self.order.index(self.selected)

    def current(self) -> Optional[Listener]:
        return next((item for group in self.groups for item in group.rows if row_key(item) == self.selected), None)

    def current_group(self) -> Optional[layout.Group]:
        return next((group for group in self.groups if any(row_key(item) == self.selected for item in group.rows)),
                    None)

    def move(self, delta: int) -> None:
        if not self.order:
            return
        index = self.order.index(self.selected) if self.selected in self.order else 0
        self.last_index = max(0, min(len(self.order) - 1, index + delta))
        self.selected = self.order[self.last_index]

    def say(self, text: str, tone: str = "") -> None:
        self.message, self.message_tone, self.message_at = text, tone, time.monotonic()

    # --- actions

    def open_selected(self) -> None:
        item = self.current()
        url = layout.url_of(item) if item else ""
        if not url:
            self.say("no url for this port", DIM)
            return
        try:
            subprocess.Popen([actions.opener(), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.say(f"no browser opener found ({actions.opener()})", GONE)
            return
        self.say(f"opened: {url}", FREE)

    def copy_selected(self) -> None:
        item = self.current()
        url = layout.url_of(item) if item else ""
        if url and actions.copy_to_clipboard(url):
            self.say(f"copied: {url}", FREE)
        else:
            self.say("nothing to copy" if not url else "no clipboard found", DIM)

    def follow_logs(self) -> None:
        item = self.current()
        command = actions.logs_command(item, 200, True) if item else None
        if not command:
            self.say(f"does not write to a file: {layout.output_of(item)}" if item else "", DIM)
            return

        def follow() -> None:
            print(f"\n\033[90m── {item.port} {scan.label(item)} · ctrl-c takes you back ──\033[0m\n", flush=True)
            subprocess.run(command, check=False)

        self.outside(follow)

    def edit_selected(self) -> None:
        """Open the selected row's folder in the editor, as l follows logs:
        a GUI editor returns at once, a terminal one when you quit it."""
        item = self.current()
        folder = actions.folder_of(item) if item else ""
        if not folder or not os.path.isdir(folder):
            self.say(f"its folder is gone: {tilde(folder)}" if folder else "no folder to open for this row", DIM)
            return
        command, problem = actions.editor_command([folder])
        if not command:
            self.say(problem, DIM)
            return
        name = actions.editor_name(command, 1)

        def edit() -> str:
            # An editor that waits, such as code --wait from $VISUAL, leaves this line on the screen.
            print(f"\n\033[90m── opening {tilde(folder)} with {name} · ctrl-c takes you back ──\033[0m\n", flush=True)
            return actions.run_editor(command)

        problem = self.outside(edit)
        if problem is None:
            self.say(f"left {name} with ctrl-c", DIM)
        else:
            self.say(problem or f"opened {tilde(folder)} with {name}", GONE if problem else FREE)

    def outside(self, action: Callable[[], object]) -> object:
        """Put the view away, run action on the plain terminal and draw the
        view again; what action gives, or None after a ctrl-c, which leaves
        what action runs, not portly."""
        terminal_write(MOUSE_OFF)
        curses.def_prog_mode()
        curses.endwin()
        try:
            return action()
        except KeyboardInterrupt:
            return None  # how you leave the logs, or an editor that waits
        finally:
            curses.reset_prog_mode()
            terminal_write(MOUSE_ON)
            self.screen.clear()

    def is_busy(self) -> bool:
        """Whether a stop, restart or start still runs, and if so, say so: one
        at a time, as two could act on the same port, and the first to end
        would hide the other."""
        with self.busy_lock:
            if self.busy:
                self.say("still busy, one stop, restart or start at a time", DIM)
            return bool(self.busy)

    def ask(self, action: str, whole_group: bool) -> None:
        """Put a stop or restart question on the bottom line."""
        item, group = self.current(), self.current_group()
        if not item or not group or self.is_busy():
            return
        verb = "Stop" if action == "stop" else "Restart"
        if not runs(item) and not whole_group:
            hint = "; u starts it" if item.start else ""
            what = f"{layout.group_name(group)} is idle" if item.is_idle else f"{item.port} {scan.label(item)} is down"
            self.say(f"{what}: nothing runs to {action}{hint}", DIM)
            return
        if action == "restart" and not whole_group and not (item.container or item.restart):
            self.say(f"{item.port} cannot restart: {item.restart_problem or 'no restart plan'}", DIM)
            return
        if whole_group:
            name = layout.group_name(group)
            running = [row for row in group.rows if runs(row)]
            if not running:  # a filter can leave only down rows
                self.say(f"nothing runs in {name} to {action}", DIM)
                return
            self.confirm = (f"{verb} {plural(len(running), 'port', 'ports')} of {name}?", running, action)
        else:
            command = scan.short_command(" ".join(item.restart.argv)) if item.restart else ""
            plan = f" with {command}" if command else ""
            self.confirm = (f"{verb} {item.port} {scan.label(item)}{plan}?", [item], action)

    def ask_start(self, whole_group: bool) -> None:
        """Put a start question on the bottom line: for the selected port when
        it is down, or for every down port of its stack."""
        item, group = self.current(), self.current_group()
        if not item or not group or self.is_busy():
            return
        if item.is_idle:  # the row is the whole stack
            if item.start:
                self.confirm = (f"Start {layout.group_name(group)} with {actions.shown_command(item.start)}?", [item], "start")
            else:
                self.say(f"cannot start {layout.group_name(group)}: {item.start_problem}", DIM)
        elif whole_group:
            down, name = [row for row in group.rows if row.is_down], layout.group_name(group)
            doable = [row for row in down if row.start]
            if not down:
                self.say(f"nothing is down in {name}", DIM)
            elif not doable:
                self.say(f"cannot start {name}: {down[0].start_problem or 'no start plan'}", DIM)
            else:
                count = f"{len(doable)} of " if len(doable) < len(down) else ""
                ports = plural(len(down), "down port", "down ports")
                self.confirm = (f"Start {count}{ports} of {name}?", doable, "start")
        elif not item.is_down:
            self.say(f"{item.port} {scan.label(item)} runs already", DIM)
        elif not item.start:
            self.say(f"{item.port} cannot start: {item.start_problem or 'no start plan'}", DIM)
        else:
            self.confirm = (f"Start {item.port} {scan.label(item)} with {actions.shown_command(item.start)}?", [item], "start")

    # What each action says while it runs.
    BUSY = {"stop": "stopping {}", "restart": "restarting {}, waiting until it listens again",
            "start": "starting {}, waiting until it listens"}

    def run_in_background(self, action: str, targets: List[Listener]) -> None:
        names = " ".join(map(name_of, targets))
        named = {row_key(item): name_of(item) for item in targets}
        self.busy = self.BUSY[action].format(names)
        self.message = ""  # the spinner line shows only what is said while it runs

        def work() -> None:
            try:
                results = {"stop": actions.stop_many, "restart": actions.restart_many, "start": actions.start_many}[action](targets)
                problems = [f"{named.get(key, key)}: {problem}" for key, problem in results.items() if problem]
                if problems:
                    outcome = (("still taken, " if action == "stop" else "") + "; ".join(problems), GONE)
                else:
                    done = "free" if action == "stop" else "back up"
                    outcome = (f"{names} {'is' if len(targets) == 1 else 'are'} {done}", FREE)
            except Exception as error:  # the view stays up and says what went wrong
                outcome = (f"{action} failed: {error}", GONE)
            with self.busy_lock:  # ask refuses under it too, so its refusal never replaces this outcome
                self.say(*outcome)
                self.busy = ""
            self.scanner.refresh()

        threading.Thread(target=work, daemon=True).start()

    # --- keys

    def handle(self, key) -> bool:
        """Act on a key; False to leave."""
        if self.confirm:
            self.answer(key)
            return True
        if self.editing:
            return self.edit_filter(key)
        if self.showing_keys:
            self.showing_keys = key == "resize"  # any key closes the list and does nothing else
            return True
        if key == "q":
            return False
        if key == "esc":
            self.message = ""
            if self.filter:
                self.filter = ""
                self.rebuild()
        moves = {"up": -1, "down": 1, "pgup": -10, "pgdn": 10, "home": -10 ** 6, "end": 10 ** 6}
        actions = {"o": self.open_selected, "enter": self.open_selected, "l": self.follow_logs, "e": self.edit_selected,
                   "c": self.copy_selected, "k": lambda: self.ask("stop", False), "K": lambda: self.ask("stop", True),
                   "r": lambda: self.ask("restart", False), "R": lambda: self.ask("restart", True),
                   "u": lambda: self.ask_start(False), "U": lambda: self.ask_start(True)}
        if isinstance(key, tuple):
            self.mouse(key)
        elif key in moves:
            self.move(moves[key])
        elif key in actions:
            actions[key]()
        elif key == "/":
            self.editing = True
        elif key == "?":
            self.showing_keys = True
        elif key == "a":
            self.show_all = not self.show_all
            self.rebuild()
        return True

    def answer(self, key) -> None:
        """y says yes; n, esc and enter say no, as [y/N] does. Any other key, a
        resize or the mouse included, leaves the question open."""
        if key not in ("y", "Y", "n", "N", "esc", "enter"):
            return
        _, targets, action = self.confirm
        self.confirm = None
        if key in ("y", "Y"):
            self.run_in_background(action, targets)
        else:
            self.say({"stop": "nothing stopped", "restart": "nothing restarted", "start": "nothing started"}[action], DIM)

    def edit_filter(self, key) -> bool:
        if key == "enter":
            self.editing = False
        elif key == "esc":
            self.filter, self.editing = "", False
        elif key == "backspace":
            self.filter = self.filter[:-1]
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.filter += key
        self.rebuild(from_top=True)
        return True

    def mouse(self, event: tuple) -> None:
        kind, value = event
        if kind == "wheel":
            self.move(value)
        elif value in self.row_at:
            self.selected = self.row_at[value]
            self.last_index = self.order.index(self.selected)

    # --- drawing

    def put(self, y: int, x: int, text: str, attr: int) -> int:
        """Write text clipped to the screen; the x after it."""
        width = self.screen.getmaxyx()[1] - 1
        if x >= width or not text:
            return x
        try:
            self.screen.addstr(y, x, cut(text, width - x), attr)
        except curses.error:
            pass  # curses refuses the bottom-right cell; the text before it is drawn
        return min(width, x + cells(text))

    def put_segments(self, y: int, segments: List[layout.Segment], styles: Dict[str, int], fill: bool = False) -> None:
        x = 0
        for text, tone in segments:
            x = self.put(y, x, text, styles[tone])
        if fill:
            self.put(y, x, " " * self.screen.getmaxyx()[1], styles[""])

    def detail_segments(self, item: Listener, width: int) -> List[List[layout.Segment]]:
        url = layout.url_of(item)
        if item.is_idle:
            lines = [[(" ", ""), (item.place.branch, BOLD), ("  ", ""), ("idle", DIM)]]
        else:
            lines = [[(" ", ""), (str(item.port), BOLD), ("  ", ""), (scan.label(item), BOLD)]
                     + ([("  ", ""), (url, LINK)] if url else [])]
        fields = layout.detail_fields(item, self.all_rows)
        key_w = max(len(key) for key, *_ in fields) + 2
        for key, value, tone, note in fields:
            lines.append([("   ", ""), (key.ljust(key_w), DIM), (value, tone)] + ([("  ", ""), (note, DIM)] if note else []))
        return lines

    def footer_segments(self, width: int) -> Tuple[List[layout.Segment], bool]:
        """The bottom line, and whether it is an input line."""
        said = [(" " + self.message, self.message_tone)] if self.message and time.monotonic() - self.message_at < 6 else []
        if self.confirm:
            question = clip(self.confirm[0], width - 14)
            return [(" " + question, GONE), ("  y", BOLD), ("es / ", DIM), ("n", BOLD), ("o", DIM)], False
        if self.editing:
            return [(" / ", ACCENT), (self.filter, BOLD), ("▏", ACCENT),
                    ("   enter done · esc clears", DIM)], True
        if self.busy:
            frame = SPINNER[int(time.monotonic() * 8) % len(SPINNER)]
            busy = clip(self.busy, width - 6 - sum(len(text) for text, _ in said))
            return [(f" {frame} ", ACCENT), (busy, ""), ("  ", "")] + said, False
        if said:
            return said, False
        if self.showing_keys:
            return [(" any key closes this", DIM)], False
        group = self.current_group()
        possible = self.possible_keys()
        segments: List[layout.Segment] = [(" ", "")]
        for keys, what in self.KEYS:
            if keys == "RKU" and group and not layout.group_stack(group):
                what = "project"
            if keys == "RKU":  # the stack keys show as one, with the letters that do something
                shown = "".join(key for key in keys if key in possible)
            else:
                shown = keys if keys in possible else ""
            if shown:
                segments += [(shown, ACCENT), (f" {what}  ", DIM)]
        return segments, False

    def possible_keys(self) -> Set[str]:
        """The keys of the key row that do something for the selected row."""
        item, group = self.current(), self.current_group()
        if not item or not group:
            return {"?"}
        running = any(runs(row) for row in group.rows)
        checks = {"↑↓": True, "o": bool(layout.url_of(item)), "l": bool(item.container or item.log), "e": actions.can_edit(item),
                  "r": bool(item.container or item.restart),
                  "k": runs(item) and not scan.is_docker_itself(item), "u": bool(item.start),
                  "R": running, "K": running, "U": any(row.start for row in group.rows), "?": True}
        return {key for key, on in checks.items() if on}

    def keys_lines(self) -> List[List[layout.Segment]]:
        key_w = max(len(key) for key, _ in self.ALL_KEYS) + 3
        return [[("  ", ""), ("keys", BOLD)], []] + [
            [("    ", ""), (key.ljust(key_w), ACCENT), (what, "")] for key, what in self.ALL_KEYS]

    def title_segments(self, width: int) -> Tuple[List[layout.Segment], List[layout.Segment]]:
        left = [("  ", DIM)] + layout.summary_segments(self.groups)
        right: List[layout.Segment] = []
        if self.filter:
            right += [("/", ACCENT), (self.filter, BOLD), ("  ", "")]
        if self.show_all:
            right += [("all", WORKTREE), ("  ", "")]
        if self.error:
            right += [(self.error, GONE), ("  ", "")]
        elif self.scanned_at:
            ago = int(time.monotonic() - self.scanned_at)
            right += [("● live", FREE) if ago < 5 else (f"refreshed {ago}s ago", DIM), (" ", "")]
        return left, right

    def draw(self, styles: Styles) -> None:
        screen = self.screen
        height, width = screen.getmaxyx()
        screen.erase()
        if height < 10 or width < 50:
            self.put(0, 0, "portly live: make the window bigger", styles.normal[""])
            screen.refresh()
            return
        left, right = self.title_segments(width)
        x = self.put(0, 0, " portly ", styles.bar)
        for text, tone in left:
            x = self.put(0, x, text, styles.normal[tone])
        right_w = sum(len(text) for text, _ in right)
        x = max(x + 2, width - 1 - right_w)
        for text, tone in right:
            x = self.put(0, x, text, styles.normal[tone])
        if self.showing_keys:
            self.row_at = {}
            for offset, segments in enumerate(self.keys_lines()[: height - 3]):
                self.put_segments(2 + offset, segments, styles.normal)
            self.put_segments(height - 1, self.footer_segments(width)[0], styles.normal)
            screen.refresh()
            return

        item = self.current()
        detail = self.detail_segments(item, width) if item else []
        detail_h = min(len(detail) + 1, max(0, (height - 4) // 2)) if detail else 0
        top, bottom = 2, height - 1 - detail_h
        lines = layout.group_lines(self.groups, width - 1)
        self.keep_visible(lines, bottom - top)
        self.row_at = {}
        if not lines:
            empty = f"Nothing found for {self.filter}." if self.filter else "Nothing is listening."
            self.put(top, 4, empty, styles.normal[DIM])
        for offset, (row, _, segments) in enumerate(lines[self.scroll: self.scroll + bottom - top]):
            y = top + offset
            chosen = row is not None and row_key(row) == self.selected
            self.put_segments(y, segments, styles.selected if chosen else styles.normal, fill=chosen)
            if row is not None:
                self.row_at[y] = row_key(row)
        above = sum(1 for row, _, _ in lines[: self.scroll] if row)
        below = sum(1 for row, _, _ in lines[self.scroll + bottom - top:] if row)
        if above:
            self.put(1, width - 12, f"↑ {above} more".rjust(10), styles.normal[DIM])
        if detail_h or below:
            rule = "─" * (width - 14) + (f" ↓ {below} more " if below else "─" * 12)
            self.put(bottom, 0, rule.ljust(width, "─"), styles.normal[DIM])
        if detail_h:
            for offset, segments in enumerate(detail[: detail_h - 1]):  # line bottom is the rule
                self.put_segments(bottom + 1 + offset, segments, styles.normal)

        footer, typing = self.footer_segments(width)
        self.put_segments(height - 1, footer, styles.normal)
        screen.refresh()

    def keep_visible(self, lines: List[layout.Line], room: int) -> None:
        """Scroll so the selected row shows, with its group's header when it is the first row."""
        index = next((i for i, (row, _, _) in enumerate(lines) if row is not None and row_key(row) == self.selected),
                     None)
        if index is not None:
            first = index - 1 if index > 0 and lines[index - 1][0] is None and lines[index - 1][1] else index
            if first < self.scroll:
                self.scroll = first
            elif index >= self.scroll + room:
                self.scroll = index - room + 1
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - room)))

    def run(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # a terminal without an invisible cursor shows it, no harm
        self.screen.keypad(False)
        keys = KeyReader(self.screen)
        styles = Styles()
        terminal_write(MOUSE_ON)
        try:
            while True:
                self.pull()
                self.draw(styles)
                key = keys.read()
                if key is not None and not self.handle(key):
                    return
        except KeyboardInterrupt:
            return
        finally:
            terminal_write(MOUSE_OFF)
