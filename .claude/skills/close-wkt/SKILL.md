---
name: close-wkt
description: >
  Close (teardown) a git worktree opened via
  /open-wkt. Shows a summary of work done and
  optionally removes the worktree + branch.
argument-hint: "[<name>] [--force] [--keep-branch]"
allowed-tools:
  - Bash(git *)
  - Bash(rm *)
  - Bash(ls *)
  - Bash(date *)
  - Bash(cat *)
  - Read
  - Glob
  - Grep
---

Close a worktree previously opened via `/open-wkt`.

## Invocation

```
/close-wkt [<name>] [--force] [--keep-branch]
```

### Parameters

- **`name`** (optional): worktree name. If omitted,
  infer from cwd (must be inside a `.claude/wkts/`
  subtree).

- **`--force`** (optional): remove even if there are
  uncommitted changes. Without this flag, dirty
  worktrees require user confirmation.

- **`--keep-branch`** (optional): don't delete the
  `wkt/<name>` branch after removing the worktree.
  Useful when you want to preserve commits for later
  cherry-pick or rebase.

## Protocol

1. **Locate metadata**: read
   `.claude/wkts/<name>/.wkt_meta.json` for lifecycle
   info (parent branch, notify preference, etc.).

2. **Notify** (if `notify_on_teardown` is true):
   - Show uncommitted changes:
     `git -C .claude/wkts/<name> diff --stat`
   - Show untracked files:
     `git -C .claude/wkts/<name> status --short`
   - Show commits made on this worktree branch:
     `git log <parent_branch>..wkt/<name> --oneline`
   - If there are uncommitted changes and `--force`
     is NOT set, ask the user whether to proceed.

3. **Return to main repo**:
   `cd` to the repo root (NOT the worktree).

4. **Remove active indicator**:
   `rm -f .claude/wkts/<name>/.wkt_active`

5. **Remove the worktree**:
   ```sh
   git worktree remove .claude/wkts/<name>
   # or with --force if requested
   git worktree remove --force .claude/wkts/<name>
   ```

6. **Delete the branch** (unless `--keep-branch`):
   ```sh
   git branch -d wkt/<name>
   # -D if --force was given
   ```

7. **Report**: print a terse summary of what was
   cleaned up (branch deleted? commits preserved?
   changes lost?).

## Edge cases

- **Stale worktree** (directory exists but git
  doesn't track it): run `git worktree prune` first,
  then `rm -rf .claude/wkts/<name>`.

- **No metadata file**: proceed with defaults
  (notify=true, infer parent from git).

- **Cwd IS the worktree**: must `cd` out before
  `git worktree remove` will work.
