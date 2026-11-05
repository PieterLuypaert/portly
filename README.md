# portly

A terminal tool that shows which ports are listening on your machine, grouped per project and branch, and lets you open, follow, restart or stop them with a key. Docker containers and git worktrees each land under the project they belong to.

```sh
mkdir -p ~/.local/bin
curl -fsSL https://github.com/PieterLuypaert/portly/releases/latest/download/portly -o ~/.local/bin/portly
chmod +x ~/.local/bin/portly
```

Needs `python3` (3.9 or newer) and `lsof` (macOS has both); Docker is optional. Run `portly`, or `portly -h` for every command.

portly used to be called poort. Installed it under that name? Run the curl lines again.

## Working on portly

- **Run it from a checkout** with `./portly`, or link it: `ln -sf "$PWD/portly" ~/.local/bin/portly`. That file only starts the code in `src/portly`.
- **Test** with `python3 -m unittest`. The tests start throwaway servers, and never signal a process they did not start: see the fence at the top of `test_portly.py`.
- **Build the one file** a release ships with `scripts/build-one-file`: it writes `dist/portly`. Pushing a tag such as `v1.2.0` builds it and attaches it to a GitHub release.

### Where the code is

| Module | What it holds |
| --- | --- |
| `base` | Running a tool, text that fits the terminal, colour and time: what every part shares. |
| `model` | The data passed around: `Listener` (what listens on a port), `Place`, `Stack`, `Container`, `Restart`. |
| `scan` | What listens and what runs it: `lsof`, `ps` and `docker ps` read into rows, one per port; restart plans; waiting for ports. |
| `wt` | wt's worktree stacks: the registry, ports that are down and how to start them, idle worktrees, the repos portly knows. |
| `actions` | What portly does: stop, restart and start, open a URL or an editor. The only module that signals a process or changes a container. |
| `layout` | How rows look: groups, columns, details, `--json`, and which rows a filter shows. |
| `live` | The interactive view: curses, its background scanner and its keys. |
| `cli` | The commands as you type them, and `main()`. |

`base` and `model` are imported by name. Every other module is used through its name, such as `actions.stop_many(...)`, so a test that replaces a function reaches every caller of it. For the same reason, every module calls `base.run` and reads `base.CACHE`.
