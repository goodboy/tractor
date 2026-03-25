---
name: pr-msg
description: >
  Generate patch/pull-request descriptions for cross-service git
  hosting (GitHub, Gitea, SourceHut, GitLab, etc.). Use when user
  wants to create or draft a PR/MR/patch description.
argument-hint: "[base-branch (default: main)]"
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

When generating a patch-request (PR/MR) description, always
follow this process:

0. **Check for branch divergence**: if `git log main..HEAD` (or
   the user-specified base branch) is empty, STOP and tell the
   user "no commits diverge from `<base>`!" with a reminder to
   commit before invoking this skill.

1. **Gather context** from the branch diff and commit history:

   - Commit log: !`git log <base>..HEAD --oneline`
   - Full hashes: !`git log <base>..HEAD --format='%H %s'`
   - Diffstat: !`git diff <base>..HEAD --stat`
   - Full diff: !`git diff <base>..HEAD`
   - Remotes: !`git remote -v` (to determine commit-link base URL)

2. **Determine the commit-link base URL**:

   Inspect remotes to find the primary hosting service and
   construct the commit URL prefix. Preference order:
   - `github` or `origin` remote → `https://github.com/<owner>/<repo>/commit/`
   - `gitea` remote → parse the SSH/HTTPS URL accordingly
   - `srht` remote → `https://git.sr.ht/~<owner>/<repo>/commit/`
   - If multiple remotes exist, default to `github`; the user
     can override via the commented-out links section.

3. **Analyze the diff**: read through the full diff to understand
   the semantic scope of changes — new features, bug fixes,
   refactors, test coverage, etc.

4. **Write the PR description** following these rules:

**Title:**
- Present tense verb (Add, Fix, Drop, Use, etc.)
- Target 50 chars (hard max 70)
- Backticks around ALL code elements
- Specific about what the branch accomplishes

**Use the accompanying style/format reference:**
- See [format-reference.md](format-reference.md) for the
  canonical PR description structure.

**Body Sections (in order):**

### Summary
- Bulleted list of changes, one per logical unit.
- Each bullet prefixed with a commit-hash reference link:
  `[<short-hash>][<short-hash>]` using md reference-style.
- End each bullet with a period for prose-y feel.
- Use backticks for all code elements.
- When a single bullet covers multiple commits, chain the
  hash refs: `[abc1234][abc1234] [def5678][def5678]`.

### Motivation
- 1-2 paragraphs explaining *why* the change exists.
- Describe the problem/limitation before the solution.
- Casual yet technically precise tone (match the project's
  commit-msg style).

### Scopes changed
- Use Python namespace-resolution syntax for scope paths:
  `tractor._exceptions.reg_err_types()` not
  `tractor/_exceptions.py`.
- Each scope bullet prefixed with relevant commit-hash
  reference link(s).
- Sub-bullets (`*`) for secondary changes within the same
  module/namespace.
- For test modules use `tests.<module_name>` style.

### Links (commented out)
- Always include a commented-out Links section as a placeholder
  for external media, issues, related PRs, etc.:
  ```
  <!--
  ### Links
  - [relevant-issue-or-discussion](url)
  - [related-pr-or-patch](url)
  - [design-doc-or-screenshot](url)
  -->
  ```

### Reference-style link definitions
- Collect ALL commit-hash links at the bottom of the document.
- Format: `[<short-hash>]: <base-url><full-or-short-hash>`
- Use the 8-char short hash as both display text and ref ID.
- This ensures cross-service markdown compatibility — most
  services will also auto-link bare SHAs but the explicit
  refs are a guaranteed fallback.

**Footer:**
```
(this pr-msg was generated in some part by [`claude-code`][claude-code-gh])
[claude-code-gh]: https://github.com/anthropics/claude-code
```

5. **Write to TWO files**:
   - `.claude/skills/pr-msg/msgs/<timestamp>_<branch-name>_pr_msg.md`
     * with `<timestamp>` from `date -u +%Y%m%dT%H%M%SZ`
     * and `<branch-name>` from `git branch --show-current`
   - `.claude/skills/pr-msg/msgs/pr_msg_LATEST.md` (overwrite)

6. **Present the raw markdown** to the user in a fenced code
   block (` ``` `) so they can copy-paste into any git-service
   web form.

**Abbreviations** (same as commit-msg style):
msg, bg, ctx, impl, mod, obvi, tn, fn, bc, var, prolly, ep,
OW, rn, sig, deps, iface, subproc, tho, ofc

Keep it terse but detailed. Match the project's casual-technical
tone. Every bullet should carry signal — no filler.
