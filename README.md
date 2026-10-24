# portly

A terminal tool that shows which ports are listening on your machine, grouped per project and branch, and lets you open, follow, restart or stop them with a key. Docker containers and git worktrees each land under the project they belong to.

```sh
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/PieterLuypaert/portly/main/portly -o ~/.local/bin/portly
chmod +x ~/.local/bin/portly
```

Needs `python3` and `lsof` (macOS has both); Docker is optional. Run `portly`, or `portly -h` for every command.

portly used to be called poort. Installed it under that name? Run the curl lines again.
