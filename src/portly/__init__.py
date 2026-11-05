"""portly shows which TCP ports are listening and which project each belongs to.

A process belongs to the git checkout it runs from, so a worktree is a project
of its own next to the main checkout of its repo. A port that Docker forwards
is named after its container and belongs to the checkout of its compose
project, except a stack that wt started (compose project <name>-wt<n>): that
belongs to its worktree, found in wt's registry, .git/<name>-worktrees/<n>.env.
A port of such a stack that nothing listens on shows as down, with wt's log,
and starts again with what wt_cfg_dev_cmd in the repo's wt.config gives for
it, or with what portly live saw run on it. A worktree of a repo portly saw
something run in shows as idle while nothing runs in it; its stack starts
with wt up. The repos it saw are kept in ~/.cache/portly/repos.

The commands are in cli.HELP (portly -h); README.md says which module holds what.
"""
