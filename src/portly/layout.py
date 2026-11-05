"""How rows look: groups per checkout, the columns of a row, the details and --json of a port, and which
rows a filter shows.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import scan, wt, actions
from .base import (
    Paint,
    ACCENT,
    BOLD,
    DIM,
    FREE,
    GONE,
    HOME,
    WORKTREE,
    cells,
    clip,
    etime_seconds,
    fit,
    plural,
    short_age,
    short_duration,
    started_at,
    tilde,
)
from .model import Listener, Restart, Stack, name_of, runs


def age_of(item: Listener) -> str:
    """How long it runs, or for a down port how long it has been down, when the live view saw it go."""
    if item.is_down:
        return short_duration(int(time.time() - item.down_since)) if item.down_since else ""
    return scan.docker_age(item.container.status) if item.container else short_age(item.elapsed)


@dataclass
class Group:
    """The ports of one project: a checkout, a docker project or a folder."""
    title: str
    branch: str = ""
    is_worktree: bool = False
    path: str = ""
    rows: List[Listener] = field(default_factory=list)


ORPHANS, OTHERS = "folder gone", "other"


def group_of(item: Listener) -> Tuple[tuple, Group]:
    """The group a port belongs to, with its sort key: checkouts by repo with
    the main checkout first, then docker projects, plain folders, servers whose
    folder is gone, and apps."""
    if item.place:
        place = item.place
        key = (0, place.repo, place.is_worktree, place.branch, place.top)
        return key, Group(place.repo, place.branch, place.is_worktree, tilde(place.top))
    if item.container:
        title = f"docker {item.container.project or item.container.name}"
        return (1, title), Group(title, path=tilde(item.container.folder))
    if scan.is_orphan(item):
        return (3,), Group(ORPHANS)
    if scan.is_dev(item):
        return (2, item.cwd), Group(os.path.basename(item.cwd), path=tilde(item.cwd))
    return (4,), Group(OTHERS)


def group_rows(rows: List[Listener]) -> List[Group]:
    groups: Dict[tuple, Group] = {}
    for item in rows:
        key, group = group_of(item)
        groups.setdefault(key, group).rows.append(item)
    return [groups[key] for key in sorted(groups)]


def role_of(item: Listener) -> str:
    """What part of its project a port is: its wt stack's role, its compose
    service or container, else the folder it runs from in its checkout,
    without the first folder of a deeper one: apps/web gives web, and
    apps/web/server web/server, so it stays apart from apps/api/server."""
    role = item.stack.role_of(item.port) if item.stack else ""
    if role:
        return role
    if item.container:
        return item.container.service or item.container.name
    head, _, rest = item.place.sub.partition("/") if item.place else ("", "", "")
    return rest or head


def tool_of(item: Listener) -> str:
    """What runs on a port: the tool or script, or a container's image. A down port runs nothing."""
    if item.is_down:
        return ""
    return item.container.image if item.container else scan.label(item)


# The status column: one glyph per row. Its shape keeps the meaning without colour.
RUNNING, DOWN, IDLE = "●", "✗", "○"


def status_of(item: Listener) -> Tuple[str, str]:
    if item.is_idle:
        return IDLE, DIM
    return (DOWN, GONE) if item.is_down else (RUNNING, FREE)


def branch_text(branch: str, is_worktree: bool) -> str:
    return f"wt {branch}" if is_worktree else branch


# A line as (text, tone) pieces, so the printed list and the live view share
# one layout: the one paints tones as ANSI, the other as curses attributes.
Segment = Tuple[str, str]


Line = Tuple[Optional[Listener], Optional[Group], List[Segment]]


def to_ansi(segments: List[Segment], paint: Paint) -> str:
    return "".join(paint(tone, text) for text, tone in segments)


def group_stack(group: Group) -> str:
    """wt<n> when a checkout's group is a wt stack. Only a checkout's: the
    group of servers whose folder is gone holds leftovers of any stack."""
    return next((word for word in map(wt.stack_word, group.rows) if word), "") if group.branch else ""


