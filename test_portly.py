"""Tests for portly. Run with: python3 -m unittest"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("portly", os.path.join(HERE, "portly"))
portly = importlib.util.module_from_spec(importlib.util.spec_from_loader("portly", _loader))
sys.modules["portly"] = portly
_loader.exec_module(portly)


# A safety net: the code under test stops real processes, and a test that
# slips (fixture ports such as 20110 can be live dev servers) must never reach
# one it did not start. Every signal goes through portly.send, which here only
# lets through descendants of this test run; docker changes are refused.
VIOLATIONS = []
_real_send = portly.send
_real_docker = portly.docker


def _started_by_this_run(pid):
    procs = portly.read_processes()
    current = pid
    while current > 1 and current in procs:
        if current == os.getpid():
            return True
        current = procs[current][0]
    return False


def _guarded_send(pid, sig):
    if not _started_by_this_run(pid):
        VIOLATIONS.append(f"signal {sig} to pid {pid}")
        return f"test refused to signal pid {pid}"
    return _real_send(pid, sig)


def _refused_docker(args, timeout):
    VIOLATIONS.append(f"docker {' '.join(args)}")
    raise AssertionError(f"a test tried to run docker {args[0]}")


portly.send = _guarded_send
portly.docker = _refused_docker


def tearDownModule():
    if VIOLATIONS:
        raise AssertionError("tests reached processes they did not start: " + "; ".join(VIOLATIONS))


def hang(test, program):
    """Make every run of program time out, as docker does while its daemon starts."""
    real = subprocess.run

    def fake(cmd, *args, **kwargs):
        if cmd[0] == program:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return real(cmd, *args, **kwargs)

    subprocess.run = fake
    test.addCleanup(setattr, subprocess, "run", real)


def listener(port=3000, pid=1, args="", cwd="", ppid=0, place=None, command="node"):
    return portly.Listener(port, pid, command, ppid=ppid, args=args, cwd=cwd, place=place)


class ParseLsofTest(unittest.TestCase):
    def test_one_listener_per_port_and_pid(self):
        text = "\n".join([
            "p40997", "cnode", "f30", "n[::1]:3000", "f31", "n127.0.0.1:3000",
            "p13826", "csymfony", "f17", "n127.0.0.1:8000",
            "p25043", "ccom.docke", "f121", "n*:20116",
        ])
        found = {(item.port, item.pid, item.command, tuple(item.addresses)) for item in portly.parse_lsof(text)}
        self.assertEqual(found, {(3000, 40997, "node", ("[::1]", "127.0.0.1")),
                                 (8000, 13826, "symfony", ("127.0.0.1",)),
                                 (20116, 25043, "com.docke", ("*",))})

    def test_skips_an_address_without_a_port(self):
        self.assertEqual(portly.parse_lsof("p1\ncnode\nf3\nn*:*"), [])


class LabelTest(unittest.TestCase):
    def test_names_a_node_tool_after_its_package(self):
        cases = {
            "node /x/web/node_modules/.bin/../vite/bin/vite.js --port 20110": "vite",
            "node /x/node_modules/.bin/vite": "vite",
            "node /x/node_modules/@nestjs/cli/bin/nest.js start": "@nestjs/cli",
        }
        for args, name in cases.items():
            self.assertEqual(portly.label(listener(args=args)), name, args)

    def test_names_a_script_after_the_script(self):
        self.assertEqual(portly.label(listener(args="node server.js")), "node server.js")
        self.assertEqual(portly.label(listener(args="node dist/main")), "node dist/main")
        self.assertEqual(portly.label(listener(args="python3 -m http.server 9000")), "python3 http.server")
        self.assertEqual(portly.label(listener(args="/usr/local/bin/node /srv/app/index.js")), "node index.js")
        macos_python = "/Library/Developer/CommandLineTools/Python.app/Contents/MacOS/Python -m http.server"
        self.assertEqual(portly.label(listener(args=macos_python)), "python http.server")

    def test_names_anything_else_after_its_executable(self):
        self.assertEqual(portly.label(listener(args="symfony server:start -d")), "symfony")
        self.assertEqual(portly.label(listener(args="next-server (v16.3.4)")), "next-server")
        self.assertEqual(portly.label(listener(args="php-fpm: master process (/a b/fpm.ini)")), "php-fpm")
        self.assertEqual(portly.label(listener(args="/Applications/Spotify.app/Contents/MacOS/Spotify")), "Spotify")

    def test_falls_back_to_the_lsof_name(self):
        self.assertEqual(portly.label(listener(args="", command="com.docke")), "com.docke")


class DeepInfoTest(unittest.TestCase):
    def test_counts_connections_on_the_server_side(self):
        text = "\n".join(["p1", "f20", "n127.0.0.1:3000->127.0.0.1:52344", "f21", "n127.0.0.1:3000->127.0.0.1:52345",
                          "p2", "f9", "n127.0.0.1:52344->127.0.0.1:3000", "n[::1]:8000->[::1]:61000"])
        self.assertEqual(portly.count_connections(text), {3000: 2, 52344: 1, 8000: 1})

    def test_finds_the_file_a_process_writes_to(self):
        text = "\n".join([
            "p1", "fcwd", "tDIR", "n/x", "f1", "tREG", "n/logs/1-web.log",
            "p2", "f1", "tCHR", "n/dev/null", "f7", "tREG", "n/x/server.log", "f8", "tREG", "n/x/app.log",
            "p3", "f1", "tCHR", "n/dev/ttys012",
        ])
        self.assertEqual(portly.find_logs(text), ({1: "/logs/1-web.log"}, {1: "/logs/1-web.log", 2: "/x/server.log"}))

    def test_shortens_a_parent_command(self):
        self.assertEqual(portly.short_command("node /x/.local/state/fnm/bin/pnpm dev"), "pnpm dev")
        self.assertEqual(portly.short_command("/bin/zsh -l"), "zsh -l")
        self.assertEqual(portly.short_command("node --inspect app.js"), "node --inspect app.js")


class DetailFieldsTest(unittest.TestCase):
    def test_gives_a_url_except_for_databases(self):
        self.assertEqual(portly.url_of(listener(3000, args="node server.js")), "http://localhost:3000")
        self.assertEqual(portly.url_of(listener(5432, args="postgres")), "")
        self.assertEqual(portly.url_of(listener(8000, args="symfony server:start -d")), "https://localhost:8000")
        self.assertEqual(portly.url_of(listener(20111, args="symfony server:start --no-tls")), "http://localhost:20111")

    def test_tells_whether_the_network_can_reach_it(self):
        self.assertTrue(portly.is_local_only(portly.Listener(3000, 1, "node", addresses=["127.0.0.1", "[::1]"])))
        self.assertFalse(portly.is_local_only(portly.Listener(3000, 1, "node", addresses=["*"])))
        self.assertFalse(portly.is_local_only(portly.Listener(3000, 1, "node", addresses=["192.168.1.5"])))

    def test_shows_the_port_inside_a_container(self):
        item = portly.Listener(20116, 9, "com.docke", addresses=["*"], connections=2)
        item.container = portly.parse_docker("\t".join([
            "shop-wt1-firestore-1", "firestore", "0.0.0.0:20116->8080/tcp", "Up 1 hour", "shop-wt1", "firestore", "/x",
        ]))[20116]
        fields = {key: (value, note) for key, value, _, note in portly.detail_fields(item)}
        self.assertEqual(fields["inside"], ("port 8080 in the container", ""))
        self.assertEqual(fields["reach"], ("your network too", "*"))
        self.assertEqual(fields["connections"], ("2 open", ""))

    def test_picks_how_to_show_logs(self):
        item = listener(3000, 1, args="node server.js")
        self.assertIsNone(portly.logs_command(item, 100, True))
        item.log = "/x/web.log"
        self.assertEqual(portly.logs_command(item, 50, True), ["tail", "-n", "50", "-F", "/x/web.log"])
        item.container = portly.Container("db-1", "mysql", "Up", "api", "database", "/x")
        self.assertEqual(portly.logs_command(item, 20, False), ["docker", "logs", "--tail", "20", "db-1"])


class FakeScanner:
    def __init__(self, rows):
        self.rows, self.version, self.refreshed = rows, 1, 0

    def latest(self):
        return self.version, self.rows, "", 0.0

    def refresh(self):
        self.refreshed += 1


class LiveViewTest(unittest.TestCase):
    def setUp(self):
        tree = place(branch="fix/cart", is_worktree=True, top="/t")
        self.rows = [
            listener(3000, 1, args="node x/node_modules/.bin/vite", cwd=HOME_DIR, place=place(sub="apps/web")),
            listener(8000, 2, args="symfony server:start", cwd=HOME_DIR, place=place(sub="apps/api")),
            listener(20110, 3, args="node x/node_modules/.bin/vite", cwd=HOME_DIR, place=tree),
            listener(20111, 4, args="symfony server:start", cwd=HOME_DIR, place=tree),
        ]
        self.scanner = FakeScanner(self.rows)
        self.view = portly.LiveView(None, self.scanner, [], False)
        self.view.pull()

    def press(self, *keys):
        for key in keys:
            self.assertTrue(self.view.handle(key))

    def test_starts_on_the_first_port_and_moves_with_the_arrows(self):
        self.assertEqual(self.view.selected, 3000)
        self.press("down", "down")
        self.assertEqual(self.view.selected, 20110)
        self.press("end")
        self.assertEqual(self.view.selected, 20111)

    def test_keeps_the_cursor_on_its_port_when_ports_come_and_go(self):
        self.press("down")
        self.scanner.rows, self.scanner.version = [self.rows[0]] + self.rows[1:], 2
        self.view.pull()
        self.assertEqual(self.view.selected, 8000)
        self.scanner.rows, self.scanner.version = [self.rows[0], self.rows[2], self.rows[3]], 3
        self.view.pull()
        self.assertEqual(self.view.selected, 20110)

    def test_filters_as_you_type_and_starts_at_the_top(self):
        self.press("/", "f", "i", "x")
        self.assertEqual(self.view.order, [20110, 20111])
        self.assertEqual(self.view.selected, 20110)

    def test_a_filter_keeps_the_cursor_on_a_port_that_still_shows(self):
        self.press("end", "/", "f", "i", "x")
        self.assertEqual(self.view.selected, 20111)
        self.press("esc")
        self.assertEqual((self.view.filter, self.view.editing, len(self.view.order)), ("", False, 4))

    def test_esc_never_quits_but_q_does(self):
        self.press("esc")
        self.assertFalse(self.view.handle("q"))

    def test_asks_before_stopping_one_port_or_its_whole_project(self):
        stopped = []
        self.view.run_in_background = lambda action, targets: stopped.append([item.port for item in targets])
        self.press("k")
        self.assertEqual(self.view.confirm[0], "Stop 3000 vite?")
        self.press("n")
        self.assertEqual((self.view.confirm, stopped, self.view.message), (None, [], "nothing stopped"))
        self.press("end", "K")
        self.assertEqual(self.view.confirm[0], "Stop 2 ports of shop wt fix/cart?")
        self.press("y")
        self.assertEqual(stopped, [[20110, 20111]])

    def test_asks_before_restarting_and_says_when_it_cannot(self):
        done = []
        self.view.run_in_background = lambda action, targets: done.append((action, [item.port for item in targets]))
        self.press("r")
        self.assertIsNone(self.view.confirm)
        self.assertEqual(self.view.message, "3000 cannot restart: no restart plan")
        self.rows[0].restart = portly.Restart(["pnpm", "dev"], "/x/shop/apps/web", [10, 1], "/x/web.log")
        self.press("r")
        self.assertEqual(self.view.confirm[0], "Restart 3000 vite with pnpm dev?")
        self.press("y")
        self.assertEqual(done, [("restart", [3000])])

    def test_a_failed_stop_ends_the_spinner_and_says_why(self):
        def broken(rows):
            raise OSError("lsof went away")

        self.addCleanup(setattr, portly, "stop_many", portly.stop_many)
        portly.stop_many = broken
        self.view.run_in_background("stop", [self.rows[0]])
        deadline = time.monotonic() + 2
        while self.view.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual((self.view.busy, self.view.message), ("", "stop failed: lsof went away"))
        self.assertEqual(self.scanner.refreshed, 1)

    def test_scrolls_to_show_the_header_of_the_selected_group(self):
        lines = portly.group_lines(self.view.groups, 100)
        self.view.selected, self.view.scroll = 20110, 0
        self.view.keep_visible(lines, 3)
        shown = lines[self.view.scroll: self.view.scroll + 3]
        self.assertIn(("fix/cart", None), [(group.branch, row) for row, group, _ in shown if group])
        self.view.selected = 3000
        self.view.keep_visible(lines, 3)
        self.assertEqual(self.view.scroll, 0)


    def test_the_mouse_picks_a_row_and_the_wheel_moves(self):
        self.view.row_at = {7: 20110}
        self.press(("click", 7))
        self.assertEqual(self.view.selected, 20110)
        self.press(("wheel", -1), ("wheel", -1))
        self.assertEqual(self.view.selected, 3000)


class FakeScreen:
    """Hands out queued characters the way curses' get_wch does."""

    def __init__(self, text):
        self.queue = list(text)

    def timeout(self, wait_ms):
        pass

    def get_wch(self):
        if not self.queue:
            raise portly.curses.error("no input")
        return self.queue.pop(0)


