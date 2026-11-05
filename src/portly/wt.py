"""wt's worktree stacks: its registry, the ports that are down and how to start them again, and the
worktrees of the repos portly knows where nothing runs.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import base, scan, actions
from .base import HOME, etime_seconds, tilde
from .model import Listener, Place, Restart, Stack, runs


# The fields of a wt registry entry that hold no port, as wt itself reads them.
REGISTRY_FIELDS = {"branch", "path", "ports", "db", "pr", "here"}


def read_stacks(common: str) -> List[Stack]:
    """wt's registry next to a git dir: one <common>/<name>-worktrees/<n>.env
    per worktree, with REG_PATH and its ports: REG_PORTS=web=20110 api=20111,
    or a REG_<ROLE> line per port from older wt engines. A byte that is no
    UTF-8, such as in a branch name, becomes �, as in run()."""
    stacks = []
    for entry in sorted(glob.glob(os.path.join(common, "*-worktrees", "*.env"))):
        index = os.path.basename(entry)[: -len(".env")]
        if not index.isdecimal():
            continue
        values = {}
        with open(entry, encoding="utf-8", errors="replace") as lines:
            for line in lines:
                key, sep, value = line.strip().partition("=")
                if sep and key.startswith("REG_"):
                    values[key[4:].lower()] = value.strip('"')
        if values.get("ports"):
            pairs = (pair.partition("=") for pair in values["ports"].split())
            ports = {role: int(port) for role, _, port in pairs if port.isdecimal()}
        else:
            ports = {role: int(value) for role, value in values.items()
                     if value.isdecimal() and role not in REGISTRY_FIELDS}
        name = os.path.basename(os.path.dirname(entry))[: -len("-worktrees")]
        stacks.append(Stack(name, int(index), values.get("path", ""), ports, os.path.dirname(entry),
                            values.get("db", "")))
    return stacks


def wt_env(stack: Stack, place: Place) -> Dict[str, str]:
    """The variables of its worktree that wt hands every dev server it starts,
    rebuilt from its registry entry: without them a worktree's server comes
    back on the main checkout's ports or database. What wt reads from its
    config, such as WT_PRODUCT_BASE, is not in the registry."""
    env = {"WT_PROJECT": stack.name, "WT_INDEX": str(stack.index), "WT_REPO_ROOT": stack.path,
           "WT_DB_NAME": stack.db, "WT_PRIMARY_ROOT": scan.main_checkout(place.common)}
    env.update((f"WT_PORT_{role.upper().replace('-', '_')}", str(port)) for role, port in stack.ports.items())
    return env


# Where wt looks for its config in a checkout when config.yml names none.
WT_CONFIGS = [".claude/wt.config", ".claude/skills/worktree-local-env/wt.config"]


def yml_value(text: str) -> str:
    """A YAML scalar as wt reads it: without a trailing comment and quotes."""
    return re.sub(r"^[\"']|[\"']$", "", re.sub(r"\s+#.*$", "", text).strip())


def named_wt_config(root: str) -> Optional[str]:
    """The wt config a checkout's .ai-tooling/config.yml (or the older
    .claude/project.yml) names in worktree: { config: path } or a worktree:
    block with a config: line; None when it names none."""
    yml = os.path.join(root, ".ai-tooling", "config.yml")
    if not os.path.isfile(yml):
        yml = os.path.join(root, ".claude", "project.yml")
    try:
        with open(yml, encoding="utf-8", errors="replace") as text:
            lines = text.read().splitlines()
    except OSError:
        return None
    in_block = False
    for line in lines:
        if re.match(r"worktree:\s*\{", line):
            found = re.search(r"config:\s*([^,}]*)", line)
            return yml_value(found.group(1)) if found else None
        if line.startswith("worktree:"):
            in_block = True
        elif in_block and re.match(r"[^\s#]", line):
            return None
        elif in_block and re.match(r"\s+config:", line):
            return yml_value(line.partition(":")[2])
    return None


def wt_config(stack: Stack) -> str:
    """The wt.config wt reads for a stack: the first found in the main
    checkout, where wt up runs, then in the worktree; "" when there is none."""
    for root in (scan.main_checkout(os.path.dirname(stack.registry)), stack.path):
        named = named_wt_config(root)
        if named is not None:
            path = os.path.join(root, named)
            return path if named and os.path.isfile(path) else ""
        found = next((os.path.join(root, name) for name in WT_CONFIGS if os.path.isfile(os.path.join(root, name))), "")
        if found:
            return found
    return ""


# Reads a wt.config as wt does and prints what its wt_cfg_dev_cmd gives for a
# role, then every variable wt would export to it: each ended by a NUL. Exit
# 4: the config has no wt_cfg_dev_cmd; 5: it failed.
DEV_COMMAND = r"""
. "$1" >/dev/null </dev/null
declare -F wt_cfg_dev_cmd >/dev/null || exit 4
: "${WT_PORT_ORIGIN:=20000}" "${WT_PRODUCT_STRIDE:=100}" "${WT_PORT_STRIDE:=10}"
WT_PRODUCT_BASE=$(( WT_PORT_ORIGIN + ${WT_PRODUCT_NUMBER:-0} * WT_PRODUCT_STRIDE ))
export WT_PROJECT WT_PRODUCT_BASE WT_PORT_ORIGIN WT_PRODUCT_STRIDE WT_PORT_STRIDE
command="$(wt_cfg_dev_cmd "$2" </dev/null)" || exit 5
printf '%s\0' "$command"
for name in $(compgen -e); do printf '%s=%s\0' "$name" "${!name}"; done
"""


# What bash itself exports and changes: not the config's.
SHELL_VARIABLES = {"_", "PWD", "OLDPWD", "SHLVL"}


# wt_cfg_dev_cmd's answers, by everything they depend on: the live view asks every scan.
DEV_COMMANDS: Dict[tuple, Tuple[str, Dict[str, str], str]] = {}


def dev_command(stack: Stack, role: str, config: str, place: Place) -> Tuple[str, Dict[str, str], str]:
    """What wt runs for a stack's role, from wt_cfg_dev_cmd in its config,
    with what wt hands it: the WT_ variables and what the config exports.
    Else why that cannot be told."""
    try:
        changed = os.stat(config).st_mtime_ns
    except OSError as error:
        return "", {}, f"cannot read {tilde(config)}: {error.strerror}"
    key = (config, changed, stack.name, stack.index, stack.path, tuple(sorted(stack.ports.items())), stack.db,
           place.common, role)
    if key not in DEV_COMMANDS:
        DEV_COMMANDS[key] = ask_dev_command(stack, role, config, place)
    return DEV_COMMANDS[key]


def ask_dev_command(stack: Stack, role: str, config: str, place: Place) -> Tuple[str, Dict[str, str], str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("WT_")}
    env.update(wt_env(stack, place), WT_CONFIG_FILE=config,
               WT_CONFIG_DIR=os.path.realpath(os.path.dirname(config)))
    try:
        done = subprocess.run(["bash", "-c", DEV_COMMAND, "portly", config, role], cwd=stack.path, env=env,
                              capture_output=True, text=True, errors="replace", timeout=10)
    except subprocess.TimeoutExpired:
        return "", {}, f"{tilde(config)} did not answer within 10s"
    except OSError as error:
        return "", {}, f"cannot read {tilde(config)}: {error.strerror}"
    said = (done.stderr.strip().splitlines() or [""])[-1]
    if done.returncode == 4:
        return "", {}, said or f"{tilde(config)} has no wt_cfg_dev_cmd"
    if done.returncode:
        return "", {}, f"wt_cfg_dev_cmd failed for {role}" + (f": {said}" if said else "")
    command, *pairs = done.stdout.split("\0")
    if not command.strip():
        return "", {}, f"wt starts no server for {role}"
    exported = dict(pair.partition("=")[::2] for pair in pairs if "=" in pair)
    return command, {name: value for name, value in exported.items() if name not in SHELL_VARIABLES
                     and (name.startswith("WT_") or env.get(name) != value)}, ""


# A server that started this recently may still be starting up.
STILL_STARTING = 60


def old_server(stack: Stack, role: str, procs: Dict[int, Tuple[int, str]]) -> Tuple[List[int], str]:
    """What to stop before a down port starts: the server wt's pid file names
    for its role, with what runs under it, while it runs from the worktree,
    such as a watcher whose server crashed. A pid file outlives its server,
    so its pid only counts when that process started before the file was
    written; a later one got the pid after the server ended. Else the reason
    to wait: a server that started a moment ago may still be starting up."""
    path = stack.pid_file(role)
    try:
        with open(path) as text:
            pid = int(text.read().strip())
        written = os.path.getmtime(path)
    except (OSError, ValueError):
        return [], ""
    age = etime_seconds(base.run(["ps", "-o", "etime=", "-p", str(pid)])) if pid > 1 and pid in procs else None
    if age is None or time.time() - age > written + 2:
        return [], ""
    cwd, tree = scan.read_cwds({pid}).get(pid, ""), os.path.realpath(stack.path)
    if cwd != tree and not cwd.startswith(tree + os.sep):
        return [], ""
    if age < STILL_STARTING:
        return [], f"pid {pid} started {age}s ago and may still be starting up"
    return [pid] + scan.descendants(pid, procs), ""


# Restart plans of the stack ports a scan saw running, by registry, stack
# index, worktree and role: wt gives a removed worktree's index to the next.
Seen = Dict[Tuple[str, int, str, str], Restart]


def seen_key(item: Listener) -> Tuple[str, int, str, str]:
    stack = item.stack
    return stack.registry, stack.index, stack.path, stack.role_of(item.port)


def plan_start(item: Listener, seen: Seen) -> Tuple[Optional[Restart], str]:
    """How to start a down port of a wt stack again: what wt runs for its
    role, else what ran on it while portly watched; or why it cannot. What
    to stop first is only known when it starts: see old_server."""
    stack, role = item.stack, item.stack.role_of(item.port)
    if not os.path.isdir(stack.path):
        return None, "its worktree folder is gone"
    config = wt_config(stack)
    problem = f"no wt.config found for {tilde(stack.path)}"
    if config:
        command, env, problem = dev_command(stack, role, config, item.place)
        if command:
            return Restart(["bash", "-c", command], stack.path, [], stack.log_file(role), stack.pid_file(role),
                           env=env, kind="start"), ""
    before = seen.get(seen_key(item))
    if before:
        return replace(before, pids=[], pidfile=stack.pid_file(role), terminal="", kind="start"), ""
    return None, problem


def add_start_plans(rows: List[Listener], seen: Optional[Seen] = None) -> None:
    """Give each down port among rows how to start it again. seen keeps the
    restart plan of every stack port that runs, for when it goes down."""
    seen = {} if seen is None else seen
    for item in rows:
        if item.stack and item.stack.role_of(item.port) and item.restart and not item.is_down:
            seen[seen_key(item)] = item.restart
    for item in rows:
        if item.is_down:
            item.start, item.start_problem = plan_start(item, seen)


# wt ships in a repo, or with Claude Code's worktree-local-env skill.
WT_SHIPPED = os.path.join(".claude", "skills", "worktree-local-env", "scripts", "wt")


WT_IN_PLUGINS = [os.path.join(HOME, ".claude", "plugins", *["*"] * depth, "skills", "worktree-local-env", "scripts", "wt")
                 for depth in (3, 4)]


# The wt found per main checkout, kept while it is there.
WT_FOUND: Dict[str, str] = {}


def find_wt(main: str) -> str:
    """The wt to run for a repo: the one it ships, else wt on PATH, else the
    newest one in Claude Code's plugins; "" when there is none."""
    if not os.access(WT_FOUND.get(main, ""), os.X_OK):
        WT_FOUND[main] = look_for_wt(main)
    return WT_FOUND[main]