def header_segments(group: Group, width: int, with_path: bool = True) -> List[Segment]:
    """repo · branch · wt1 and how many of the stack's ports are up, or repo
    · branch · worktree for a worktree wt did not make, then the path, dim,
    while it fits. A part that does not fit is left out, with what follows it."""
    idle = is_idle_group(group)
    tone = GONE if group.title == ORPHANS else DIM if group.title == OTHERS or idle else ACCENT
    wt = group_stack(group)
    kind = wt or ("worktree" if group.is_worktree else "")
    branch_tone = DIM if idle else WORKTREE if group.is_worktree else ""
    parts = [(group.title, tone), (group.branch, branch_tone), (kind, DIM if idle else WORKTREE)]
    segments: List[Segment] = [("  ", "")]
    room = width - 2
    for text, part_tone in (part for part in parts if part[0]):
        gap = 3 if len(segments) > 1 else 0
        if room - gap < min(len(text), 4):
            break
        if gap:
            segments.append((" · ", DIM))
        text = clip(text, room - gap)
        segments.append((text, part_tone))
        room -= gap + len(text)
    stack = next((item.stack for item in group.rows if item.stack and f"wt{item.stack.index}" == wt), None)
    if stack and stack.of and segments[-1] == (wt, WORKTREE) and room - 2 >= len(up_text(stack)):
        segments += [("  ", ""), (up_text(stack), FREE if stack.up == stack.of else WORKTREE)]
        room -= 2 + len(up_text(stack))
    if with_path and group.path and room - 2 >= min(len(group.path), 4):
        segments += [("  ", ""), (clip(group.path, room - 2), DIM)]
    return segments


def group_name(group: Group) -> str:
    """A group as a question names it: shop wt fix/cart."""
    return f"{group.title} {branch_text(group.branch, group.is_worktree)}".strip()


def up_text(stack: Stack) -> str:
    return f"{stack.up}/{stack.of} up"


def note_of(item: Listener) -> List[Segment]:
    """The dim text at the end of a row: +1 process when another server
    shares the port, and the folder a server runs from when it is gone.
    Docker's own pid says nothing about the container, so a port with only
    containers beside it says +1 container. A down port notes the last line
    of its log, which is often why it went down."""
    notes: List[Segment] = []
    if item.is_down:
        return [(item.last_output[-1], GONE)] if item.last_output else notes
    if item.others:
        containers = all(other.container for other in item.others)
        kind = ("container", "containers") if containers else ("process", "processes")
        notes.append(("+" + plural(len(item.others), *kind), DIM))
    if scan.is_orphan(item):
        notes.append((tilde(item.cwd), GONE))
    return notes


# A row has every column from this much room on: an 80-column window, whose
# last column the live view leaves empty. Narrower, a row keeps port, name,
# status and age, and its note while that fits.
FULL_ROW = 79


@dataclass
class Columns:
    """What the rows of one list share: the port and age widths that line them up, and each row's age."""
    port_w: int
    age_w: int
    ages: Dict[int, str]


def group_lines(groups: List[Group], width: int) -> List[Line]:
    """Every line of the grouped list: a header per group, a row per port and
    a blank line between groups. Columns line up across groups."""
    rows = [item for group in groups for item in group.rows if not item.is_idle]
    row_segments = {}
    if rows:
        ages = {id(item): age_of(item) for item in rows}
        columns = Columns(max(len(str(item.port)) for item in rows), max(map(len, ages.values())), ages)
        row_segments = (full_rows if width >= FULL_ROW else narrow_rows)(rows, width, columns)
    lines: List[Line] = []
    after_idle = False
    for group in groups:
        idle = is_idle_group(group)
        if lines and not (idle and after_idle):  # idle worktrees follow each other without a gap
            lines.append((None, None, []))
        if idle:
            lines.append((group.rows[0], group, idle_line(group, width)))
        else:
            lines.append((None, group, header_segments(group, width)))
            lines += [(item, group, row_segments[id(item)]) for item in group.rows]
        after_idle = idle
    return lines


def is_idle_group(group: Group) -> bool:
    return bool(group.rows) and all(item.is_idle for item in group.rows)


def idle_line(group: Group, width: int) -> List[Segment]:
    """An idle worktree in one dim line: its header without the path, then
    ○ idle · ports 20130-20133 · last ran 2d ago, while that fits."""
    item = group.rows[0]
    parts = ["idle"]
    if item.stack and item.stack.ports:
        parts.append("ports " + port_span(item.stack.ports.values()))
    if item.idle_since:
        parts.append(f"last ran {short_duration(int(time.time() - item.idle_since))} ago")
    segments = header_segments(group, width - 8, with_path=False)  # room left for ○ idle
    room = width - sum(cells(text) for text, _ in segments) - 4
    return segments + [("  ", ""), (IDLE, DIM), (" ", ""), (clip(" · ".join(parts), room), DIM)]