class KeyReaderTest(unittest.TestCase):
    def keys(self, text):
        reader, found = portly.KeyReader(FakeScreen(text)), []
        while True:
            key = reader.read()
            if key is None and not reader.screen.queue:
                return found
            found.append(key)

    def test_reads_arrows_in_both_cursor_modes(self):
        self.assertEqual(self.keys("\x1b[A\x1b[B\x1bOA\x1bOB\x1b[1;2B"), ["up", "down", "up", "down", "down"])

    def test_reads_page_and_home_keys(self):
        self.assertEqual(self.keys("\x1b[5~\x1b[6~\x1b[H\x1b[F\x1b[1~\x1b[4~"),
                         ["pgup", "pgdn", "home", "end", "home", "end"])

    def test_reads_sgr_and_x10_mouse(self):
        self.assertEqual(self.keys("\x1b[<0;12;8M\x1b[<0;12;8m\x1b[<64;3;3M\x1b[<65;3;3M"),
                         [("click", 7), None, ("wheel", -1), ("wheel", 1)])
        self.assertEqual(self.keys("\x1b[M" + chr(32) + chr(40) + chr(40)), [("click", 7)])

    def test_reads_plain_keys_enter_backspace_and_a_lone_esc(self):
        self.assertEqual(self.keys("k\r\x7f"), ["k", "enter", "backspace"])
        self.assertEqual(self.keys("\x1b"), ["esc"])


