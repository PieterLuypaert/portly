"""The data portly passes around: what listens on a port (Listener), where it runs (Place, Stack,
Container) and how to start it again (Restart).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union


@dataclass
class Place:
    repo: str  # folder name of the main checkout
    sub: str  # folder inside the checkout, "" at its root
    branch: str
    is_worktree: bool
    top: str = ""  # root of the checkout
    common: str = ""  # git common dir, where wt keeps its registry


@dataclass
class Stack:
    """A worktree stack that wt started, read from its registry entry."""
    name: str  # wt's project name: shop in the compose project shop-wt1
    index: int
    path: str  # the worktree
    ports: Dict[str, int]  # role (web, api, gcs, ...) to port
    registry: str = ""  # the folder with wt's entries, pid files and logs
    db: str = ""  # the worktree's database, "" without one
    # Of its ports wt started a server for, how many listen and how many listen
    # or are down: set per scan by count_up, so not part of what a Stack is.
    up: int = field(default=0, compare=False, repr=False)
    of: int = field(default=0, compare=False, repr=False)

    def role_of(self, port: int) -> str:
        return next((role for role, value in self.ports.items() if value == port), "")

    def pid_file(self, role: str) -> str:
        """wt writes it when it starts the role's server and removes it when it stops it."""
        return os.path.join(self.registry, f"{self.index}-{role}.pid")

    def log_file(self, role: str) -> str:
        return os.path.join(self.registry, "logs", f"{self.index}-{role}.log")


@dataclass
class Restart:
    """How to start a server again: the command that started it, in its folder."""
    argv: List[str]
    cwd: str
    pids: List[int]  # the launcher first, then everything under it: stopped before the start
    log: str  # the new output is appended here
    pidfile: str = ""  # wt's pid file for this server: it gets the new pid, so wt down still finds it
    terminal: str = ""  # the tty it ran in; after the restart it runs in the background
    env: Dict[str, str] = field(default_factory=dict)  # set on top of portly's own environment
    kind: str = "restart"  # or start, for a port that is down: what the log's header line says


@dataclass
class Container:
    name: str
    image: str
    status: str  # docker's "Up 6 days"
    project: str  # compose project, "" outside compose
    service: str
    folder: str  # compose working dir
    inner: Dict[int, int] = field(default_factory=dict)  # host port to container port


@dataclass
class Listener:
    port: int
    pid: int
    command: str  # lsof's short name, the label of last resort
    ppid: int = 0
    elapsed: str = ""  # ps etime: [[dd-]hh:]mm:ss
    args: str = ""
    cwd: str = ""
    place: Optional[Place] = None
    container: Optional[Container] = None
    stack: Optional[Stack] = None
    addresses: List[str] = field(default_factory=list)  # bound to, such as 127.0.0.1 or *
    tty: str = ""  # ps tty: ?? on macOS and ? on Linux without a terminal
    rss_kb: int = 0
    cpu: str = ""
    others: List["Listener"] = field(default_factory=list)  # other servers on this port, beside this row
    is_down: bool = False  # a wt stack's port that nothing listens on: no process, pid 0
    is_idle: bool = False  # a worktree of a known repo where nothing runs: no port (0), no process
    idle_since: float = 0.0  # an idle worktree: when its stack last wrote to wt's logs; 0 when unknown
    last_output: List[str] = field(default_factory=list)  # a down port's last lines in wt's log
    down_since: float = 0.0  # when the live view saw a down port go down; 0 when it does not know
    # Only read for the detail views (deep=True):
    parent: str = ""  # short command line of the parent
    log: str = ""  # the file its output goes to: stdout, or an open .log
    stdout: str = ""  # stdout when that is a file
    connections: int = -1
    restart: Optional[Restart] = None
    restart_problem: str = ""  # why it cannot be restarted
    start: Optional[Restart] = None  # a down port: how to start it again
    start_problem: str = ""  # a down port: why it cannot be started


def runs(item: Listener) -> bool:
    """Whether a process or container serves a row: not a down port, nor an idle worktree."""
    return not item.is_down and not item.is_idle


# What tells rows apart: a port, or an idle worktree's folder.
RowKey = Union[int, str]


def row_key(item: Listener) -> RowKey:
    return item.cwd if item.is_idle else item.port


def name_of(item: Listener) -> str:
    """A row as a message names it: its port, or an idle worktree's stack or branch."""
    if not item.is_idle:
        return str(item.port)
    return f"wt{item.stack.index}" if item.stack else item.place.branch