def look_for_wt(main: str) -> str:
    shipped = os.path.join(main, WT_SHIPPED)
    if os.access(shipped, os.X_OK):
        return shipped
    if shutil.which("wt"):
        return shutil.which("wt")
    found = [path for pattern in WT_IN_PLUGINS for path in glob.glob(pattern) if os.access(path, os.X_OK)]
    return max(found, key=os.path.getmtime, default="")


def plan_wt_up(item: Listener, wt: str) -> Tuple[Optional[Restart], str]:
    """How to start an idle worktree's stack with the wt found for its repo:
    wt up <branch> in the main checkout, as you would; or why portly cannot."""
    if not item.stack:
        return None, "portly does not know how to start it: it has no wt stack"
    if item.place.branch == "HEAD":
        return None, "its HEAD is detached, and wt up needs a branch"
    main = scan.main_checkout(item.place.common)
    if not wt:
        return None, "cannot find wt: not in the repo, on PATH or in Claude Code's plugins"
    log = os.path.join(base.CACHE, f"{item.place.repo}-wt{item.stack.index}.log")
    return Restart([wt, "up", item.place.branch], main, [], log, kind="start"), ""


# wt up may build before it starts anything.
WT_UP_SECONDS = 600


def start_idle(item: Listener, timeout: float = 120) -> Optional[str]:
    """Run an idle worktree's wt up, then wait until the ports wt started a
    server for listen; None then, else the reason."""
    plan, stack = item.start, item.stack
    # Its own session, as start's: leaving portly does not stop a wt up halfway.
    with actions.open_log(plan) as log:
        try:
            done = subprocess.run(plan.argv, cwd=plan.cwd, env=actions.plan_env(plan), stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=WT_UP_SECONDS, start_new_session=True)
        except subprocess.TimeoutExpired:
            return f"wt up did not finish within {WT_UP_SECONDS}s, see {tilde(plan.log)}"
        except OSError as error:
            return f"cannot run wt: {error.strerror}"
    if done.returncode:
        return f"{(last_lines(plan.log, 1) or ['wt up failed'])[-1]} (see {tilde(plan.log)})"
    ports = [port for role, port in stack.ports.items() if os.path.isfile(stack.pid_file(role))]
    if not ports:
        return f"wt up started no server, see {tilde(plan.log)}"
    if scan.wait_for_listen(ports, timeout)[0]:
        return None
    return f"not listening after {int(timeout)}s, see wt's logs in {tilde(os.path.join(stack.registry, 'logs'))}"