class StartTest(unittest.TestCase):
    def start(self, argv, terminal):
        called = []
        originals = portly.interactive, portly.cmd_live, portly.cmd_show
        portly.interactive = lambda: terminal
        portly.cmd_live = lambda args: called.append(("live", args)) or 0
        portly.cmd_show = lambda args: called.append(("show", args)) or 0
        self.addCleanup(lambda: setattr(portly, "interactive", originals[0]))
        self.addCleanup(lambda: setattr(portly, "cmd_live", originals[1]))
        self.addCleanup(lambda: setattr(portly, "cmd_show", originals[2]))
        portly.main(argv)
        return called[0][0]

    def test_opens_the_live_view_in_a_terminal(self):
        self.assertEqual(self.start([], terminal=True), "live")
        self.assertEqual(self.start(["shop", "-a"], terminal=True), "live")

    def test_prints_a_port_json_a_pipe_and_list(self):
        self.assertEqual(self.start(["3000"], terminal=True), "show")
        self.assertEqual(self.start(["--json"], terminal=True), "show")
        self.assertEqual(self.start([], terminal=False), "show")
        self.assertEqual(portly.COMMANDS["list"].__name__, "cmd_show")

    def test_keeps_the_dutch_command_names(self):
        for dutch, english in {"lijst": "list", "herstart": "restart", "vrij": "free", "wacht": "wait"}.items():
            self.assertIs(portly.COMMANDS[dutch], portly.COMMANDS[english], dutch)


class JsonTest(unittest.TestCase):
    def test_describes_a_worktree_process(self):
        item = listener(20110, 7, args="node server.js", cwd="/t/apps/web",
                        place=place(sub="apps/web", branch="fix/a", is_worktree=True, top="/t"))
        item.stack = portly.Stack("shop", 1, "/t", {"web": 20110})
        item.elapsed = "02:00"
        data = json.loads(json.dumps(portly.as_json(item)))
        self.assertEqual(data["project"], {"repo": "shop", "branch": "fix/a", "worktree": True,
                                           "path": "/t", "folder": "apps/web"})
        self.assertEqual(data["stack"], {"name": "shop", "index": 1, "role": "web"})
        self.assertEqual((data["pid"], data["container"], data["running_seconds"]), (7, None, 120))
        self.assertNotIn("connections", data)


