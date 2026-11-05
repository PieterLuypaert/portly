"""What listens and what runs it: lsof, ps and docker read into Listeners, one row per port, restart
plans, and waiting until ports listen or are free.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

from . import base, wt
from .base import short_duration
from .model import Container, Listener, Place, Restart, Stack, runs


# Executables that run a script: the script names the process, not the binary.
# macOS runs python3 as .../Python.app/Contents/MacOS/Python.
INTERPRETER = re.compile(r"node|bun|deno|python[\d.]*|ruby|php")


def port_of(address: str) -> Optional[int]:
    """The port of an lsof address such as 127.0.0.1:8000, *:80 or [::1]:3000."""
    port = address.rpartition(":")[2]
    return int(port) if port.isdigit() else None


def parse_lsof(text: str) -> List[Listener]:
    """One Listener per port and pid from `lsof -F pcn` output; a process that
    listens on both IPv4 and IPv6 is counted once."""
    found = {}
    pid, command = 0, ""
    for line in text.splitlines():
        field, value = line[:1], line[1:]
        if field == "p":
            pid = int(value)
        elif field == "c":
            command = value
        elif field == "n":
            port = port_of(value)
            if port is not None:
                item = found.setdefault((port, pid), Listener(port, pid, command))
                address = value.rpartition(":")[0]
                if address not in item.addresses:
                    item.addresses.append(address)
    return list(found.values())


def main_checkout(common: str) -> str:
    """The main checkout of a git common dir: the folder that holds .git."""
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common


def repo_name(common: str) -> str:
    """A repo's name: the folder of its main checkout, without .git."""
    name = os.path.basename(main_checkout(common))
    return name[:-4] if name.endswith(".git") else name


def place_of(cwd: str) -> Optional[Place]:
    if not cwd or cwd == "/":
        return None
    out = base.run([
        "git", "-C", cwd, "rev-parse", "--path-format=absolute",
        "--show-toplevel", "--git-common-dir", "--abbrev-ref", "HEAD",
    ]).splitlines()
    if len(out) != 3:
        return None
    top, common, branch = os.path.realpath(out[0]), os.path.realpath(out[1]), out[2]
    main = main_checkout(common)
    sub = os.path.relpath(os.path.realpath(cwd), top)
    return Place(
        repo=repo_name(common),
        sub="" if sub == "." else sub,
        branch=branch,
        is_worktree=top != main,
        top=top,
        common=common,
    )


class Lookup:
    """Places and registries found during one scan: many ports share a folder."""

    def __init__(self):
        self.places: Dict[str, Optional[Place]] = {}
        self.stacks: Dict[str, List[Stack]] = {}

    def place(self, path: str) -> Optional[Place]:
        if path not in self.places:
            self.places[path] = place_of(path)
        return self.places[path]

    def stacks_of(self, place: Place) -> List[Stack]:
        if place.common not in self.stacks:
            self.stacks[place.common] = wt.read_stacks(place.common)
        return self.stacks[place.common]

    def stack_at(self, place: Place) -> Optional[Stack]:
        return next((stack for stack in self.stacks_of(place)
                     if os.path.realpath(stack.path) == place.top), None)

    def container_home(self, container: "Container") -> Tuple[Optional[Place], Optional[Stack]]:
        """The checkout a container belongs to: its wt worktree when wt started
        it, else the checkout of its compose folder."""
        home = self.place(container.folder) if container.folder else None
        match = re.fullmatch(r"(.+)-wt(\d+)", container.project)
        if home and match:
            for stack in self.stacks_of(home):
                if (stack.name, stack.index) == (match.group(1), int(match.group(2))) and stack.path:
                    return self.place(stack.path), stack
        return home, None


def read_cwds(pids: Set[int]) -> Dict[int, str]:
    """The working folder of each of pids that still runs."""
    cwds, pid = {}, 0
    listed = ",".join(sorted(map(str, pids)))
    for line in (base.run(["lsof", "-a", "-p", listed, "-d", "cwd", "-Fpn"]) if listed else "").splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n"):
            cwds[pid] = line[1:]
    return cwds


