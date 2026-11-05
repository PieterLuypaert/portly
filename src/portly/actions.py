"""What portly does to processes and containers: stop, restart and start them, and open a port in the
browser or a checkout in the editor.

Every signal goes through send() and every docker change through docker(): the two places the
tests fence off, so a test never reaches a process it did not start.
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import scan, wt
from .base import tilde
from .model import Listener, Restart, RowKey, row_key


def env_note(plan: Restart) -> str:
    """What a restart keeps of the environment. ps cannot show a process's
    environment on recent macOS, so only wt's variables come back, and a
    command line such as PORT=3001 pnpm dev comes back as pnpm dev."""
    return "only wt's environment variables are kept" if plan.env else "environment variables are not kept"


def shown_command(plan: Restart) -> str:
    """A plan's command as you would type it: wt's for a start, else without paths."""
    if plan.argv[:2] == ["bash", "-c"] and len(plan.argv) == 3:
        return plan.argv[2]
    return scan.short_command(" ".join(plan.argv))


def docker(args: List[str], timeout: float) -> subprocess.CompletedProcess:
    """Run a docker command that changes containers; one place, so tests can fence it off.
    A docker that does not answer in time fails with that as its reason."""
    cmd = ["docker", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", f"docker did not answer within {int(timeout)}s")


def send(pid: int, sig: int) -> Optional[str]:
    """Signal pid; a reason when that is not allowed. A pid that is already
    gone is fine: it was going to stop anyway. Pid 0 and -1 signal whole
    groups of processes, and pid 1 is launchd or init. A down row has pid 0."""
    if pid <= 1:
        return f"refused to signal pid {pid}"
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        return f"no permission to stop pid {pid}"
    return None


def own_pids(item: Listener, procs: Dict[int, Tuple[int, str]]) -> List[int]:
    """What stopping item may SIGKILL: item and what runs under it, such as
    php-fpm's workers on its socket, never Docker itself. Children come before
    their parent, so none is signalled after its parent is gone and it was
    handed to launchd."""
    under = [pid for pid in scan.descendants(item.pid, procs)[::-1]
             if not scan.is_docker_itself(Listener(item.port, pid, "", args=procs[pid][1]))]
    return under + [item.pid]


def kill_late(late: Dict[int, List[int]]) -> Dict[int, str]:
    """SIGKILL the pids that outlived their SIGTERM; per port the reason when that is not allowed."""
    problems = {}
    for port, pids in late.items():
        for pid in pids:
            problem = send(pid, signal.SIGKILL)
            if problem:
                problems[port] = problem
    return problems


def taken_by(holders: List[Listener], procs: Dict[int, Tuple[int, str]]) -> str:
    """Who keeps a port taken: pid 123 (vite) listens on it. lsof only names
    the executable, node for vite, so the name comes from the command line."""
    for item in holders:
        item.args = procs.get(item.pid, (0, ""))[1]
    names = ", ".join(f"pid {item.pid} ({scan.label(item)})" for item in sorted(holders, key=lambda item: item.pid))
    return f"{names} {'listens' if len(holders) == 1 else 'listen'} on it"


def stop_many(rows: List[Listener]) -> Dict[int, Optional[str]]:
    """Free the ports of rows: one docker stop for the containers, SIGTERM for
    the processes and after five seconds SIGKILL for those that still listen on
    their port. Another server on that port, one of the row's others, is left
    running. Per port None once free, else the reason it is still taken,
    which names what still listens on it."""
    problems: Dict[int, str] = {}
    containers = [item for item in rows if item.container]
    if containers:
        names = sorted({item.container.name for item in containers})
        done = docker(["stop", *names], timeout=120)
        if done.returncode != 0:
            reason = (done.stderr.strip().splitlines() or ["docker stop failed"])[-1]
            problems.update((item.port, reason) for item in containers)
    processes = [item for item in rows if not item.container and not scan.is_docker_itself(item)]
    problems.update((item.port, scan.LEAVE_DOCKER) for item in rows if scan.is_docker_itself(item))
    procs = scan.read_processes()
    own = {item.port: own_pids(item, procs) for item in processes}
    for item in processes:
        problem = send(item.pid, signal.SIGTERM)
        if problem:
            problems[item.port] = problem
    waiting = {item.port: own.get(item.port, [item.pid]) for item in rows if item.port not in problems}
    found = scan.wait_until_gone(waiting, 5)
    # Only a process gets SIGKILL: on a container's port listens Docker itself.
    listening = {port: {item.pid for item in items} for port, items in found.items()}
    late = {port: [pid for pid in pids if pid in listening.get(port, ())]
            for port, pids in own.items() if port in waiting}
    problems.update(kill_late(late))
    if any(late.values()):
        found = scan.wait_until_gone(late, 2)
    if any(item.port in found and item.port not in problems for item in rows):
        procs = scan.read_processes()  # a supervisor may have started the server again
    return {item.port: (problems.get(item.port) or taken_by(found[item.port], procs)) if item.port in found
            else None for item in rows}


def stop(item: Listener) -> Optional[str]:
    return stop_many([item])[item.port]


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it is just not ours
    return True


def stop_tree(pids: List[int], ports: List[int], besides: Optional[Dict[int, Set[int]]] = None) -> Optional[str]:
    """SIGTERM a launcher and everything under it; after five seconds SIGKILL
    what is left. A reason when the ports stay taken by anything but besides."""
    for pid in pids:
        problem = send(pid, signal.SIGTERM)
        if problem:
            return problem
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (any(map(is_alive, pids)) or set(ports) & scan.listening_ports(besides)):
        time.sleep(0.1)
    for pid in pids:
        if is_alive(pid):
            send(pid, signal.SIGKILL)
    return "the port stayed taken" if scan.wait_for_ports(ports, 2, besides) else None


def open_log(plan: Restart):
    """The plan's log, opened to append, after a line that says what portly runs and when."""
    os.makedirs(os.path.dirname(plan.log), exist_ok=True)
    log = open(plan.log, "a")
    log.write(f"\n── portly {plan.kind} {datetime.now():%Y-%m-%d %H:%M:%S}: {' '.join(plan.argv)} ──\n")
    log.flush()
    return log