class ShortAgeTest(unittest.TestCase):
    def test_turns_ps_etime_into_one_unit(self):
        cases = {"00:42": "<1m", "12:05": "12m", "03:10:00": "3h", "06-10:49:22": "6d", "  01:00:00": "1h"}
        for etime, age in cases.items():
            self.assertEqual(portly.short_age(etime), age, etime)

    def test_says_when_something_started(self):
        now = portly.datetime(2026, 10, 10, 14, 0)
        self.assertEqual(portly.started_at(3600, now), "today 13:00")
        self.assertEqual(portly.started_at(20 * 3600, now), "yesterday 18:00")
        self.assertEqual(portly.started_at(3 * 86400, now), "Wed 7 Oct 14:00")

    def test_gives_nothing_for_garbage(self):
        self.assertEqual(portly.short_age(""), "")
        self.assertEqual(portly.short_age("soon"), "")


class OnePerPortTest(unittest.TestCase):
    def test_keeps_the_parent_of_workers_on_one_port(self):
        rows = [listener(51443, 61035, ppid=13848), listener(51443, 13848, ppid=13826),
                listener(51443, 65207, ppid=13848)]
        self.assertEqual([item.pid for item in portly.one_per_port(rows)], [13848])

    def test_sorts_by_port(self):
        rows = [listener(8000, 2), listener(3000, 1)]
        self.assertEqual([item.port for item in portly.one_per_port(rows)], [3000, 8000])

    def test_drop_helpers_hides_a_child_of_another_listener(self):
        rows = [listener(8000, 13826, ppid=1), listener(51443, 13848, ppid=13826)]
        self.assertEqual([item.port for item in portly.drop_helpers(rows)], [8000])


class FitTest(unittest.TestCase):
    def test_narrows_the_column_with_the_most_to_spare(self):
        self.assertEqual(portly.fit([10, 40, 30], [6, 12, 10], 60), [10, 26, 24])

    def test_stops_at_the_minimum_widths(self):
        self.assertEqual(portly.fit([10, 40, 30], [6, 12, 10], 5), [6, 12, 10])


def place(repo="shop", sub="", branch="main", is_worktree=False, top="/x/shop"):
    return portly.Place(repo, sub, branch, is_worktree, top, top + "/.git")


class GroupTest(unittest.TestCase):
    def test_groups_by_checkout_with_main_before_its_worktrees(self):
        rows = [
            listener(20110, 4, cwd=HOME_DIR, place=place(sub="apps/web", branch="fix/a", is_worktree=True, top="/t")),
            listener(3001, 3, cwd=HOME_DIR, place=place(repo="blog")),
            listener(3000, 1, cwd=HOME_DIR, place=place(sub="apps/web")),
            listener(8000, 2, cwd=HOME_DIR, place=place(sub="apps/api")),
            listener(3417, 5, cwd="/nowhere/at/all"),
        ]
        groups = portly.group_rows(rows)
        self.assertEqual([(g.title, g.branch, [r.port for r in g.rows]) for g in groups], [
            ("blog", "main", [3001]),
            ("shop", "main", [3000, 8000]),
            ("shop", "fix/a", [20110]),
            ("folder gone", "", [3417]),
        ])

    def test_puts_a_container_without_checkout_under_its_docker_project(self):
        item = listener(5432, 9)
        item.container = portly.Container("db-1", "postgres", "Up 1 hour", "db", "postgres", "")
        self.assertEqual([g.title for g in portly.group_rows([item])], ["docker db"])

    def test_renders_a_header_per_project(self):
        rows = [
            listener(3000, 1, args="node x/node_modules/.bin/vite", cwd=HOME_DIR, place=place(sub="apps/web")),
            listener(20110, 2, args="node x/node_modules/.bin/vite", cwd=HOME_DIR,
                     place=place(sub="apps/web", branch="fix/thing", is_worktree=True, top="/t")),
            listener(3417, 3, args="node server.js", cwd="/nowhere/at/all"),
        ]
        lines = portly.render_groups(portly.group_rows(rows), portly.Paint(False), 120)
        self.assertEqual([line.split() for line in lines], [
            ["shop", "main", "/x/shop"],
            ["3000", "vite", "apps/web", "pid", "1"],
            [],
            ["shop", "wt", "fix/thing", "/t"],
            ["20110", "vite", "apps/web", "pid", "2"],
            [],
            ["folder", "gone"],
            ["3417", "node", "server.js", "/nowhere/at/all", "pid", "3"],
        ])


class FilterTest(unittest.TestCase):
    def rows(self):
        web = listener(3000, 1, args="node x/node_modules/.bin/vite", cwd=HOME_DIR + "/shop/apps/web",
                       place=place(sub="apps/web"))
        tree = listener(20110, 2, args="symfony server:start", cwd=HOME_DIR + "/t",
                        place=place(branch="fix/cart", is_worktree=True, top="/t"))
        tree.stack = portly.Stack("shop", 1, "/t", {"api": 20110})
        app = listener(7768, 3, args="/Applications/Spotify.app/Contents/MacOS/Spotify", cwd="/")
        return [web, tree, app]

    def ports(self, terms, show_all=False):
        return [item.port for item in portly.visible(self.rows(), terms, show_all)]

    def test_matches_project_branch_tool_or_stack(self):
        self.assertEqual(self.ports(["shop"]), [3000, 20110])
        self.assertEqual(self.ports(["fix/cart"]), [20110])
        self.assertEqual(self.ports(["VITE"]), [3000])
        self.assertEqual(self.ports(["wt1"]), [20110])

    def test_any_word_may_match(self):
        self.assertEqual(self.ports(["vite", "symfony"]), [3000, 20110])

    def test_finds_an_app_when_no_dev_server_matches(self):
        self.assertEqual(self.ports([]), [3000, 20110])
        self.assertEqual(self.ports(["spotify"]), [7768])

    def test_splits_ports_from_words(self):
        self.assertEqual(portly.split_targets(["3000", "shop", "8000"]), ([3000, 8000], ["shop"]))
        with self.assertRaises(argparse.ArgumentTypeError):
            portly.split_targets(["0"])