def port_span(ports) -> str:
    """20130-20133 for ports in a row, else 20130, 20135."""
    ordered = sorted(set(ports))
    if len(ordered) > 2 and ordered[-1] - ordered[0] == len(ordered) - 1:
        return f"{ordered[0]}-{ordered[-1]}"
    return ", ".join(map(str, ordered))


def port_segments(item: Listener, columns: Columns) -> List[Segment]:
    return [("    ", ""), (str(item.port).rjust(columns.port_w), DIM if item.is_down else BOLD)]


def row_end(item: Listener, columns: Columns, width: int, segments: List[Segment]) -> List[Segment]:
    """segments, then status, age and each note that fits."""
    segments = segments + [("  ", ""), status_of(item), ("  ", ""), (columns.ages[id(item)].rjust(columns.age_w), DIM)]
    for text, tone in note_of(item):
        room = width - sum(cells(piece) for piece, _ in segments) - 2
        if room >= min(len(text), 4):
            segments += [("  ", ""), (clip(text, room), tone)]
    return segments


def full_rows(rows: List[Listener], width: int, columns: Columns) -> Dict[int, List[Segment]]:
    """Per row: port, role, tool, status, age and the note, which gets what is left."""
    roles = {id(item): role_of(item) for item in rows}
    tools = {id(item): tool_of(item) for item in rows}
    widths = [max(map(len, col.values())) for col in (roles, tools)]
    gaps = 2 * sum(1 for w in widths if w)
    role_w, tool_w = fit(widths, [min(w, 8) for w in widths], width - 4 - columns.port_w - gaps - 5 - columns.age_w)
    lines = {}
    for item in rows:
        segments = port_segments(item, columns)
        if role_w:
            segments += [("  ", ""), (clip(roles[id(item)], role_w).ljust(role_w), DIM if item.is_down else "")]
        if tool_w:
            segments += [("  ", ""), (clip(tools[id(item)], tool_w).ljust(tool_w), DIM)]
        lines[id(item)] = row_end(item, columns, width, segments)
    return lines


def narrow_rows(rows: List[Listener], width: int, columns: Columns) -> Dict[int, List[Segment]]:
    """Per row: port, its role or else its tool, status, age, and the note while it fits."""
    names = {id(item): role_of(item) or tool_of(item) for item in rows}
    name_w = max(1, min(max(map(len, names.values())), width - 4 - columns.port_w - 2 - 5 - columns.age_w))
    return {id(item): row_end(item, columns, width, port_segments(item, columns) + [
        ("  ", ""), (clip(names[id(item)], name_w).ljust(name_w), DIM if item.is_down else "")]) for item in rows}


def render_groups(groups: List[Group], paint: Paint, width: int) -> List[str]:
    return [to_ansi(segments, paint).rstrip() for _, _, segments in group_lines(groups, width)]


def haystack(item: Listener) -> str:
    """Everything a filter word can match, lower case."""
    words = [str(item.port), scan.label(item), item.cwd]
    if item.place:
        words += [item.place.repo, item.place.branch, item.place.top]
    if item.container:
        c = item.container
        words += [c.name, c.project, c.service, c.image]
    if item.stack:
        words.append(f"wt{item.stack.index}")
    if scan.is_orphan(item):
        words.append(ORPHANS)
    return "\n".join(words).lower()


def matches(item: Listener, terms: List[str]) -> bool:
    """True when any of the filter words occurs in the port's project, branch,
    folder, tool or container. A stack word such as wt1 only matches the ports
    of that stack, not those of wt10 or a branch or folder that contains it."""
    text, stack = haystack(item), wt.stack_word(item)
    return any(term == stack if wt.STACK_WORD.fullmatch(term) else term in text
               for term in map(str.lower, terms))


# Ports that speak a database protocol, not http: no url for them.
NOT_HTTP = {3306, 5432, 6379, 9042, 11211, 27017, 5672}


LOOPBACK = re.compile(r"127\.[\d.]+|\[::1\]|localhost")


def url_of(item: Listener) -> str:
    if not runs(item):
        return ""
    inner = item.container.inner.get(item.port) if item.container else None
    if item.port in NOT_HTTP or inner in NOT_HTTP:
        return ""
    # The symfony server speaks https unless started with --no-tls.
    secure = scan.label(item) == "symfony" and "--no-tls" not in item.args
    return f"{'https' if secure else 'http'}://localhost:{item.port}"


def is_local_only(item: Listener) -> bool:
    return bool(item.addresses) and all(LOOPBACK.fullmatch(address) for address in item.addresses)


