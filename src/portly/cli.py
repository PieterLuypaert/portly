"""The commands as you type them, from portly list to portly wait, and main().
"""
from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import re
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

from . import scan, wt, actions, layout, live
from .base import Paint, ACCENT, BOLD, DIM, FREE, GONE, SPINNER, plural, terminal_width, tilde
from .model import Listener, name_of, row_key, runs


def show_ports(ports: List[int], rows: List[Listener], paint: Paint) -> int:
    by_port = {item.port: item for item in rows}
    lines = []
    for port in ports:
        item = by_port.get(port)
        if lines:
            lines.append("")
        if item:
            lines += layout.render_detail(item, paint, rows)
        else:
            lines.append(f"  {paint(BOLD, str(port))} is {paint(FREE, 'free')}.")
    print("\n" + "\n".join(lines) + "\n")
    return 0


def pick_targets(rows: List[Listener], ports: List[int], terms: List[str]) -> Tuple[List[Listener], List[int]]:
    """What to act on, by port, then idle worktrees, and the asked ports
    that are already free. A word picks every dev server it matches; helpers
    stop with their parent. Rows are told apart by row_key: every idle
    worktree has port 0."""
    by_port = {item.port: item for item in rows if not item.is_idle}
    chosen = {port: by_port[port] for port in ports if port in by_port}
    if terms:
        for item in layout.visible(rows, terms, show_all=False):
            chosen.setdefault(row_key(item), item)
    order = sorted(chosen.values(), key=lambda item: (item.is_idle, item.port, item.cwd))
    return order, [port for port in ports if port not in by_port]