def add_process_info(listeners: List[Listener], lookup: Lookup) -> None:
    """Fill in parent, age, command line, working folder and place."""
    pids = ",".join(sorted({str(item.pid) for item in listeners}))
    info = {}
    for line in base.run(["ps", "-o", "pid=,ppid=,etime=,tty=,rss=,%cpu=,command=", "-p", pids]).splitlines():
        parts = line.split(None, 6)
        if len(parts) == 7:
            info[int(parts[0])] = parts[1:]
    cwds = read_cwds({item.pid for item in listeners})
    for item in listeners:
        if item.pid in info:
            ppid, item.elapsed, item.tty, rss, item.cpu, item.args = info[item.pid]
            item.ppid, item.rss_kb = int(ppid), int(rss) if rss.isdigit() else 0
        item.cwd = cwds.get(item.pid, "")
        item.place = lookup.place(item.cwd)


DOCKER_FORMAT = "\t".join([
    "{{.Names}}", "{{.Image}}", "{{.Ports}}", "{{.Status}}",
    '{{.Label "com.docker.compose.project"}}',
    '{{.Label "com.docker.compose.service"}}',
    '{{.Label "com.docker.compose.project.working_dir"}}',
])


def parse_docker(text: str) -> Dict[int, Container]:
    """Containers by the host ports they publish, from `docker ps` in DOCKER_FORMAT."""
    by_port = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        name, image, ports, status, project, service, folder = fields
        container = Container(name, image, status, project, service, folder)
        for match in re.finditer(r":(\d+)(?:-(\d+))?->(\d+)(?:-\d+)?/tcp", ports):
            first, inner = int(match.group(1)), int(match.group(3))
            for offset in range(int(match.group(2) or first) - first + 1):
                by_port[first + offset] = container
                container.inner[first + offset] = inner + offset
    return by_port


def short_command(args: str) -> str:
    """A command line without the interpreter and paths: pnpm dev, vite --port 1."""
    words = args.split()
    if len(words) > 1 and INTERPRETER.fullmatch(os.path.basename(words[0]).lower()) \
            and not words[1].startswith("-"):
        words = words[1:]
    return " ".join([os.path.basename(words[0])] + words[1:]) if words else ""


def count_connections(text: str) -> Counter:
    """Open connections per local port, from `lsof -F n` on established sockets."""
    counts: Counter = Counter()
    for line in text.splitlines():
        if line.startswith("n") and "->" in line:
            port = port_of(line[1:].partition("->")[0])
            if port is not None:
                counts[port] += 1
    return counts


def find_logs(text: str) -> Tuple[Dict[int, str], Dict[int, str]]:
    """Per pid its stdout when that is a file, and the file its output goes to:
    that stdout, else the first .log file it has open (symfony's server). From
    `lsof -F pftn`."""
    stdout, open_logs = {}, {}
    pid, fd, kind = 0, "", ""
    for line in text.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value)
        elif tag == "f":
            fd = value
        elif tag == "t":
            kind = value
        elif tag == "n" and kind == "REG":
            if fd == "1":
                stdout[pid] = value
            elif value.endswith(".log"):
                open_logs.setdefault(pid, value)
    return stdout, {**open_logs, **stdout}


# Tools that run a server for you: a restart reruns the topmost of them.
PACKAGE_MANAGERS = {"npm", "npx", "pnpm", "yarn", "bun", "bunx"}


RUNNERS = PACKAGE_MANAGERS | {"turbo", "nx", "nodemon", "concurrently", "tsx", "ts-node", "deno"}


def read_processes() -> Dict[int, Tuple[int, str]]:
    """Every process: pid to parent and command line."""
    procs = {}
    for line in base.run(["ps", "-A", "-o", "pid=,ppid=,command="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) == 3 else "")
    return procs


def runner_name(args: str) -> str:
    short = short_command(args)
    return short.split(" ")[0] if short else ""


def launcher_of(pid: int, procs: Dict[int, Tuple[int, str]]) -> int:
    """The process that started a server: from the server up through the tools
    that run it (pnpm, npm, turbo, nodemon), stopping below anything else, such
    as a shell, a script like wt, or launchd."""
    current = pid
    while True:
        parent = procs.get(current, (0, ""))[0]
        if parent <= 1 or parent not in procs or runner_name(procs[parent][1]) not in RUNNERS:
            return current
        current = parent


def descendants(pid: int, procs: Dict[int, Tuple[int, str]]) -> List[int]:
    children: Dict[int, List[int]] = {}
    for child, (parent, _) in procs.items():
        children.setdefault(parent, []).append(child)
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def has_terminal(tty: str) -> bool:
    """Whether ps's tty is a terminal: without one ps shows ?? on macOS and ? on Linux."""
    return tty not in ("", "?", "??")