class DockerTest(unittest.TestCase):
    def test_maps_published_tcp_ports_to_their_container(self):
        text = "\n".join([
            "\t".join(["api-database-1", "mysql:8.4", "0.0.0.0:3306->3306/tcp, [::]:3306->3306/tcp",
                       "Up 6 days (healthy)", "api", "database", "/x/shop/apps/api"]),
            "\t".join(["dns", "coredns", "0.0.0.0:53->53/udp, 0.0.0.0:9000-9001->9000-9001/tcp, 8080/tcp",
                       "Up 3 minutes", "", "", ""]),
        ])
        by_port = portly.parse_docker(text)
        self.assertEqual(sorted(by_port), [3306, 9000, 9001])
        self.assertEqual(by_port[3306].service, "database")
        self.assertEqual(by_port[9001].name, "dns")

    def test_skips_lines_it_cannot_read(self):
        self.assertEqual(portly.parse_docker("Cannot connect to the Docker daemon"), {})

    def test_a_hanging_lsof_is_not_taken_for_free_ports(self):
        hang(self, "lsof")
        with self.assertRaises(subprocess.TimeoutExpired):
            portly.listening_ports()

    def test_lists_processes_when_docker_hangs(self):
        hang(self, "docker")
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            ports = {item.port for item in portly.read_listeners()}
        self.assertIn(port, ports)

    def test_turns_the_status_into_an_age(self):
        cases = {"Up 6 days (healthy)": "6d", "Up About an hour": "1h", "Up 25 hours": "1d",
                 "Up 45 minutes": "45m", "Up Less than a second": "<1m", "Up 2 weeks": "14d",
                 "Exited (0) 2 days ago": ""}
        for status, age in cases.items():
            self.assertEqual(portly.docker_age(status), age, status)

    def test_shows_a_container_by_service_project_and_image(self):
        item = listener(3306, 25043, args="/Applications/Docker.app/Contents/MacOS/com.docker.backend")
        item.container = portly.Container("api-database-1", "mysql:8.4", "Up 6 days", "api", "database", "/x")
        self.assertTrue(portly.is_dev(item))
        lines = portly.render_groups(portly.group_rows([item]), portly.Paint(False), 120)
        self.assertEqual(lines[1].split(), ["3306", "database", "docker", "api", "mysql:8.4", "6d"])