def output_of(item: Listener) -> str:
    """Where a process's output goes."""
    if item.log:
        return tilde(item.log)
    if item.is_idle:
        return "nothing runs"
    if item.is_down:
        return "no log from wt"
    if scan.has_terminal(item.tty):
        return f"terminal {item.tty}"
    return "no terminal or log file"


def detail_fields(item: Listener, rows: Optional[List[Listener]] = None) -> List[Tuple[str, str, str, str]]:
    """Key, value, the value's tone and a dim note per line of the detail views.
    rows, all ports, name the others a restart takes along."""
    fields = []

    def add(key: str, value: str, tone: str = "", note: str = "") -> None:
        fields.append((key, value, tone, note))

    if item.place:
        add("project", f"{item.place.repo} · {branch_text(item.place.branch, item.place.is_worktree)}")
    if item.stack:
        add("stack", f"wt {item.stack.index} of {item.stack.name}", note=item.stack.role_of(item.port))
    if item.is_idle:
        return fields + idle_fields(item)
    if item.is_down:
        since = f" · since {started_at(int(time.time() - item.down_since))}" if item.down_since else ""
        add("state", "down", GONE, "nothing listens on it" + since)
        if item.start:
            add("start", actions.start_summary(item))
        elif item.start_problem:
            add("start", "not possible", DIM, item.start_problem)
        add("output", output_of(item))
        for number, line in enumerate(item.last_output):
            add("last lines" if number == 0 else "", line, GONE if number == len(item.last_output) - 1 else DIM)
        return fields
    if item.container:
        c = item.container
        add("container", c.name)
        add("image", c.image)
        if c.project:
            add("compose", f"{c.project} · {c.service}", note=tilde(c.folder))
        inner = c.inner.get(item.port)
        if inner and inner != item.port:
            add("inside", f"port {inner} in the container")
        add("status", c.status, GONE if "unhealthy" in c.status else "")
    else:
        orphan = scan.is_orphan(item)
        add("folder", tilde(item.cwd), GONE if orphan else "", "gone" if orphan else "")
        add("command", item.args.replace(HOME, "~") or item.command)
        parent = f"started by {item.parent} ({item.ppid})" if item.parent else \
            "detached from its terminal" if item.ppid == 1 else ""
        add("pid", str(item.pid), note=parent)
        seconds = etime_seconds(item.elapsed)
        add("running", age_of(item), note=f"since {started_at(seconds)}" if seconds is not None else "")
        if item.rss_kb:
            add("memory", f"{round(item.rss_kb / 1024)} MB", note=f"cpu {item.cpu}%")
        add("output", output_of(item))
    for other in item.others:
        what = f"container {other.container.name}" if other.container else f"pid {other.pid} ({scan.label(other)})"
        add("also", what, note=" ".join(other.addresses))
    if item.addresses:
        if is_local_only(item):
            add("reach", "this Mac only" if sys.platform == "darwin" else "this computer only",
                note=" ".join(item.addresses))
        else:
            add("reach", "your network too", WORKTREE, " ".join(item.addresses))
    if item.connections >= 0:
        add("connections", f"{item.connections} open")
    if item.container or item.restart:
        add("restart", actions.restart_summary(item, rows or [item]), note=actions.env_note(item.restart) if item.restart else "")
    elif item.restart_problem:
        add("restart", "not possible", DIM, item.restart_problem)
    return fields


def idle_fields(item: Listener) -> List[Tuple[str, str, str, str]]:
    """What the details of an idle worktree add after its project and stack."""
    ran = f" · last ran {started_at(int(time.time() - item.idle_since))}" if item.idle_since else ""
    fields = [("state", "idle", DIM, "nothing runs in this worktree" + ran), ("folder", tilde(item.cwd), "", "")]
    if item.stack:
        ports = sorted(item.stack.ports.items(), key=lambda pair: pair[1])
        fields.append(("ports", " · ".join(f"{port} {role}" for role, port in ports), "", ""))
    if item.start:
        fields.append(("start", actions.start_summary(item), "", ""))
    else:
        fields.append(("start", "not possible", DIM, item.start_problem))
    return fields


def hints_for(item: Listener) -> List[str]:
    if item.is_idle:
        return [f"portly up {name_of(item)}"] if item.start and item.stack else []
    if item.is_down:
        return ([f"portly up {item.port}"] if item.start else []) + ([f"portly logs {item.port}"] if item.log else [])
    hints = []
    if url_of(item):
        hints.append(f"portly open {item.port}")
    if item.container or item.log:
        hints.append(f"portly logs {item.port}")
    if item.container or item.restart:
        hints.append(f"portly restart {item.port}")
    if not scan.is_docker_itself(item):
        hints.append(f"portly kill {item.port}")
    return hints