def restart_argv(args: str, cwd: str) -> Optional[List[str]]:
    """The command to run again, or None when it cannot be found any more. A
    package manager runs through PATH: the folder it ran from, such as the bin
    of an fnm shell that has closed, may be gone. ps joins arguments with
    spaces, so an argument that held a space does not come back."""
    words = args.split()
    if not words:
        return None
    short = short_command(args).split()
    if short and short[0] in PACKAGE_MANAGERS and shutil.which(short[0]):
        return short
    program = words[0]
    found = os.path.exists(os.path.join(cwd, program)) if "/" in program else shutil.which(program)
    return words if found else None


def default_log(item: Listener) -> str:
    name = item.place.repo if item.place else re.sub(r"\W+", "-", label(item))
    return os.path.join(base.CACHE, f"{name}-{item.port}.log")


def plan_restart(item: Listener, procs: Dict[int, Tuple[int, str]],
                 cwds: Dict[int, str]) -> Tuple[Optional[Restart], str]:
    """How to restart a process, or why that cannot be done."""
    if is_orphan(item):
        return None, "its folder no longer exists"
    if is_docker_itself(item):
        return None, LEAVE_DOCKER
    if not is_dev(item):
        return None, "not a dev server: restart apps yourself"
    launcher = launcher_of(item.pid, procs)
    cwd = item.cwd if launcher == item.pid else cwds.get(launcher, "")
    if not cwd or not os.path.isdir(cwd):
        return None, "the folder of its start command is unknown"
    args = procs.get(launcher, (0, item.args))[1]
    argv = restart_argv(args, cwd)
    if not argv:
        return None, f"cannot find its start command: {short_command(args)}"
    pidfile = ""
    if item.stack and item.stack.registry:
        candidate = item.stack.pid_file(item.stack.role_of(item.port))
        pidfile = candidate if os.path.exists(candidate) else ""
    # Output already going to a file means it ran in the background anyway.
    terminal = item.tty if has_terminal(item.tty) and not item.stdout else ""
    env = wt.wt_env(item.stack, item.place) if item.stack and item.place else {}
    return Restart(argv, cwd, [launcher] + descendants(launcher, procs), item.stdout or default_log(item),
                   pidfile, terminal, env), ""


def add_restart_plans(listeners: List[Listener]) -> None:
    procs = read_processes()
    processes = [item for item in listeners if not item.container]
    launchers = {item.pid: launcher_of(item.pid, procs) for item in processes}
    cwds = read_cwds({pid for pid in launchers.values() if pid not in launchers})
    for item in processes:
        item.restart, item.restart_problem = plan_restart(item, procs, cwds)


def add_deep_info(listeners: List[Listener]) -> None:
    """Parents, output files, open connections and restart plans, for the detail views."""
    ppids = ",".join(sorted({str(item.ppid) for item in listeners if item.ppid > 1}))
    parents = {}
    for line in (base.run(["ps", "-o", "pid=,command=", "-p", ppids]) if ppids else "").splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            parents[int(pid)] = short_command(args.strip())
    pids = ",".join(sorted({str(item.pid) for item in listeners}))
    stdouts, logs = find_logs(base.run(["lsof", "-p", pids, "-Fpftn"]))
    counts = count_connections(base.run(["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fn"]))
    for item in listeners:
        item.parent = parents.get(item.ppid, "")
        item.log = logs.get(item.pid, "")
        item.stdout = stdouts.get(item.pid, "")
        item.connections = counts.get(item.port, 0)
    add_restart_plans(listeners)