class DetailTest(unittest.TestCase):
    def test_shows_where_a_process_runs_from(self):
        item = listener(3000, 40997, ppid=40983, args="node server.js", cwd=HOME_DIR + "/shop",
                        place=place(repo="shop", branch="fix/cart", is_worktree=True))
        item.elapsed = "01:00:00"
        lines = [" ".join(line.split()) for line in portly.render_detail(item, portly.Paint(False))]
        for expected in ["3000 node server.js http://localhost:3000", "pid 40997", "folder ~/shop gone",
                         "project shop · wt fix/cart", "portly open 3000 · portly kill 3000"]:
            self.assertIn(expected, lines)
        self.assertTrue(any(line.startswith("running 1h since") for line in lines))

    def test_says_a_free_port_is_free(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = portly.show_ports([3999], [], paint=portly.Paint(False))
        self.assertEqual((code, out.getvalue().strip()), (0, "3999 is free."))

    def test_rejects_a_number_that_is_no_port(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            portly.port_number("70000")


class StopTest(unittest.TestCase):
    def serve(self, ignore_term):
        """A throwaway listener on a free port; returns the process and port."""
        code = (
            "import signal, socket, sys, time\n"
            f"if {ignore_term}: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
            "print(s.getsockname()[1], flush=True); time.sleep(60)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc, int(proc.stdout.readline())

    def test_stops_a_server_politely(self):
        proc, port = self.serve(ignore_term=False)
        self.assertIsNone(portly.stop(listener(port, proc.pid)))
        self.assertEqual(portly.listening_pids(port), [])

    def test_kills_a_server_that_ignores_sigterm(self):
        proc, port = self.serve(ignore_term=True)
        self.assertIsNone(portly.stop(listener(port, proc.pid)))
        self.assertEqual(proc.wait(timeout=5), -9)

    def test_stops_several_servers_at_once(self):
        servers = [self.serve(ignore_term=False) for _ in range(3)]
        rows = [listener(port, proc.pid) for proc, port in servers]
        self.assertEqual(portly.stop_many(rows), {port: None for _, port in servers})

    def test_names_a_port_it_may_not_free(self):
        proc, port = self.serve(ignore_term=False)
        original = portly.send
        portly.send = lambda pid, sig: f"no permission to stop pid {pid}"
        self.addCleanup(setattr, portly, "send", original)
        self.assertEqual(portly.stop_many([listener(port, proc.pid)]),
                         {port: f"no permission to stop pid {proc.pid}"})

    def test_leaves_docker_itself_alone(self):
        signalled = []
        self.addCleanup(setattr, portly, "send", portly.send)
        portly.send = lambda pid, sig: signalled.append((pid, sig)) or "test refused to signal"
        # What a container's port looks like when docker ps did not answer.
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            item = listener(port, 5975, args=DOCKER_BACKEND, cwd=HOME_DIR, command="com.docker.backend")
            self.assertEqual(portly.stop_many([item]), {port: portly.LEAVE_DOCKER})
        self.assertEqual(signalled, [])

    def test_names_a_docker_that_does_not_answer(self):
        hang(self, "docker")
        self.addCleanup(setattr, portly, "docker", portly.docker)
        portly.docker = _real_docker  # safe: every docker run times out before it starts
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            item = listener(port, os.getpid())
            item.container = portly.Container("portly-test-no-such-container", "mysql", "Up", "", "", "")
            self.assertEqual(portly.stop_many([item]), {port: "docker did not answer within 120s"})


DOCKER_BACKEND = "/Applications/Docker.app/Contents/MacOS/com.docker.backend services"


PROCS = {
    # shop main: zsh -c, pnpm dev, vite
    40982: (1, "/bin/zsh -c source snapshot && pnpm dev"),
    40983: (40982, "node /x/fnm_multishells/1/bin/pnpm dev"),
    40997: (40983, "node /x/web/node_modules/.bin/../vite/bin/vite.js"),
    # blog: zsh, pnpm start, turbo (node script), turbo (binary), pnpm run start, next-server
    97880: (1, "-zsh"),
    97886: (97880, "node /x/bin/pnpm start"),
    97904: (97886, "node /blog/node_modules/.bin/../turbo/bin/turbo run start --ui=stream"),
    97910: (97904, "/blog/node_modules/@turbo/darwin-arm64/bin/turbo run start"),
    97956: (97910, "node /x/bin/pnpm run start"),
    97970: (97956, "next-server (v16.3.4)"),
    # a wt stack: bash -c from wt, pnpm exec vite, vite
    41368: (41367, "bash -c cd apps/web && pnpm exec vite --port 20110 --strictPort"),
    41367: (1, "bash ./wt up fix/x"),
    41377: (41368, "node /x/fnm_multishells/2/bin/pnpm exec vite --port 20110 --strictPort"),
    41418: (41377, "node ./node_modules/.bin/../vite/bin/vite.js --port 20110 --strictPort"),
    # a daemon
    13826: (1, "symfony server:start -d"),
}


class RestartPlanTest(unittest.TestCase):
    def test_finds_the_command_that_started_a_server(self):
        self.assertEqual(portly.launcher_of(40997, PROCS), 40983)
        self.assertEqual(portly.launcher_of(97970, PROCS), 97886)
        self.assertEqual(portly.launcher_of(41418, PROCS), 41377)
        self.assertEqual(portly.launcher_of(13826, PROCS), 13826)

    def test_collects_everything_under_a_launcher(self):
        self.assertEqual(sorted(portly.descendants(97886, PROCS)), [97904, 97910, 97956, 97970])

    def test_reads_every_process_also_one_with_an_argument_that_is_no_utf8(self):
        odd = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", b"caf\xe9"])
        self.addCleanup(odd.wait)
        self.addCleanup(odd.kill)
        self.assertIn(odd.pid, portly.read_processes())

    def test_reruns_a_package_manager_through_path(self):
        original = portly.shutil.which
        portly.shutil.which = lambda name: f"/bin/{name}" if name in ("pnpm", "symfony", "node") else None
        self.addCleanup(setattr, portly.shutil, "which", original)
        self.assertEqual(portly.restart_argv("node /gone/fnm/bin/pnpm exec vite --port 1", "/"),
                         ["pnpm", "exec", "vite", "--port", "1"])
        self.assertEqual(portly.restart_argv("symfony server:start -d", "/"), ["symfony", "server:start", "-d"])
        self.assertIsNone(portly.restart_argv("next-server (v16.3.4)", "/"))
        self.assertEqual(portly.restart_argv("node ./x.js", "/"), ["node", "./x.js"])

    def test_refuses_an_orphan_and_an_app(self):
        orphan = listener(3417, 5, args="node server.js", cwd="/nowhere/at/all")
        self.assertEqual(portly.plan_restart(orphan, {}, {}), (None, "its folder no longer exists"))
        app = listener(7768, 6, args="/Applications/Spotify.app/Contents/MacOS/Spotify", cwd="/")
        self.assertIsNone(portly.plan_restart(app, {}, {})[0])

    def test_refuses_docker_itself(self):
        docker = listener(3306, 5975, args=DOCKER_BACKEND, cwd=HOME_DIR, command="com.docker.backend")
        self.assertEqual(portly.plan_restart(docker, {}, {}), (None, portly.LEAVE_DOCKER))
        self.assertNotIn("portly kill 3306", portly.hints_for(docker))
        self.assertTrue(portly.is_docker_itself(listener(3306, 5975, command="com.docke")))
        self.assertFalse(portly.is_docker_itself(listener(3000, 1, args="node /x/docker-tools/server.js")))

    def test_hands_a_wt_server_its_pid_file(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = os.path.join(folder, "shop-worktrees")
            os.makedirs(registry)
            open(os.path.join(registry, "1-web.pid"), "w").write("41367\n")
            item = listener(20110, 41418, args=PROCS[41418][1], cwd=folder)
            item.stack = portly.Stack("shop", 1, folder, {"web": 20110}, registry)
            item.stdout = os.path.join(registry, "logs", "1-web.log")
            original = portly.shutil.which
            portly.shutil.which = lambda name: f"/bin/{name}"
            self.addCleanup(setattr, portly.shutil, "which", original)
            plan, problem = portly.plan_restart(item, PROCS, {41377: folder})
        self.assertEqual(problem, "")
        self.assertEqual(plan.argv, ["pnpm", "exec", "vite", "--port", "20110", "--strictPort"])
        self.assertEqual((plan.pids, plan.pidfile, plan.log),
                         ([41377, 41418], os.path.join(registry, "1-web.pid"), item.stdout))


SERVER = """import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(); print("listening", flush=True); time.sleep(120)
"""


class RestartTest(unittest.TestCase):
    def test_restarts_a_server_with_its_own_command(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", folder])
        with open(os.path.join(folder, "server.py"), "w") as script:
            script.write(SERVER)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        log = open(os.path.join(folder, "out.log"), "w")
        self.addCleanup(log.close)
        first = subprocess.Popen([sys.executable, "server.py", str(port)], cwd=folder, stdout=log)
        self.addCleanup(first.wait)
        self.addCleanup(first.kill)  # cleanups run last first: kill, then wait
        self.assertTrue(portly.wait_for_listen([port], 5)[0])
        item = next(row for row in portly.one_per_port(portly.read_listeners(deep=True)) if row.port == port)
        self.assertIsNotNone(item.restart, item.restart_problem)
        self.assertEqual(portly.restart_many([item], timeout=10), {port: None})
        new = portly.listening_pids(port)
        self.addCleanup(lambda: [portly.send(pid, portly.signal.SIGKILL) for pid in portly.listening_pids(port)])
        self.assertTrue(new and first.pid not in new)
        with open(os.path.join(folder, "out.log")) as output:
            self.assertIn("── portly restart", output.read())


class FreeAndWaitTest(unittest.TestCase):
    def test_skips_taken_ports(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            busy = sock.getsockname()[1]
            found = portly.free_ports(busy, 2, taken={busy + 1})
        self.assertNotIn(busy, found)
        self.assertNotIn(busy + 1, found)
        self.assertEqual(len(found), 2)

    def test_waits_until_a_port_listens(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = socket.socket()
        self.addCleanup(server.close)
        timer = threading.Timer(0.5, lambda: (server.bind(("127.0.0.1", port)), server.listen()))
        timer.start()
        self.addCleanup(timer.cancel)
        ticks = []
        ok, waited = portly.wait_for_listen([port], 5, tick=lambda waited, waiting: ticks.append(waiting))
        self.assertTrue(ok)
        self.assertGreaterEqual(waited, 0.4)
        self.assertEqual(ticks[0], [port])

    def test_gives_up_after_the_timeout(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertFalse(portly.wait_for_listen([port], 0.3)[0])


class KillTest(unittest.TestCase):
    def rows(self):
        tree = place(branch="fix/cart", is_worktree=True, top="/t")
        return [
            listener(3000, 1, args="node x/node_modules/.bin/vite", cwd=HOME_DIR, place=place()),
            listener(20110, 2, args="node x/node_modules/.bin/vite", cwd=HOME_DIR, place=tree),
            listener(20111, 3, args="symfony server:start", cwd=HOME_DIR, place=tree),
            listener(51443, 4, ppid=3, args="php-fpm: master", cwd=HOME_DIR, place=tree),
        ]

    def test_picks_ports_and_every_dev_server_a_word_matches(self):
        targets, free = portly.pick_targets(self.rows(), [3000, 3999], ["fix/cart"])
        self.assertEqual(([item.port for item in targets], free), ([3000, 20110, 20111], [3999]))

    def kill(self, ports, terms, sure=False, answer=True):
        asked, stopped = [], []
        original = portly.stop_many
        portly.stop_many = lambda rows: stopped.extend(item.port for item in rows) or {
            item.port: None for item in rows}
        self.addCleanup(setattr, portly, "stop_many", original)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = portly.kill(self.rows(), ports, terms, sure, portly.Paint(False),
                              ask=lambda question: asked.append(question) or answer)
        return code, asked, stopped

    def test_stops_asked_ports_without_asking(self):
        self.assertEqual(self.kill([3000], []), (0, [], [3000]))

    def test_asks_before_stopping_a_project(self):
        code, asked, stopped = self.kill([], ["fix/cart"])
        self.assertEqual((code, asked, stopped), (0, ["  Stop 2 ports? [y/N] "], [20110, 20111]))

    def test_stops_nothing_on_no(self):
        self.assertEqual(self.kill([], ["fix/cart"], answer=False), (1, ["  Stop 2 ports? [y/N] "], []))

    def test_needs_yes_without_a_terminal(self):
        self.assertEqual(self.kill([], ["fix/cart"], answer=None)[0], 2)
        self.assertEqual(self.kill([], ["fix/cart"], sure=True), (0, [], [20110, 20111]))

    def test_fails_when_a_word_finds_nothing(self):
        self.assertEqual(self.kill([], ["nope"]), (1, [], []))


def unused_port():
    """A free port with room above it, which portly free needs to find more."""
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port < 65000:
            return port


class CommandTest(unittest.TestCase):
    """Every command the way it is typed, through main() in this process, so the
    signal fence holds. The server runs in a folder of its own, whose name is a
    filter word that finds that server and nothing else."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="portly-cli-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.folder])
        self.word = os.path.basename(self.folder)
        with open(os.path.join(self.folder, "server.py"), "w") as script:
            script.write(SERVER)
        self.addCleanup(setattr, sys, "stdin", sys.stdin)
        sys.stdin = io.StringIO()  # no terminal: a question gets no answer instead of waiting
        self.ran = []
        self.addCleanup(setattr, portly.os, "execvp", portly.os.execvp)
        portly.os.execvp = lambda file, args: self.ran.append(args)  # logs would replace the test run

    def serve(self, log=False):
        """A throwaway server; its output goes to out.log, or nowhere."""
        port = unused_port()
        output = open(os.path.join(self.folder, "out.log"), "w") if log else subprocess.DEVNULL
        if log:
            self.addCleanup(output.close)
        proc = subprocess.Popen([sys.executable, "server.py", str(port)], cwd=self.folder,
                                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertTrue(portly.wait_for_listen([port], 5)[0])
        return proc, port

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = portly.main(list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_list_finds_the_server_by_its_folder(self):
        _, port = self.serve()
        code, out, _ = self.call("list", self.word)
        self.assertEqual(code, 0)
        self.assertIn(str(port), out)

    def test_a_port_shows_its_details_and_a_free_port_says_so(self):
        proc, port = self.serve()
        free = unused_port()
        code, out, _ = self.call(str(port), str(free))
        self.assertEqual(code, 0)
        self.assertIn(str(proc.pid), out)
        self.assertIn(f"{free} is free.", out)

    def test_json_of_a_port_keeps_its_fields(self):
        proc, port = self.serve()
        code, out, _ = self.call("--json", str(port))
        self.assertEqual(code, 0)
        [data] = json.loads(out)
        self.assertEqual((data["port"], data["pid"], data["cwd"]), (port, proc.pid, os.path.realpath(self.folder)))
        self.assertEqual(sorted(data), sorted([
            "port", "name", "url", "addresses", "local_only", "project", "stack", "container", "pid",
            "command", "cwd", "folder_gone", "running_seconds", "memory_kb",
            "log", "parent", "connections", "restart", "restart_problem"]))

    def test_kill_frees_a_port_and_knows_a_free_one(self):
        _, port = self.serve()
        free = unused_port()
        code, out, _ = self.call("kill", str(port), str(free))
        self.assertEqual(code, 0)
        self.assertIn(f"{port} is free.", out)
        self.assertIn(f"{free} is already free.", out)
        self.assertEqual(portly.listening_pids(port), [])

    def test_kill_by_word_needs_yes_without_a_terminal(self):
        _, port = self.serve()
        code, _, err = self.call("kill", self.word)
        self.assertEqual(code, 2)
        self.assertIn("add -y", err)
        self.assertNotEqual(portly.listening_pids(port), [])
        self.assertEqual(self.call("kill", self.word, "-y")[0], 0)
        self.assertEqual(portly.listening_pids(port), [])

    def test_restart_brings_a_port_back_under_a_new_pid(self):
        proc, port = self.serve(log=True)
        self.addCleanup(lambda: [portly.send(pid, portly.signal.SIGKILL) for pid in portly.listening_pids(port)])
        code, out, _ = self.call("restart", str(port))
        self.assertEqual(code, 0, out)
        self.assertIn("listens again", out)
        new = portly.listening_pids(port)
        self.assertTrue(new and proc.pid not in new)

    def test_restart_of_a_free_port_has_nothing_to_do(self):
        free = unused_port()
        self.assertIn(f"{free} is not listening: there is nothing to restart.", self.call("restart", str(free))[1])


HOME_DIR = os.path.expanduser("~")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class PlaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "shop")
        os.makedirs(os.path.join(self.repo, "apps", "web"))
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        self.tree = os.path.join(self.tmp.name, "trees", "fix-cart")
        git("worktree", "add", "-q", "-b", "fix/cart", self.tree, cwd=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_places_the_main_checkout(self):
        found = portly.place_of(os.path.join(self.repo, "apps", "web"))
        repo = os.path.realpath(self.repo)
        self.assertEqual(found, portly.Place("shop", "apps/web", "main", False, repo, repo + "/.git"))

    def test_places_a_worktree_under_its_repo(self):
        found = portly.place_of(self.tree)
        self.assertEqual((found.repo, found.sub, found.branch, found.is_worktree, found.top),
                         ("shop", "", "fix/cart", True, os.path.realpath(self.tree)))

    def write_registry(self, index=1):
        registry = os.path.join(self.repo, ".git", "shop-worktrees")
        os.makedirs(registry, exist_ok=True)
        with open(os.path.join(registry, f"{index}.env"), "w") as entry:
            entry.write(f'REG_BRANCH="fix/cart"\nREG_PATH="{self.tree}"\nREG_WEB=20110\n'
                        'REG_API=20111\nREG_DB="shop_wt1"\nREG_PR="42"\n')

    def test_reads_the_wt_registry(self):
        self.write_registry()
        stacks = portly.read_stacks(os.path.realpath(os.path.join(self.repo, ".git")))
        registry = os.path.realpath(os.path.join(self.repo, ".git", "shop-worktrees"))
        self.assertEqual(stacks, [portly.Stack("shop", 1, self.tree, {"web": 20110, "api": 20111}, registry)])

    def test_reads_the_wt_registry_with_every_port_on_one_line(self):
        registry = os.path.join(self.repo, ".git", "shop-worktrees")
        os.makedirs(registry)
        with open(os.path.join(registry, "1.env"), "w") as entry:
            entry.write(f"REG_BRANCH=fix/cart\nREG_PATH={self.tree}\nREG_PORTS=web=20110 api=20111\n"
                        "REG_DB=shop_wt1\nREG_PR=42\nREG_HERE=1\n")
        stacks = portly.read_stacks(os.path.realpath(os.path.join(self.repo, ".git")))
        self.assertEqual(stacks, [portly.Stack("shop", 1, self.tree, {"web": 20110, "api": 20111},
                                               os.path.realpath(registry))])

    def test_reads_no_port_from_a_wt_field_or_a_number_int_cannot_read(self):
        registry = os.path.join(self.repo, ".git", "shop-worktrees")
        os.makedirs(registry)
        with open(os.path.join(registry, "1.env"), "w") as entry:
            entry.write(f"REG_PATH={self.tree}\nREG_PORTS=\nREG_DB=1\nREG_PR=42\nREG_HERE=1\n")
        with open(os.path.join(registry, "2.env"), "w") as entry:
            entry.write(f"REG_PATH={self.tree}\nREG_PORTS=web=² api=20121\n")
        stacks = portly.read_stacks(os.path.realpath(os.path.join(self.repo, ".git")))
        self.assertEqual([stack.ports for stack in stacks], [{}, {"api": 20121}])

    def test_ties_a_worktree_and_its_wt_containers_to_the_stack(self):
        self.write_registry()
        lookup = portly.Lookup()
        self.assertEqual(lookup.stack_at(lookup.place(self.tree)).index, 1)
        tools = os.path.join(self.repo, "tools")
        os.makedirs(tools)
        container = portly.Container("shop-wt1-gcs-1", "gcs", "Up 1 hour", "shop-wt1", "gcs", tools)
        home, stack = lookup.container_home(container)
        self.assertEqual((home.branch, home.is_worktree, stack.index), ("fix/cart", True, 1))

    def test_ties_another_container_to_its_compose_folder(self):
        os.makedirs(os.path.join(self.repo, "apps", "api"))
        container = portly.Container("api-db-1", "mysql", "Up 1 hour", "api", "db",
                                    os.path.join(self.repo, "apps", "api"))
        home, stack = portly.Lookup().container_home(container)
        self.assertEqual((home.repo, home.sub, home.branch, stack), ("shop", "apps/api", "main", None))

    def test_has_no_place_outside_git(self):
        self.assertIsNone(portly.place_of(self.tmp.name))
        self.assertIsNone(portly.place_of("/"))


if __name__ == "__main__":
    unittest.main()