def render_detail(item: Listener, paint: Paint, rows: Optional[List[Listener]] = None) -> List[str]:
    port = str(item.port)
    url = url_of(item)
    lines = [f"  {paint(BOLD, port)}  {paint(BOLD, scan.label(item))}" + (f"  {paint.link(url)}" if url else "")]
    indent = " " * (len(port) + 4)
    fields = detail_fields(item, rows)
    key_w = max(len(key) for key, *_ in fields) + 2
    for key, value, tone, note in fields:
        lines.append(f"{indent}{paint(DIM, key.ljust(key_w))}{paint(tone, value)}"
                     + (f"  {paint(DIM, note)}" if note else ""))
    lines.append("")
    lines.append(indent + paint(DIM, "  ·  ".join(hints_for(item))))
    return lines


def as_json(item: Listener, deep: bool = False) -> dict:
    """One port as plain data; fields that do not apply are null. deep adds
    what only the detail views read: log, parent, connections and restart.
    A down port has no process, so only its log, which is wt's, and how it
    starts again."""
    place, stack, container = item.place, item.stack, item.container
    process = not container and runs(item)
    data = {
        "port": None if item.is_idle else item.port,
        "name": scan.label(item),
        "down": item.is_down,
        "idle": item.is_idle,
        "url": url_of(item) or None,
        "addresses": item.addresses,
        "local_only": is_local_only(item),
        "project": place and {"repo": place.repo, "branch": place.branch, "worktree": place.is_worktree,
                              "path": place.top, "folder": place.sub},
        "stack": stack and {"name": stack.name, "index": stack.index,
                            "role": stack.role_of(item.port) or None,
                            "up": stack.up if stack.of else None, "of": stack.of or None, "ports": stack.ports},
        "container": container and {"name": container.name, "image": container.image,
                                    "status": container.status, "compose_project": container.project or None,
                                    "service": container.service or None,
                                    "inner_port": container.inner.get(item.port)},
        "pid": item.pid if process else None,
        "command": item.args if process else None,
        "cwd": item.cwd if process else None,
        "folder_gone": scan.is_orphan(item),
        "running_seconds": etime_seconds(item.elapsed) if process else None,
        "memory_kb": item.rss_kb if process else None,
        "others": [as_json(other, deep) for other in item.others],
        "last_lines": item.last_output if item.is_down else None,
        "last_ran_seconds": int(time.time() - item.idle_since) if item.is_idle and item.idle_since else None,
    }
    if deep:
        data.update(log=item.log or None, parent=item.parent or None,
                    connections=item.connections if runs(item) else None,
                    restart=plan_json(item.restart), restart_problem=item.restart_problem or None,
                    start=plan_json(item.start), start_problem=item.start_problem or None)
    return data


def plan_json(plan: Optional[Restart]) -> Optional[dict]:
    return plan and {"command": plan.argv, "cwd": plan.cwd, "log": plan.log, "env": plan.env}


def visible(rows: List[Listener], terms: List[str], show_all: bool) -> List[Listener]:
    """The rows to list: dev servers, or everything with -a. A filter that finds
    nothing among the dev servers also looks at apps, so `portly spotify` works."""
    dev = rows if show_all else [item for item in scan.drop_helpers(rows) if scan.is_dev(item)]
    if not terms:
        return dev
    found = [item for item in dev if matches(item, terms)]
    return found or [item for item in rows if matches(item, terms)]


def summary_segments(groups: List[Group]) -> List[Segment]:
    """2 projects · 4 ports, then · 1 down in red when a stack port is, and · 3 idle for idle worktrees."""
    rows = [item for group in groups for item in group.rows]
    down, idle = sum(1 for item in rows if item.is_down), sum(1 for item in rows if item.is_idle)
    projects = sum(1 for group in groups if any(not item.is_idle for item in group.rows))
    ports = sum(1 for item in rows if runs(item))
    segments = [(f"{plural(projects, 'project', 'projects')} · {plural(ports, 'port', 'ports')}", DIM)]
    segments += [(" · ", DIM), (f"{down} down", GONE)] if down else []
    return segments + ([(" · ", DIM), (f"{idle} idle", DIM)] if idle else [])