def confirm(question: str) -> Optional[bool]:
    """Ask yes or no on the terminal; None when there is no one to ask."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def refused(verb: str, done: str, count: int, ask, noun: Tuple[str, str] = ("port", "ports")) -> Optional[int]:
    """Ask before acting on count ports: None on yes, else the exit code, 2
    when there is no one to ask and 1 on a no."""
    answer = ask(f"  {verb.capitalize()} {plural(count, *noun)}? [y/N] ")
    if answer is None:
        print(f"  No terminal to ask: add -y to {verb} without asking.\n", file=sys.stderr)
        return 2
    if not answer:
        print(f"  Nothing {done}.\n")
        return 1
    return None


def kill(rows: List[Listener], ports: List[int], terms: List[str], sure: bool, paint: Paint,
         ask=confirm) -> int:
    """Stop the asked ports at once; a project or stack only after a yes."""
    targets, free = pick_targets(rows, ports, terms)
    lines = [f"  {paint(BOLD, str(port))} is already {paint(FREE, 'free')}." for port in free]
    if terms and not targets:
        print(f"\n  Nothing found for {' '.join(terms)}.\n")
        return 1
    if terms and not sure:
        width = terminal_width()
        print("\n" + "\n".join(layout.render_groups(layout.group_rows(targets), paint, width)) + "\n", flush=True)
        code = refused("stop", "stopped", len(targets), ask)
        if code is not None:
            return code
    results = actions.stop_many(targets) if targets else {}
    port_w = max([len(str(item.port)) for item in targets] + [0])
    for item in targets:
        name = paint(BOLD, str(item.port).rjust(port_w))
        problem = results[item.port]
        if problem:
            lines.append(f"  {name} is still taken: {paint(GONE, problem)}")
        else:
            lines.append(f"  {name} is {paint(FREE, 'free')}.  {paint(DIM, f'{scan.label(item)} stopped')}")
    print("\n" + "\n".join(lines) + "\n")
    return 1 if any(results.values()) else 0


def restart(rows: List[Listener], ports: List[int], terms: List[str], sure: bool, paint: Paint,
            ask=confirm) -> int:
    """Restart the asked ports at once; a project or stack only after a yes.
    The plan for every port is shown first."""
    targets, free = pick_targets(rows, ports, terms)
    lines = [f"  {paint(BOLD, str(port))} is not listening: there is nothing to restart." for port in free]
    if terms and not targets:
        print(f"\n  Nothing found for {' '.join(terms)}.\n")
        return 1
    port_w = max([len(str(item.port)) for item in targets] + [0])
    plan_lines = []
    for item in targets:
        plan = actions.restart_summary(item, rows)
        what = paint(DIM, plan) if plan else paint(GONE, f"not possible: {item.restart_problem}")
        plan_lines.append(f"  {paint(BOLD, str(item.port).rjust(port_w))}  {scan.label(item)}  {what}")
        if item.restart and item.restart.terminal:
            note = (f"ran in terminal {item.restart.terminal}; comes back in the background, "
                    f"see portly logs {item.port}")
            plan_lines.append(f"  {' ' * port_w}  {paint(DIM, note)}")
        if item.restart:
            plan_lines.append(f"  {' ' * port_w}  {paint(DIM, actions.env_note(item.restart))}")
    print("\n" + "\n".join(plan_lines) + "\n", flush=True)
    doable = [item for item in targets if item.container or item.restart]
    if terms and not sure and doable:
        code = refused("restart", "restarted", len(doable), ask)
        if code is not None:
            return code
    if doable and sys.stdout.isatty():
        print(paint(DIM, "  restarting, waiting until everything listens again…"), flush=True)
    results = actions.restart_many(doable) if doable else {}
    for item in doable:
        name = paint(BOLD, str(item.port).rjust(port_w))
        problem = results[item.port]
        if problem:
            lines.append(f"  {name} {paint(GONE, problem)}")
        else:
            lines.append(f"  {name} {paint(FREE, 'listens again')}.  {paint(DIM, scan.label(item))}")
    if lines:
        print("\n".join(lines) + "\n")
    return 1 if any(results.values()) or len(doable) < len(targets) else 0


def bring_up(rows: List[Listener], ports: List[int], terms: List[str], sure: bool, paint: Paint,
             ask=confirm) -> int:
    """Start the asked ports of wt stacks that are down, the way wt started
    them, and an idle worktree's stack when its word (wt3) is asked; a
    project or stack only after a yes. What runs is left alone."""
    targets, free = pick_targets(rows, ports, terms)
    if terms and not targets:
        print(f"\n  Nothing found for {' '.join(terms)}.\n")
        return 1
    lines = [f"  {paint(BOLD, str(port))} is not a port of a wt stack that is down." for port in free]
    lines += [f"  {paint(BOLD, str(item.port))} {scan.label(item)} runs already."
              for item in targets if runs(item) and item.port in ports]
    words = {term.lower() for term in terms}
    down = [item for item in targets if item.is_down or (item.is_idle and wt.stack_word(item) in words)]
    if terms and not down:
        lines.append(f"  Nothing is down in {' '.join(terms)}.")
    name_w = max([len(name_of(item)) for item in down] + [0])
    if down:
        print("\n" + "\n".join(start_plan_lines(down, name_w, paint)) + "\n", flush=True)
    doable = [item for item in down if item.start]
    if terms and not sure and doable:
        noun = ("port", "ports") if any(item.is_down for item in doable) else ("stack", "stacks")
        code = refused("start", "started", len(doable), ask, noun)
        if code is not None:
            return code
    if doable and sys.stdout.isatty():
        print(paint(DIM, "  starting, waiting until it listens…"), flush=True)
    results = actions.start_many(doable) if doable else {}
    for item in doable:
        name, problem = paint(BOLD, name_of(item).rjust(name_w)), results[row_key(item)]
        lines.append(f"  {name} {paint(GONE, problem)}" if problem else
                     f"  {name} {paint(FREE, 'is up' if item.is_idle else 'listens')}.  {paint(DIM, scan.label(item))}")
    print(("" if down else "\n") + "\n".join(lines) + "\n")
    return 1 if free or any(results.values()) or len(doable) < len(down) else 0


def start_plan_lines(down: List[Listener], name_w: int, paint: Paint) -> List[str]:
    """Per down port or idle stack what starts it, or why it cannot start."""
    lines = []
    for item in down:
        what = paint(DIM, actions.start_summary(item)) if item.start else paint(GONE, f"not possible: {item.start_problem}")
        lines.append(f"  {paint(BOLD, name_of(item).rjust(name_w))}  {scan.label(item)}  {what}")
    return lines


def port_number(text: str) -> int:
    if not text.isdigit() or not 0 < int(text) < 65536:
        raise argparse.ArgumentTypeError(f"not a port: {text}")
    return int(text)


def split_targets(words: List[str]) -> Tuple[List[int], List[str]]:
    """Port numbers and filter words."""
    ports, terms = [], []
    for word in words:
        if word.isdigit():
            ports.append(port_number(word))
        else:
            terms.append(word)
    return ports, terms


def show_list(rows: List[Listener], terms: List[str], show_all: bool, paint: Paint) -> int:
    rows = layout.visible(rows, terms, show_all)
    if not rows:
        if terms:
            print(f"\n  Nothing found for {' '.join(terms)}.\n")
            return 1
        print("\n  Nothing is listening." + ("" if show_all else " portly -a shows apps too.") + "\n")
        return 0
    width = terminal_width()
    groups = layout.group_rows(rows)
    print("\n" + "\n".join(layout.render_groups(groups, paint, width)) + "\n")
    if sys.stdout.isatty():
        hint = "  ·  portly 3000 for details  ·  portly for the interactive view"
        print(f"  {layout.to_ansi(layout.summary_segments(groups), paint)}{paint(DIM, hint)}\n")
    return 0


HELP = """\
  portly                   interactive view of your dev servers per project
  portly list              the same list, printed once
  portly shop              only what belongs to shop (project, branch, folder, tool)
  portly 3000              one port in detail
  portly kill 3000 8000    stop what listens there
  portly kill wt1          stop everything of a project or stack (asks first)
  portly restart 3000      stop it and start it again the way it was started (also: restart wt1)
  portly up wt1            start the ports of a wt stack that are down, the way wt started them
                           (an idle worktree's stack: wt up)
  portly forget shop       stop showing shop's idle worktrees, until something runs in it
  portly open 3000/admin   open http://localhost:3000/admin in your browser (https for symfony)
  portly logs 3000         follow its output: docker logs, or the file it writes to
  portly edit 3000         open its checkout in your editor ($PORTLY_EDITOR, else $VISUAL or $EDITOR)
  portly free 3000         the first free port from 3000 on
  portly wait 3000         wait until something listens on 3000 (handy in scripts)
"""


def parser_for(command: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=f"portly {command}".strip(), description=description,
                                   epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)


def targets_of(parser: argparse.ArgumentParser, words: List[str]) -> Tuple[List[int], List[str]]:
    try:
        return split_targets(words)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))


def paint_for_stdout() -> Paint:
    return Paint(sys.stdout.isatty() and "NO_COLOR" not in os.environ)


KILL_FLAG = re.compile(r"-[a-z]*k[a-z]*")  # -k, also in a group such as -ak or -ky


def split_kill_groups(argv: List[str]) -> List[str]:
    """-ky as -k -y: the plain parser does not know kill's -y, so it cannot split that group itself."""
    words = []
    for word in argv:
        words += [f"-{letter}" for letter in word[1:]] if KILL_FLAG.fullmatch(word) else [word]
    return words


def cmd_show(argv: List[str]) -> int:
    parser = parser_for("", "Who listens on which port, per project. In a terminal portly opens "
                            "the interactive view; portly list prints.")
    parser.add_argument("targets", nargs="*", metavar="port|filter",
                        help="a port number shows that port in detail; a word filters by project")
    parser.add_argument("-a", "--all", action="store_true", help="apps and helper processes too")
    parser.add_argument("-k", "--kill", action="store_true", help="the same as portly kill")
    parser.add_argument("--json", action="store_true", help="as JSON, for scripts")
    argv = split_kill_groups(argv)
    args, rest = parser.parse_known_args(argv)
    if args.kill:  # the words this parser does not use, -y too, are portly kill's to check, in their order
        kept = set(args.targets + rest)
        return cmd_kill([word for word in argv if word in kept])
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    ports, terms = targets_of(parser, args.targets)
    rows = wt.with_down_ports(scan.one_per_port(scan.read_listeners(deep=bool(ports))))
    if ports:
        wt.add_start_plans([item for item in rows if item.port in ports])
    else:
        rows = wt.with_idle_worktrees(rows)
    if args.json:
        chosen = [item for item in rows if item.port in ports] if ports else layout.visible(rows, terms, args.all)
        print(json.dumps([layout.as_json(item, deep=bool(ports)) for item in chosen], indent=2, ensure_ascii=False))
        return 0
    if ports:
        return show_ports(ports, rows, paint_for_stdout())
    return show_list(rows, terms, args.all, paint_for_stdout())


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def cmd_start(argv: List[str]) -> int:
    """Plain portly: the live view in a terminal; a port, --json or a pipe prints."""
    printing = {"-h", "--help", "--json", "--kill"}
    killing = any(KILL_FLAG.fullmatch(word) for word in argv)
    if not interactive() or printing & set(argv) or killing or any(word.isdigit() for word in argv):
        return cmd_show(argv)
    return cmd_live(argv)


def cmd_kill(argv: List[str]) -> int:
    parser = parser_for("kill", "Stop what listens on a port, or everything of a project or stack.")
    parser.add_argument("targets", nargs="+", metavar="port|project")
    parser.add_argument("-y", "--yes", action="store_true", help="stop a project without asking")
    args = parser.parse_args(argv)
    ports, terms = targets_of(parser, args.targets)
    return kill(scan.one_per_port(scan.read_listeners()), ports, terms, args.yes, paint_for_stdout())


def cmd_restart(argv: List[str]) -> int:
    parser = parser_for("restart", "Stop what listens on a port and start it again the way it was started.")
    parser.add_argument("targets", nargs="+", metavar="port|project")
    parser.add_argument("-y", "--yes", action="store_true", help="restart a project without asking")
    args = parser.parse_args(argv)
    ports, terms = targets_of(parser, args.targets)
    return restart(scan.one_per_port(scan.read_listeners(deep=True)), ports, terms, args.yes, paint_for_stdout())


def cmd_up(argv: List[str]) -> int:
    parser = parser_for("up", "Start the ports of a wt stack that are down, the way wt started them.")
    parser.add_argument("targets", nargs="+", metavar="port|stack")
    parser.add_argument("-y", "--yes", action="store_true", help="start a stack's ports without asking")
    args = parser.parse_args(argv)
    ports, terms = targets_of(parser, args.targets)
    rows = wt.with_idle_worktrees(wt.with_down_ports(scan.one_per_port(scan.read_listeners())))
    wt.add_start_plans(pick_targets(rows, ports, terms)[0])
    return bring_up(rows, ports, terms, args.yes, paint_for_stdout())


def cmd_forget(argv: List[str]) -> int:
    parser = parser_for("forget", "Stop showing a repo's idle worktrees, until something runs in it again.")
    parser.add_argument("repos", nargs="+", metavar="repo", help="a repo's name, or the folder of its main checkout")
    args = parser.parse_args(argv)
    paint, known, lines, unknown = paint_for_stdout(), wt.read_known_repos(), [], False
    kept = list(known)
    for name in args.repos:
        gone = [common for common in kept if name in (scan.repo_name(common), scan.main_checkout(common))
                or os.path.realpath(name) == scan.main_checkout(common)]
        kept = [common for common in kept if common not in gone]
        unknown = unknown or not gone
        lines += [f"  Forgot {paint(BOLD, scan.repo_name(common))}  {paint(DIM, tilde(scan.main_checkout(common)))}"
                  for common in gone] or [f"  portly does not know {paint(BOLD, name)}."]
    if kept != known:
        wt.write_known_repos(kept)
        lines.append(paint(DIM, "  Its worktrees show again once something runs in it."))
    print("\n" + "\n".join(lines) + "\n")
    return 1 if unknown else 0


def cmd_edit(argv: List[str]) -> int:
    parser = parser_for("edit", "Open the folder of what listens on a port, or of a project, in your editor "
                                "($PORTLY_EDITOR, else $VISUAL or $EDITOR).")
    parser.add_argument("targets", nargs="+", metavar="port|project")
    args = parser.parse_args(argv)
    ports, terms = targets_of(parser, args.targets)
    targets, free = pick_targets(wt.with_idle_worktrees(wt.with_down_ports(scan.one_per_port(scan.read_listeners()))), ports, terms)
    # An idle worktree opens when a word names it (wt3, or its branch), not with every word of its repo.
    words = {term.lower() for term in terms}
    targets = [item for item in targets if not item.is_idle or words & {wt.stack_word(item), item.place.branch.lower()}]
    paint = paint_for_stdout()
    lines = [f"  {paint(BOLD, str(port))} is not listening." for port in free]
    lines += [f"  {paint(BOLD, name_of(item))} {scan.label(item)}: " + (f"its folder is gone ({tilde(actions.folder_of(item))})"
              if actions.folder_of(item) else "no folder to open") for item in targets if not actions.can_edit(item)]
    if terms and not targets:
        lines.append(f"  Nothing found for {' '.join(terms)}.")
    folders = list(dict.fromkeys(actions.folder_of(item) for item in targets if actions.can_edit(item)))
    if lines:
        print("\n" + "\n".join(lines) + "\n")
    if not folders:
        return 1
    command, problem = actions.editor_command(folders)
    problem = problem or actions.run_editor(command)
    if problem:
        print(f"portly: {problem}", file=sys.stderr)
        return 2 if not command else 1
    return 1 if lines else 0


def cmd_open(argv: List[str]) -> int:
    parser = parser_for("open", "Open a port in your browser.")
    parser.add_argument("targets", nargs="+", metavar="port[/path]")
    args = parser.parse_args(argv)
    paint, code = paint_for_stdout(), 0
    rows = {item.port: item for item in scan.one_per_port(scan.read_listeners())}
    print()
    for target in args.targets:
        match = re.fullmatch(r"(\d+)(/.*)?", target)
        if not match:
            parser.error(f"not a port: {target}")
        port = port_number(match.group(1))
        if port not in rows:
            print(f"  {paint(BOLD, str(port))} is not listening.")
            code = 1
            continue
        url = layout.url_of(rows[port])
        if not url:
            print(f"  {paint(BOLD, str(port))} speaks no http.")
            code = 1
            continue
        url += match.group(2) or ""
        problem = actions.open_in_browser(url)
        if problem:
            print(f"  {paint(BOLD, str(port))} {paint(GONE, problem)}: {paint.link(url)}")
            code = 1
            continue
        print(f"  {paint(BOLD, str(port))} opened: {paint.link(url)}")
    print()
    return code


def cmd_logs(argv: List[str]) -> int:
    parser = parser_for("logs", "Follow the output of what listens on a port.")
    parser.add_argument("port", type=port_number, metavar="port")
    parser.add_argument("-n", "--lines", type=int, default=100, help="how many lines back (100)")
    parser.add_argument("--no-follow", action="store_true", help="show the lines and stop")
    args = parser.parse_args(argv)
    paint = paint_for_stdout()
    item = next((row for row in wt.with_down_ports(scan.one_per_port(scan.read_listeners(deep=True)))
                 if row.port == args.port), None)
    if not item:
        print(f"\n  {paint(BOLD, str(args.port))} is not listening.\n")
        return 1
    command = actions.logs_command(item, args.lines, not args.no_follow)
    if not command:
        print(f"\n  {paint(BOLD, str(args.port))} {scan.label(item)} does not write to a file: {layout.output_of(item)}.\n")
        return 1
    stop_hint = "" if args.no_follow else " · ctrl-c stops"
    print(paint(DIM, f"── {args.port} {scan.label(item)}{stop_hint} ──"), flush=True)
    os.execvp(command[0], command)
    return 0


def cmd_free(argv: List[str]) -> int:
    parser = parser_for("free", "Find free ports.")
    parser.add_argument("start", nargs="?", type=port_number, default=3000, metavar="from")
    parser.add_argument("-n", "--count", type=int, default=1, help="how many free ports (1)")
    args = parser.parse_args(argv)
    found = scan.free_ports(args.start, args.count, scan.listening_ports())
    if not sys.stdout.isatty():
        print("\n".join(map(str, found)))
    else:
        paint = paint_for_stdout()
        print("\n" + "\n".join(f"  {paint(BOLD, str(port))} is {paint(FREE, 'free')}." for port in found) + "\n")
    return 0 if len(found) == args.count else 1


def cmd_wait(argv: List[str]) -> int:
    parser = parser_for("wait", "Wait until something listens on a port.")
    parser.add_argument("ports", nargs="+", type=port_number, metavar="port")
    parser.add_argument("-t", "--timeout", type=float, default=60, help="seconds, 0 waits for ever (60)")
    args = parser.parse_args(argv)
    paint, tty = paint_for_stdout(), sys.stdout.isatty()

    def tick(waited: float, waiting: List[int]) -> None:
        frame = SPINNER[int(waited * 4) % len(SPINNER)]
        names = " ".join(map(str, waiting))
        print(f"\r  {paint(ACCENT, frame)} waiting for {paint(BOLD, names)}  {paint(DIM, f'{int(waited)}s')}\033[K",
              end="", flush=True)

    clear = "\r\033[K" if tty else ""
    try:
        ok, waited = scan.wait_for_listen(args.ports, args.timeout, tick if tty else None)
    except KeyboardInterrupt:
        print()
        return 130
    except subprocess.TimeoutExpired:
        print(clear, end="", flush=True)  # main says which tool hung, without the spinner in front
        raise
    names = " ".join(map(str, args.ports))
    if not ok:
        print(f"{clear}  {paint(BOLD, names)} not listening after {int(waited)}s.")
        return 1
    print(f"{clear}  {paint(BOLD, names)} {paint(FREE, 'listening')}  {paint(DIM, f'after {waited:.1f}s')}")
    return 0


def cmd_live(argv: List[str]) -> int:
    parser = parser_for("live", "Interactive: pick a port, see it, open, follow, copy or stop it.")
    parser.add_argument("terms", nargs="*", metavar="filter")
    parser.add_argument("-a", "--all", action="store_true", help="apps and helper processes too")
    args = parser.parse_args(argv)
    if not interactive():
        print("portly live needs a terminal; portly list prints the list.", file=sys.stderr)
        return 2
    os.environ.setdefault("ESCDELAY", "25")  # esc reacts at once instead of after a second
    locale.setlocale(locale.LC_ALL, "")  # curses counts ─ … ● as one cell only under a UTF-8 locale
    scanner = live.Scanner()
    scanner.start()
    curses.wrapper(lambda screen: live.LiveView(screen, scanner, args.terms, args.all).run())
    return 0


COMMANDS = {"list": cmd_show, "ls": cmd_show, "kill": cmd_kill, "restart": cmd_restart, "up": cmd_up,
            "forget": cmd_forget, "edit": cmd_edit, "open": cmd_open, "logs": cmd_logs, "free": cmd_free, "wait": cmd_wait, "live": cmd_live,
            # The Dutch names from its days as poort, so scripts and habits keep working.
            "lijst": cmd_show, "herstart": cmd_restart, "vrij": cmd_free, "wacht": cmd_wait}


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # run() reads a missing tool as no output, so without lsof nothing would seem to listen.
    if not shutil.which("lsof") and not {"-h", "--help"} & set(argv):
        print("portly needs lsof: install it, or add the folder it is in (often /usr/sbin) to PATH.",
              file=sys.stderr)
        return 2
    try:
        if argv and argv[0] in COMMANDS:
            return COMMANDS[argv[0]](argv[1:])
        return cmd_start(argv)
    except subprocess.TimeoutExpired as error:  # what listens is unknown then, never empty
        print(f"portly stopped: {error.cmd[0]} did not answer within {int(error.timeout)}s.", file=sys.stderr)
        return 2