def read_listeners(deep: bool = False) -> List[Listener]:
    listeners = parse_lsof(base.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"]))
    if not listeners:
        return listeners
    lookup = Lookup()
    add_process_info(listeners, lookup)
    # Without Docker, with its daemon down, or while it does not answer, this is
    # empty. Only docker's hang means that: a hanging lsof is no empty list.
    try:
        containers = parse_docker(base.run(["docker", "ps", "--format", DOCKER_FORMAT]))
    except subprocess.TimeoutExpired:
        containers = {}
    add_containers(listeners, containers)
    for item in listeners:
        if item.container:
            item.place, item.stack = lookup.container_home(item.container)
        elif item.place:
            item.stack = lookup.stack_at(item.place)
    if deep:
        add_deep_info(listeners)
    return listeners


def add_containers(listeners: List[Listener], containers: Dict[int, Container]) -> None:
    """Give each listener the container whose port it listens on. Beside a
    server of its own on that port, such as a local Postgres on 127.0.0.1:5432
    next to a Postgres container on *:5432, only the runtime's listener is the
    container's."""
    runtime_ports = {item.port for item in listeners if is_runtime(item)}
    for item in listeners:
        if item.port not in runtime_ports or is_runtime(item):
            item.container = containers.get(item.port)


def one_per_port(listeners: List[Listener]) -> List[Listener]:
    """One row per port, sorted by port. When workers share the port with
    their parent (php-fpm), the parent stands for them. Another server on the
    port, such as a local Postgres beside a Postgres container, goes in the
    row's others; the row is a container before a dev server before an app,
    so a shared port keeps its place in the list. A runtime that publishes a
    port with a process per address, as docker-proxy does on Linux, is one
    server."""
    by_port = {}
    for item in listeners:
        by_port.setdefault(item.port, []).append(item)
    kept = []
    for _, group in sorted(by_port.items()):
        pids = {item.pid for item in group}
        roots = [item for item in group if item.ppid not in pids] or group
        row = min(roots, key=lambda item: (item.container is None, not is_dev(item), item.pid))
        row.others = [item for item in roots if item is not row and not (is_runtime(item) and is_runtime(row))]
        kept.append(row)
    return kept


def drop_helpers(rows: List[Listener]) -> List[Listener]:
    """Drop a process whose parent listens too, such as php-fpm under the
    symfony server: its port is internal. A down row's pid 0 is nobody's
    parent, also not of a process whose ppid is 0, such as launchd."""
    pids = {item.pid for item in rows if runs(item)}
    return [item for item in rows if item.ppid not in pids]


# Container runtimes publish container ports on the host from a process of
# their own: Docker Desktop's com.docker.backend, docker-proxy on Linux,
# rootless Docker's rootlesskit, OrbStack Helper, Podman machine's gvproxy and
# Lima's limactl hostagent, which Colima and Rancher Desktop run too. When
# docker does not say which container a port is, such a port looks like a plain
# process.
DOCKER_ITSELF = re.compile(r"com\.docke.*|dockerd|docker-proxy|vpnkit.*|rootlessk.*|OrbStack.*|gvproxy|limactl")


# Others are known by their command line. ps joins a program's path and its
# arguments with spaces, so /Applications/Rancher Desktop.app/.../limactl has no
# name to read. Podman machine's VM (vfkit, krunkit) listens for its API, and
# stopping it stops every container all the same; your own vfkit VM has no
# Podman machine files.
RUNTIME_ARGS = re.compile(r"(?:^|/)(?:limactl hostagent |(?:vfkit|krunkit) .*/containers/podman/machine/)")


# Lima's ssh forwarder (Colima's default) is the ssh master on an instance's
# control socket, and Lima's QEMU VM holds the VM's ssh port: both name the
# instance folder. That folder is in ~/.lima, ~/.colima/_lima (or
# ~/.config/colima/_lima) or rancher-desktop/lima, or holds the lima.yaml of an
# instance under a LIMA_HOME of its own. Any other ssh or QEMU, such as your own
# ssh -L, is yours to stop.
LIMA_FOLDER = re.compile(r"^ssh: (.+)/ssh\.sock \[mux\]|(?:^|/)ssh .*ControlPath=\"?(.+?)/ssh\.sock"
                         r"|(?:^|/)qemu-system-\S+ .*-pidfile (.+?)/qemu\.pid")


LIMA_HOMES = re.compile(r".*/(?:\.lima|\.?colima/_lima|rancher-desktop/lima)/[^/]+")


LEAVE_DOCKER = "that is the container runtime itself, not one container: portly leaves it running"


def names_lima_instance(args: str) -> bool:
    match = LIMA_FOLDER.search(args)
    folder = next(filter(None, match.groups()), "") if match else ""
    return bool(folder) and bool(LIMA_HOMES.fullmatch(folder) or os.path.isfile(os.path.join(folder, "lima.yaml")))


def is_runtime(item: Listener) -> bool:
    """A container runtime's process by its command, also one that publishes
    a container's port."""
    words = item.args.split()
    exe = os.path.basename(words[0]) if words else ""
    return (any(DOCKER_ITSELF.fullmatch(name) for name in (exe, item.command))
            or bool(RUNTIME_ARGS.search(item.args)) or names_lima_instance(item.args))


def is_docker_itself(item: Listener) -> bool:
    """A container runtime's own process, such as Docker's: stopping it stops
    every container with it."""
    return not item.container and is_runtime(item)


def is_dev(item: Listener) -> bool:
    """Dev servers run from a project folder; desktop apps run from /."""
    return bool(item.container) or item.cwd not in ("", "/")


def label(item: Listener) -> str:
    """A short name: the compose service, the tool (vite, next-server,
    symfony) or the script."""
    if item.container:
        return item.container.service or item.container.name
    words = item.args.split()
    if not words:
        return item.command
    exe = os.path.basename(words[0]).rstrip(":")
    if not INTERPRETER.fullmatch(exe.lower()):
        return exe
    exe = exe.lower()
    package = re.search(r"node_modules/(?:\.bin/(?:\.\./)?)?((?:@[^/\s]+/)?[^/\s]+)", item.args)
    if package:
        return package.group(1)
    script = next((word for word in words[1:] if not word.startswith("-")), "")
    if os.path.isabs(script):
        script = os.path.basename(script)
    return f"{exe} {script}" if script else exe


UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400,
                "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}


def docker_age(status: str) -> str:
    """Docker's "Up 6 days (healthy)" or "Up About an hour" as 6d or 1h."""
    match = re.match(r"Up (?:About |Less than )?(an?|\d+) (second|minute|hour|day|week|month|year)", status)
    if not match:
        return ""
    count = int(match.group(1)) if match.group(1).isdigit() else 1
    return short_duration(count * UNIT_SECONDS[match.group(2)])


def is_orphan(item: Listener) -> bool:
    """Still running from a folder that was deleted, such as a removed worktree."""
    return bool(item.cwd) and not os.path.isdir(item.cwd)


LSOF_LISTEN = ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"]


def listening_ports(besides: Optional[Dict[int, Set[int]]] = None) -> set:
    """The ports something listens on. besides gives per port the pids that do
    not count, such as another server beside the one that is restarting."""
    besides = besides or {}
    return {item.port for item in parse_lsof(base.run(LSOF_LISTEN)) if item.pid not in besides.get(item.port, ())}


def listening_pids(port: int) -> List[int]:
    return [int(pid) for pid in base.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).split()]