# Colour and cursor codes a dev server writes to its log (CSI, OSC such as a
# window title, other escapes), and the other control characters.
TERMINAL_CODES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"
                            r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
                            r"|\x1b[@-_]"
                            r"|[\x00-\x08\x0b-\x1f\x7f]")


TAIL_BYTES = 16384


def last_lines(path: str, count: int) -> List[str]:
    """The last count lines of a log that hold text, without colour codes;
    of a line a progress bar rewrote in place, what it showed last. Only the
    end of the file is read; a missing file has none."""
    try:
        with open(path, "rb") as log:
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - TAIL_BYTES))
            lines = log.read().decode("utf-8", errors="replace").split("\n")
    except OSError:
        return []
    if size > TAIL_BYTES and len(lines) > 1:
        lines = lines[1:]  # the read began inside this one
    shown = (TERMINAL_CODES.sub("", line.rstrip("\r").rpartition("\r")[2]).replace("\t", " ").strip()
             for line in lines)
    return [line for line in shown if line][-count:]


def down_ports(rows: List[Listener]) -> List[Listener]:
    """A row per port of a wt stack that wt started a server for, but that
    nothing listens on, such as the api of a stack whose web still runs, in
    its worktree's group. wt's pid file tells it started one and has not
    stopped it: the registry lists a port for every service, also those wt
    starts no server for, such as a container's. The row's output is wt's
    log for that role. A stack where nothing runs has no row to start from,
    so it gets none."""
    taken = {item.port for item in rows}
    seen: Set[Tuple[str, int]] = set()
    down = []
    for item in rows:
        stack, place = item.stack, item.place
        if not stack or not place or (stack.registry, stack.index) in seen:
            continue
        seen.add((stack.registry, stack.index))
        for role, port in stack.ports.items():
            if port in taken or not os.path.isfile(stack.pid_file(role)):
                continue
            taken.add(port)  # two stacks on the same ports: rows are told apart by port
            down.append(down_row(stack, role, place))
    return down


