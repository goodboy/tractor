---
name: commit-msg
description: >
  Generate git commit messages following project style. Use when user
  wants to create a commit or asks for a commit message.
argument-hint: "[optional-scope-or-description]"
disable-model-invocation: true
allowed-tools:
  - Bash(git *)
  - Bash(date *)
  - Bash(cp *)
  - Read
  - Grep
  - Glob
  - Write
---

When generating commit messages, always follow this process:

0. **Detect working context**: run
   `git rev-parse --show-toplevel` to find the repo
   root and `git rev-parse --git-common-dir` to check
   if the cwd is inside a worktree. If the common-dir
   differs from the git-dir, you are in a worktree.
   Tell the user which tree you're operating on
   (e.g. "generating commit msg for worktree
   `remote-exc-registry-tests`").

   Then check for staged changes: if
   `git diff --staged` is empty, STOP and tell the
   user "nothing is staged!" with a reminder to
   `git add` before invoking this skill.

1. **Gather context** from the staged diff and recent
   history:

   - Staged changes: !`git diff --staged --stat`
   - Recent commits: !`git log --oneline -5`

2. **Analyze the diff**: understand what files changed and
   the nature of the changes (new feature, bug fix, refactor,
   etc.)

3. **Write the commit message** following these rules:

**Use the accompanying style guide:**
- See [style-guide-reference.md](style-guide-reference.md)
  for detailed analysis of past commits in this repo.

**Subject Line Format:**
- Present tense verbs: Add, Drop, Fix, Use, Move, Adjust, etc.
- Target 50 chars (hard max: 67)
- Backticks around ALL code elements (classes, functions, modules, vars)
- Specific about what changed

**Body Format (optional - keep simple if warranted):**
- Max 67 char line length
- Use `-` bullets for lists
- Section markers: `Also,` `Deats,` `Other,` `Further,`
- Abbreviations: msg, bg, ctx, impl, mod, obvi, tn, fn, bc, var, prolly
- Casual yet technically precise tone
- Never write lines with only whitespace

**Common Opening Patterns:**
- New features: "Add `feature` to `module`"
- Removals: "Drop `attr` from `class`"
- Bug fixes: "Fix `issue` in `function`"
- Code moves: "Move `thing` to `new_location`"
- Adoption: "Use `new_tool` for `task`"
- Minor tweaks: "Adjust `behavior` in `component`"

4. **Write to TWO files** relative to the repo root
   detected in step 0 (i.e. `git rev-parse
   --show-toplevel`; in a worktree this is the
   *worktree* root, NOT the main repo):
   - `.claude/skills/commit-msg/msgs/<timestamp>_<hash>_commit_msg.md`
     * with `<timestamp>` from `date -u +%Y%m%dT%H%M%SZ`
       or similar filesystem-safe format.
     * and `<hash>` from `git log -1 --format=%h`
       first 7 chars.
     * `mkdir -p` the `msgs/` dir if it doesn't exist.
   - `.claude/git_commit_msg_LATEST.md` (overwrite)

   This ensures
   `git commit --edit --file .claude/git_commit_msg_LATEST.md`
   always works regardless of whether we're in the
   main checkout or a worktree. The `msgs/` backups
   are nice-to-have; if a worktree gets cleaned up
   they're not critical.

5. **Always include credit footer**:

When no part of the patch was written by `claude`,
```
(this commit msg was generated in some part by [`claude-code`][claude-code-gh])
[claude-code-gh]: https://github.com/anthropics/claude-code
```

when some or all of the patch was written by `claude`
instead use,

```
(this patch was generated in some part by [`claude-code`][claude-code-gh])
[claude-code-gh]: https://github.com/anthropics/claude-code
```

Keep it concise. Match the tone of recent commits. For simple
changes, use subject line only.