def plan_env(plan: Restart) -> Dict[str, str]:
    """portly's environment with the plan's variables. wt's variables in
    portly's own shell belong to the worktree portly runs in, not to this one."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("WT_")}
    env.update(plan.env)
    return env


def start(plan: Restart) -> int:
    """Run the plan's command in its folder, detached, its output appended to
    the plan's log. The new pid goes into wt's pid file when there is one."""
    with open_log(plan) as log:
        process = subprocess.Popen(plan.argv, cwd=plan.cwd, env=plan_env(plan), stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    if plan.pidfile:
        with open(plan.pidfile, "w") as pidfile:
            pidfile.write(f"{process.pid}\n")
    return process.pid


def restart_many(rows: List[Listener], timeout: float = 60) -> Dict[int, Optional[str]]:
    """Restart the ports of rows: one docker restart for the containers; a
    process is stopped with its launcher and the launcher runs again. A
    launcher that serves several ports (turbo) restarts once. Per port None
    once it listens again, else the reason."""
    return run_plans(rows, lambda item: (item.restart, item.restart_problem or "no restart plan"), timeout)


def start_many(rows: List[Listener], timeout: float = 60) -> Dict[RowKey, Optional[str]]:
    """Start down ports of wt stacks by their start plans, after stopping what
    still runs of their old server, and idle worktrees' stacks through wt.
    Per row (row_key) None once it listens, else the reason."""
    results: Dict[RowKey, Optional[str]] = {
        row_key(item): wt.start_idle(item, timeout) if item.start else item.start_problem or "no start plan"
        for item in rows if item.is_idle}
    procs = scan.read_processes()

    def plan_of(item: Listener) -> Tuple[Optional[Restart], str]:
        if not item.start:
            return None, item.start_problem or "no start plan"
        pids, wait = wt.old_server(item.stack, item.stack.role_of(item.port), procs)
        return (None, wait) if wait else (replace(item.start, pids=pids), "")

    down = [item for item in rows if not item.is_idle]
    return {**results, **(run_plans(down, plan_of, timeout) if down else {})}


def run_plans(rows: List[Listener], plan_of: Callable[[Listener], Tuple[Optional[Restart], str]],
              timeout: float) -> Dict[int, Optional[str]]:
    """Docker restarts the containers among rows; for every other row,
    plan_of gives its plan, whose pids stop before its command runs, or why
    it has none. Rows that share a launcher run it once."""
    results: Dict[int, Optional[str]] = {}
    containers = [item for item in rows if item.container]
    if containers:
        names = sorted({item.container.name for item in containers})
        done = docker(["restart", *names], timeout=300)
        if done.returncode != 0:
            reason = (done.stderr.strip().splitlines() or ["docker restart failed"])[-1]
            results.update((item.port, reason) for item in containers)
    plans: Dict[int, Tuple[Restart, List[Listener]]] = {}
    for item in rows:
        if item.container:
            continue
        plan, problem = plan_of(item)
        if not plan:
            results[item.port] = problem
            continue
        # A start may have nothing to stop first: such a plan is its own.
        plans.setdefault(plan.pids[0] if plan.pids else id(plan), (plan, []))[1].append(item)
    besides = others_on_ports([pid for plan, _ in plans.values() for pid in plan.pids])
    for plan, items in plans.values():
        ports = [item.port for item in items]
        problem = stop_tree(plan.pids, ports, besides)
        if not problem:
            try:
                start(plan)
            except OSError as error:
                problem = f"start failed: {error}"
        if problem:
            results.update((port, problem) for port in ports)
    waiting = [item.port for item in rows if item.port not in results]
    if waiting:
        scan.wait_for_listen(waiting, timeout, besides=besides)
        taken = scan.listening_ports(besides)
        logs = {item.port: plan.log for plan, items in plans.values() for item in items}
        for port in waiting:
            late = f"not listening after {int(timeout)}s" + (f", see {tilde(logs[port])}" if port in logs else "")
            results[port] = None if port in taken else late
    return results


def others_on_ports(pids: List[int]) -> Dict[int, Set[int]]:
    """Per port what listens on it now outside pids, such as a second server
    on [::1] with its workers: it keeps running through a restart of pids, and
    does not count as the port listening again."""
    return {port: {item.pid for item in items if item.pid not in pids}
            for port, items in scan.listeners_by_port().items()}


def restart_summary(item: Listener, rows: List[Listener]) -> str:
    """What a restart of item runs, for the detail views and the question."""
    if item.container:
        return f"docker restart {item.container.name}"
    if not item.restart:
        return ""
    plan = item.restart
    others = sorted(row.port for row in rows if row.port != item.port and not row.container
                    and row.restart and row.restart.pids[0] == plan.pids[0])
    text = f"{scan.short_command(' '.join(plan.argv))} in {tilde(plan.cwd)}"
    return text + (f" (also {', '.join(map(str, others))})" if others else "")


def start_summary(item: Listener) -> str:
    """What starting a down port runs, for the detail views and the question."""
    return f"{shown_command(item.start)} in {tilde(item.start.cwd)}" if item.start else ""


def folder_of(item: Listener) -> str:
    """The folder to open in an editor: a container's compose folder, the
    top of a server's checkout, else the folder it runs from; "" when there
    is none, such as for an app. It may be gone by now."""
    if item.container:
        return item.container.folder
    if item.place:
        return item.place.top
    return "" if item.cwd in ("", "/") else item.cwd


def can_edit(item: Listener) -> bool:
    return os.path.isdir(folder_of(item)) if folder_of(item) else False


# Where the editor command comes from, first set first.
EDITOR_VARIABLES = ("PORTLY_EDITOR", "VISUAL", "EDITOR")


def editor_command(folders: List[str]) -> Tuple[Optional[List[str]], str]:
    """The editor command with folders as its last arguments, from the
    first of EDITOR_VARIABLES that is set; else why there is none."""
    name = next((name for name in EDITOR_VARIABLES if os.environ.get(name, "").strip()), "")
    if not name:
        return None, 'no editor set: set PORTLY_EDITOR, such as PORTLY_EDITOR="open -a Cursor"'
    try:
        return shlex.split(os.environ[name]) + folders, ""
    except ValueError as error:
        return None, f"cannot read {name}: {error}"


def editor_name(command: List[str], folders: int) -> str:
    """The editor command as you would type it, without its folders: open -a Cursor."""
    return " ".join([os.path.basename(command[0])] + command[1:len(command) - folders])


def run_editor(command: List[str]) -> str:
    """Run the editor and wait for it; "" when it worked, else what went wrong."""
    try:
        done = subprocess.run(command, check=False)
    except OSError as error:
        return f"cannot run {command[0]}: {error.strerror}"
    return f"{os.path.basename(command[0])} ended with code {done.returncode}" if done.returncode else ""


def logs_command(item: Listener, lines: int, follow: bool) -> Optional[List[str]]:
    """The command that shows a port's output, or None when it writes to no file."""
    if item.container:
        return ["docker", "logs", "--tail", str(lines)] + (["--follow"] if follow else []) + [item.container.name]
    if item.log:
        return ["tail", "-n", str(lines)] + (["-F"] if follow else []) + [item.log]
    return None


def opener() -> str:
    return "open" if sys.platform == "darwin" else "xdg-open"


def open_in_browser(url: str) -> Optional[str]:
    """Hand url to the system's opener; why that failed, or None. A server,
    WSL or a container may have no xdg-open at all."""
    try:
        done = subprocess.run([opener(), url], check=False)
    except OSError:
        return f"no browser opener found ({opener()})"
    return f"{opener()} could not open it" if done.returncode else None


def copy_to_clipboard(text: str) -> bool:
    for command in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(command[0]):
            return subprocess.run(command, input=text, text=True, check=False).returncode == 0
    return False