def down_row(stack: Stack, role: str, place: Place) -> Listener:
    """The row of a stack's port that nothing listens on, with wt's log for its role."""
    log = stack.log_file(role) if os.path.isfile(stack.log_file(role)) else ""
    return Listener(stack.ports[role], 0, role, cwd=place.top, place=replace(place, sub=""), stack=stack,
                    is_down=True, log=log, last_output=last_lines(log, 10) if log else [])


def with_down_ports(rows: List[Listener]) -> List[Listener]:
    """rows and the stack ports that are down, by port, with each stack's
    count of what is up. Only for showing: a down row has no process to
    stop or restart."""
    rows = sorted(rows + down_ports(rows), key=lambda item: item.port)
    count_up(rows)
    return rows


def known_repos_file() -> str:
    """The repos portly saw something run in, so their worktrees show when
    nothing runs: one git common dir per line."""
    return os.path.join(base.CACHE, "repos")


def read_known_repos() -> List[str]:
    try:
        with open(known_repos_file(), encoding="utf-8", errors="replace") as text:
            return [line.strip() for line in text if line.strip()]
    except OSError:
        return []  # none known yet


def write_known_repos(commons: List[str]) -> None:
    try:
        os.makedirs(base.CACHE, exist_ok=True)
        handle, path = tempfile.mkstemp(dir=base.CACHE, prefix="repos.")  # one each: two portly's can write at once
        with os.fdopen(handle, "w") as text:
            text.write("".join(f"{common}\n" for common in commons))
        os.replace(path, known_repos_file())
    except OSError as error:
        raise OSError(error.errno, f"cannot keep the repos it knows in {tilde(known_repos_file())}: "
                                   f"{error.strerror}") from error