def wait_for_ports(ports: List[int], seconds: float, besides: Optional[Dict[int, Set[int]]] = None) -> set:
    """Wait until none of ports listens, but for besides; the ones still taken when time is up."""
    deadline = time.monotonic() + seconds
    while True:
        busy = set(ports) & listening_ports(besides)
        if not busy or time.monotonic() > deadline:
            return busy
        time.sleep(0.1)


def listeners_by_port() -> Dict[int, List[Listener]]:
    found: Dict[int, List[Listener]] = {}
    for item in parse_lsof(base.run(LSOF_LISTEN)):
        found.setdefault(item.port, []).append(item)
    return found


def wait_until_gone(pids: Dict[int, List[int]], seconds: float) -> Dict[int, List[Listener]]:
    """Wait until none of the pids listens on its port any more; what listens
    per port when that is so or time is up."""
    deadline = time.monotonic() + seconds
    while True:
        found = listeners_by_port()
        left = any(item.pid in pids.get(port, ()) for port, items in found.items() for item in items)
        if not left or time.monotonic() > deadline:
            return found
        time.sleep(0.1)


def is_bindable(port: int) -> bool:
    """True when a server could listen on port, on localhost and on all addresses."""
    for host in ("127.0.0.1", "0.0.0.0"):
        with socket.socket() as sock:
            try:
                sock.bind((host, port))
            except OSError:
                return False
    return True


def free_ports(start: int, count: int, taken: set) -> List[int]:
    found, port = [], start
    while len(found) < count and port < 65536:
        if port not in taken and is_bindable(port):
            found.append(port)
        port += 1
    return found


def wait_for_listen(ports: List[int], timeout: float, tick=None,
                    besides: Optional[Dict[int, Set[int]]] = None) -> Tuple[bool, float]:
    """Wait until every port listens, but for besides; whether they did, and after
    how long. A timeout of 0 waits for ever. tick(seconds, waiting) runs on every check."""
    start = time.monotonic()
    while True:
        waited = time.monotonic() - start
        listening = listening_ports(besides)
        waiting = [port for port in ports if port not in listening]
        if not waiting:
            return True, waited
        if timeout and waited >= timeout:
            return False, waited
        if tick:
            tick(waited, waiting)
        time.sleep(0.25)
