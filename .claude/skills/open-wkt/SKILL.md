---
name: open-wkt
description: >
  Open (create + enter) a git worktree for isolated,
  ephemeral work. Manages lifecycle from creation
  through teardown with optional fixturization.
argument-hint: "<name> [--fixturize] [--notify-on-teardown]"
allowed-tools:
  - Bash(git *)
  - Bash(ln *)
  - Bash(ls *)
  - Bash(mkdir *)
  - Bash(chmod *)
  - Bash(rm *)
  - Bash(date *)
  - Bash(cat *)
  - Bash(touch *)
  - Read
  - Write
  - Glob
  - Grep
---

Open (or re-enter) a git worktree for isolated work.

## Naming + directory schema

Worktrees live under `.claude/wkts/<name>/` where
`<name>` is a **snake_case semantic label** describing
the *work being done*, NOT necessarily the git branch
name.

Examples:
- `fix_sigint_flaky_timeout`
- `pr366_review_fixes`
- `refactor_pr_msg_line_len`
- `proto_wkt_skill`

The git branch created for the worktree follows the
pattern `wkt/<name>` (e.g. `wkt/fix_sigint_flaky_timeout`).

### Convenience symlink

On first invocation in a repo, ensure a top-level
symlink exists for easy filesystem navigation:

```
<repo-root>/claude_wkts -> .claude/wkts/
```

Create it if missing. This lets the user `ls claude_wkts/`
or `cd claude_wkts/<name>` without remembering the
`.claude/` nesting.

## Invocation

```
/open-wkt <name> [--fixturize] [--notify-on-teardown]
```

### Parameters

- **`name`** (required): snake_case label for the
  worktree. Used as directory name and branch suffix.

- **`--fixturize`** (optional, default: false): when
  set, run `UV_PROJECT_ENVIRONMENT=.venv uv sync`
  inside the worktree to create a local venv so
  tests/tooling work in isolation. The venv dir
  name (`.venv`) avoids collision with the main
  repo's `py313/` env.

- **`--notify-on-teardown`** (optional, default: true):
  when the worktree is closed via `/close-wkt`, print
  a summary of what changed (diffstat, uncommitted
  files) before prompting for removal. When false,
  teardown is silent (useful for fully automated
  subagent work).

## Creation protocol

1. **Validate name**: ensure `<name>` is snake_case,
   no spaces, no leading dots/dashes.

2. **Check for existing**: if `.claude/wkts/<name>/`
   already exists, re-enter it instead of creating
   a new one. Print a notice.

3. **Create the worktree**:
   ```sh
   git worktree add \
     .claude/wkts/<name> \
     -b wkt/<name>
   ```
   This branches from current HEAD.

4. **Ensure convenience symlink**:
   ```sh
   # only if not already present
   ln -sfn .claude/wkts claude_wkts
   ```

5. **Write lifecycle metadata**:
   Create `.claude/wkts/<name>/.wkt_meta.json`:
   ```json
   {
     "name": "<name>",
     "branch": "wkt/<name>",
     "parent_branch": "<current-branch>",
     "created": "<ISO-8601 timestamp>",
     "fixturized": false,
     "notify_on_teardown": true,
     "status": "active"
   }
   ```
   This file is `.gitignore`d (never committed).

6. **Set active-work indicator**:
   Create a lock-style file:
   ```sh
   touch .claude/wkts/<name>/.wkt_active
   ```
   While this file exists, it signals that an agent
   (or human) is actively working in the worktree.
   A human seeing this file knows not to mutate the
   contents. The `/close-wkt` skill removes it.

   For subagent usage, the agent should hold this
   file (create on entry, remove on exit). A stale
   `.wkt_active` older than 1 hour can be considered
   abandoned.

7. **Copy `.claude/settings.local.json`** from the
   main repo into the worktree's `.claude/` dir so
   tool permissions carry over.

8. **Fixturize** (if requested):
   ```sh
   cd .claude/wkts/<name>
   UV_PROJECT_ENVIRONMENT=.venv uv sync
   ```

9. **Switch working directory** to the worktree.

## Re-entry protocol

If `/open-wkt <name>` is called and
`.claude/wkts/<name>/` exists:

1. Verify the git worktree is still valid
   (`git worktree list` includes it).
2. Check `.wkt_active` — if present AND fresh
   (< 1 hour old), warn that another session may
   be working here.
3. Touch `.wkt_active` to refresh timestamp.
4. Switch working directory to the worktree.

## Teardown protocol (`/close-wkt`)

A companion `/close-wkt` skill handles cleanup:

```
/close-wkt [<name>] [--force] [--keep-branch]
```

1. If `<name>` omitted, infer from cwd.
2. If `--notify-on-teardown` (from metadata):
   - Show `git diff --stat` of uncommitted changes.
   - Show `git log <parent_branch>..<wkt_branch>`
     of commits made.
   - Prompt user: "remove worktree? (y/n)"
3. Remove `.wkt_active`.
4. Set `status: "closed"` in `.wkt_meta.json`.
5. `cd` back to the main repo root.
6. `git worktree remove .claude/wkts/<name>`
   (or `--force` if dirty).
7. Unless `--keep-branch`, delete the local branch:
   `git branch -d wkt/<name>`.

## Listing worktrees

`/open-wkt --list` (or `/ls-wkts` alias):

```
NAME                  BRANCH                   STATUS   AGE
fix_sigint_timeout    wkt/fix_sigint_timeout   active   2h
pr366_review          wkt/pr366_review         idle     1d
proto_wkt_skill       wkt/proto_wkt_skill      active   5m
```

Status derived from `.wkt_active` file presence +
freshness.

## Subagent usage

When a `Task` agent needs an isolated worktree:

1. The parent agent calls `/open-wkt <name>` with
   `--notify-on-teardown=false`.
2. Spawns the subagent with cwd set to the worktree.
3. On subagent completion, calls `/close-wkt <name>`.

The `.wkt_active` file prevents concurrent mutation.

## Files managed

```
<repo>/
├── claude_wkts -> .claude/wkts/     # convenience
├── .claude/
│   └── wkts/
│       ├── <name>/                  # the worktree
│       │   ├── .wkt_meta.json       # lifecycle meta
│       │   ├── .wkt_active          # lock indicator
│       │   ├── .claude/
│       │   │   └── settings.local.json
│       │   └── ...                  # repo contents
│       └── <another_name>/
└── .gitignore                       # includes wkts
```

## .gitignore entries

Ensure these patterns exist in the repo's root
`.gitignore` (add if missing):

```
.claude/wkts/
claude_wkts
```

## TODO: factor upstream

This skill is prototyped in-repo (`tractor`). Once
stable, factor the generic worktree lifecycle logic
into `~/.claude/skills/open-wkt/` (user-global) and
leave only repo-specific fixturization details
(venv tooling, test runner config) in the per-repo
layer. The `gish` project (or whatever it becomes)
is the natural home for the cross-repo version.