def remember_repos(rows: List[Listener]) -> List[str]:
    """The git common dirs of the repos portly knows: those of rows, and
    those it saw before that still exist. Written back when that changed."""
    known = read_known_repos()
    seen = {item.place.common for item in rows if item.place and item.place.common}
    kept = sorted(common for common in set(known) | seen if os.path.isdir(common))
    if kept != known:
        write_known_repos(kept)
    return kept


# git worktree list per repo, by what changes when a worktree comes, goes or
# switches branch: the live view asks every scan.
WORKTREES: Dict[str, Tuple[tuple, str]] = {}


def worktree_list(common: str) -> str:
    try:
        entries = [os.path.join(entry.path, name) for entry in os.scandir(os.path.join(common, "worktrees"))
                   for name in ("HEAD", "gitdir")]
        stamp = tuple(sorted((path, os.stat(path).st_mtime_ns) for path in entries if os.path.exists(path)))
    except OSError:
        stamp = ()  # no linked worktree yet
    if WORKTREES.get(common, ((None,), ""))[0] != stamp:
        WORKTREES[common] = stamp, base.run(["git", "-C", scan.main_checkout(common), "worktree", "list", "--porcelain"])
    return WORKTREES[common][1]


def worktrees(common: str) -> List[Tuple[str, str]]:
    """Each linked worktree of a repo whose folder exists, with its branch
    (HEAD when detached), from git worktree list; not the main checkout."""
    found, path, branch = [], "", ""
    for line in worktree_list(common).splitlines() + [""]:
        if line.startswith("worktree "):
            path, branch = line[len("worktree "):], ""
        elif line.startswith("branch "):
            branch = line[len("branch "):].replace("refs/heads/", "", 1)
        elif line == "detached":
            branch = "HEAD"
        elif not line and path:
            found.append((os.path.realpath(path), branch))
            path = ""
    return [(path, branch) for path, branch in found[1:] if os.path.isdir(path)]


def last_ran(stack: Optional[Stack]) -> float:
    """When a stack's servers last wrote to wt's logs; 0 when there is no log."""
    times = []
    for path in glob.glob(os.path.join(stack.registry, "logs", f"{stack.index}-*.log")) if stack else []:
        try:
            times.append(os.path.getmtime(path))
        except OSError:
            continue  # removed since the glob
    return max(times, default=0.0)


