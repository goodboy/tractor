---
name: commit-msg
description: >
  Generate git commit messages following project style. Use when user
  wants to create a commit or asks for a commit message.
argument-hint: "[optional-scope-or-description]"
disable-model-invocation: true
allowed-tools:
  - Bash(git *)
  - Bash(gh *)
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

   **Check for regression context**: look for
   `.claude/review_regression.md` (written by the
   `/code-review-changes` skill when a self-caused
   regression was found and fixed). If present, read
   it and incorporate its fields into the commit
   message body (see step 3). Delete the file after
   the message is written — it's single-use context.

   **Check for review context**: look for
   `.claude/review_context.md` (written by the
   `/code-review-changes` skill after applying
   review fixes). If present, read it and extract
   `pr`, `reviewer`, `review_url`, and optionally
   `reply_ids`. These are used in step 3 to add a
   `Review:` trailer. Delete the file after the
   message is written — it's single-use context.
   If the file is absent, this is not a
   review-motivated commit; skip the trailer.

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

**Regression fix messages** (when
`.claude/review_regression.md` exists):

The subject line should describe the actual fix,
not the regression itself. In the body, add a
terse `Regressed-by:` + `Found-via:` block, e.g.:

```
Coerce `addr` to `tuple` in `delete_addr()`

Msgpack deserializes tuples as lists; the
`bidict.inverse.pop()` lookup needs a hashable
key.

Regressed-by: 85457cb (`registry_addrs` change)
Found-via: `/run-tests` test_stale_entry_is_deleted
```

Keep it minimal - one line each for
`Regressed-by` and `Found-via`. The commit hash
and test name(s) are the essential signal.

**Review trailer** (when
`.claude/review_context.md` exists):

Add a `Review:` block at the end of the body,
just before the credit footer. Format:

```
Review: PR #<N> (<reviewer>)
<review_url>
```

Example:

```
Tighten `uid` and `addr` type annotations

Broaden `addr` param to accept `list` from IPC;
narrow `uid` from bare `tuple` to
`tuple[str, str]`.

Review: PR #366 (copilot-pull-request-reviewer)
https://github.com/goodboy/tractor/pull/366#pullrequestreview-4004156812

(this patch was generated in some part by ...)
```

The `Review:` line is terse — PR number +
reviewer login. The URL goes on the next line
for click-through. Keeps the 67-char line limit.

If the context file contains `reply_ids`, hold
off on deleting it — step 6 will PATCH those
GH comments after the user commits, then delete
the file. If no `reply_ids` are present, delete
`.claude/review_context.md` right after the
message is written (single-use, same lifecycle
as `review_regression.md`).

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

6. **PATCH review reply placeholders** (only when
   `reply_ids` was present in `review_context.md`):

   After writing the commit message files, tell the
   user to commit:
   ```
   git commit --edit --file \
     .claude/git_commit_msg_LATEST.md
   ```

   Once the user confirms the commit (or you detect
   a new HEAD via `git log -1 --format=%h` differing
   from the hash seen in step 1), the real commit
   hash is known. For each reply ID:

   - Fetch the current comment body:
     ```
     gh api \
       repos/<owner>/<repo>/pulls/comments/<id> \
       --jq '.body'
     ```
   - Replace `> 📎 commit pending` with:
     ```
     > 📎 fixed in [`<hash>`](<commit-url>)
     ```
     where `<commit-url>` =
     `https://github.com/<repo>/commit/<hash>`
     (derive `<repo>` from the `repo` field in
     `review_context.md`).
   - PATCH the comment:
     ```
     gh api \
       repos/<owner>/<repo>/pulls/comments/<id> \
       -X PATCH -f body="<updated-body>"
     ```

   After all PATCHes succeed, delete
   `.claude/review_context.md`.

   If the user declines to commit now (e.g. wants
   to review further), remind them that the
   `reply_ids` are saved in `.claude/review_context.md`
   and can be PATCHed in a follow-up session.