def stopped_worktrees(rows: List[Listener], commons: List[str]) -> List[Listener]:
    """Rows for the worktrees of the repos in commons where nothing
    listens: a down row per port wt started a server for, such as a stack
    whose servers all crashed, else one idle row. A worktree that a row of
    rows belongs to is left out, and so is one whose stack has a port that
    listens, such as a container that went to another checkout's group."""
    busy = {item.place.top for item in rows if item.place}
    taken = {item.port for item in rows if not item.is_idle}  # a port gets one row, as in down_ports
    listening = {item.port for item in rows if runs(item)}
    found = []
    for common in commons:
        stacks = {os.path.realpath(stack.path): stack for stack in read_stacks(common)}
        wt: Optional[str] = None  # looked up once per repo, when it has an idle worktree
        for top, branch in worktrees(common):
            stack = stacks.get(top)
            if top in busy or (stack and listening & set(stack.ports.values())):
                continue
            place = Place(scan.repo_name(common), "", branch, True, top, common)
            down = [down_row(stack, role, place) for role, port in stack.ports.items()
                    if port not in taken and os.path.isfile(stack.pid_file(role))] if stack else []
            taken.update(item.port for item in down)
            if not down and wt is None:
                wt = find_wt(scan.main_checkout(common))
            found += down or [idle_row(place, stack, wt or "")]
    return found


def idle_row(place: Place, stack: Optional[Stack], wt: str) -> Listener:
    item = Listener(0, 0, "idle", cwd=place.top, place=place, stack=stack, is_idle=True, idle_since=last_ran(stack))
    item.start, item.start_problem = plan_wt_up(item, wt)
    return item


def warn(text: str) -> None:
    print(f"portly: {text}", file=sys.stderr)


def with_idle_worktrees(rows: List[Listener], problem: Callable[[str], None] = warn) -> List[Listener]:
    """rows and the worktrees of the repos portly knows where nothing runs,
    each stack among them with its count of what is up. When the repos it
    knows cannot be kept, problem hears why and this scan's still show."""
    try:
        commons = remember_repos(rows)
    except OSError as error:
        problem(error.strerror)
        commons = sorted({item.place.common for item in rows if item.place and item.place.common})
    stopped = stopped_worktrees(rows, commons)
    count_up(stopped)
    return rows + stopped


def count_up(rows: List[Listener]) -> None:
    """Give each wt stack among rows how many of the ports wt started a
    server for listen, of those that listen or are down: what down_ports can
    tell is down. A container's port has no pid file of wt's, so it counts
    neither way, and neither does a port another checkout holds."""
    stacks: Dict[int, Tuple[Stack, List[Listener]]] = {}
    for item in rows:
        role = item.stack.role_of(item.port) if item.stack else ""
        if role and os.path.isfile(item.stack.pid_file(role)):
            stacks.setdefault(id(item.stack), (item.stack, []))[1].append(item)
    for stack, items in stacks.values():
        stack.up, stack.of = sum(1 for item in items if not item.is_down), len(items)


# A filter word that names one wt stack whole: wt1 is not part of wt10.
STACK_WORD = re.compile(r"wt\d+")


# The folder wt makes for a worktree by default: .claude/worktrees/<branch>-wt<n>.
WT_FOLDER = re.compile(r"/\.claude/worktrees/[^/]+-(wt\d+)(?:/|$)")


def stack_word(item: Listener) -> str:
    """wt<n> for a port of wt's stack n, known from wt's registry or, once its
    entry is gone, from the compose project <name>-wt<n> or the removed
    worktree folder a server still runs from; else empty."""
    if item.stack:
        return f"wt{item.stack.index}"
    if item.container:
        found = re.fullmatch(r".+-(wt\d+)", item.container.project)
    else:
        found = WT_FOLDER.search(item.cwd) if scan.is_orphan(item) else None
    return found.group(1) if found else ""
