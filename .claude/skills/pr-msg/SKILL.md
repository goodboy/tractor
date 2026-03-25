---
name: pr-msg
description: >
  Generate patch/pull-request descriptions for
  cross-service git hosting (GitHub, Gitea,
  SourceHut, GitLab, etc.). Use when user wants
  to create or draft a PR/MR/patch description.
argument-hint: "[base-branch (default: main)]"
disable-model-invocation: true
allowed-tools:
  - Bash(git *)
  - Bash(date *)
  - Bash(cp *)
  - Bash(mkdir *)
  - Read
  - Grep
  - Glob
  - Write
---

When generating a patch-request (PR/MR) description,
always follow this process:

0. **Check for branch divergence**: if
   `git log main..HEAD` (or the user-specified base
   branch) is empty, STOP and tell the user "no
   commits diverge from BASE!" with a reminder
   to commit before invoking this skill.

1. **Gather context** from the branch diff and
   commit history:

   - Commit log:
     `git log BASE..HEAD --oneline`
   - Full hashes:
     `git log BASE..HEAD --format='%H %s'`
   - Diffstat:
     `git diff BASE..HEAD --stat`
   - Full diff:
     `git diff BASE..HEAD`
   - Remotes:
     !`git remote -v`
     (to determine commit-link base URL)

2. **Determine the commit-link base URL**:

   Inspect remotes to find the primary hosting
   service and construct the commit URL prefix.
   Preference order:
   - `github` or `origin` remote →
     `https://github.com/<owner>/<repo>/commit/`
   - `gitea` remote → parse the SSH/HTTPS URL
   - `srht` remote →
     `https://git.sr.ht/~<owner>/<repo>/commit/`
   - If multiple remotes exist, default to `github`;
     the user can override via the cross-service
     ref-link stubs.

3. **Analyze the diff**: read through the full diff
   to understand the semantic scope of changes — new
   features, bug fixes, refactors, test coverage,
   etc.

4. **Write the PR description** following these
   rules:

**Line length: 72 char max** for ALL prose content
(Summary bullets, Motivation paragraphs, Scopes
bullets). Only raw URLs in reference-link defs
may exceed this.

**Title:**
- Present tense verb (Add, Fix, Drop, Use, etc.)
- Target 50 chars (hard max 70)
- Backticks around ALL code elements
- Specific about what the branch accomplishes

**Use the accompanying style/format reference:**
- See [format-reference.md](format-reference.md)
  for the canonical PR description structure.

**Body Sections (in order):**

### Metadata comment
- Always include an HTML-comment metadata block at
  the very top of the generated file (before the
  title) with branch info and cross-service
  submission placeholders:
  ```
  <!-- pr-msg-meta
  branch: <branch-name>
  base: <base-branch>
  submitted:
    github: ___
    gitea: ___
    srht: ___
  -->
  ```

### Summary
- Bulleted list of changes, one per logical unit.
- Each bullet prefixed with a commit-hash reference
  link: `[<short-hash>][<short-hash>]` using md
  reference-style.
- End each bullet with a period for prose-y feel.
- Use backticks for all code elements.
- **72 char line limit** — wrap long bullets.
- When a single bullet covers multiple commits,
  chain the hash refs:
  `[abc1234][abc1234] [def5678][def5678]`.

### Motivation
- 1-2 paragraphs explaining *why* the change exists.
- Describe the problem/limitation before the
  solution.
- **72 char line limit** — hard-wrap paragraphs.
- Casual yet technically precise tone (match the
  project's commit-msg style).

### Scopes changed
- Use Python namespace-resolution syntax for scope
  paths: `tractor._exceptions.reg_err_types()` not
  `tractor/_exceptions.py`.
- Each scope bullet prefixed with relevant
  commit-hash reference link(s).
- Sub-bullets (`*`) for secondary changes within
  the same module/namespace.
- For test modules use `tests.<module_name>` style.
- **72 char line limit** on each bullet line.

### Cross-references (commented out)
- Always include a commented-out cross-references
  section as a placeholder for linking the same
  PR/patch across services:
  ```
  <!--
  ### Cross-references
  Also submitted as
  [github-pr][] | [gitea-pr][] | [srht-patch][].

  ### Links
  - [relevant-issue-or-discussion](url)
  - [design-doc-or-screenshot](url)
  -->
  ```

### Reference-style link definitions
- Collect ALL commit-hash links at the bottom of
  the document.
- Format:
  `[<short-hash>]: <base-url><full-or-short-hash>`
- Use the 8-char short hash as both display text
  and ref ID.
- This ensures cross-service md compatibility —
  most services will also auto-link bare SHAs but
  the explicit refs are a guaranteed fallback.

### Cross-service PR ref-link stubs
- After the commit-hash refs, include commented-out
  reference-link stubs for each detected remote's
  PR URL pattern, using `___` as the number
  placeholder:
  ```
  <!-- cross-service pr refs (fill after submit):
  [github-pr]: https://github.com/<owner>/<repo>/pull/___
  [gitea-pr]: https://<host>/<owner>/<repo>/pulls/___
  [srht-patch]: https://git.sr.ht/~<owner>/<repo>/patches/___
  -->
  ```
- Only include stubs for remotes actually detected
  via `git remote -v`.

**Footer:**
```
(this pr content was generated in some part by [`claude-code`][claude-code-gh])
[claude-code-gh]: https://github.com/anthropics/claude-code
```

5. **Write to TWO files**:
   - `.claude/skills/pr-msg/msgs/<timestamp>_<branch>_pr_msg.md`
     * `<timestamp>` from `date -u +%Y%m%dT%H%M%SZ`
     * `<branch>` from `git branch --show-current`
   - `.claude/skills/pr-msg/pr_msg_LATEST.md`
     (overwrite)

6. **Present the raw markdown** to the user in a
   fenced code block (` ```` `) so they can
   copy-paste into any git-service web form.

## Post-submission workflow

After the user submits the PR content to one or
more git services, they (or claude via `/gish`)
can:

1. **Fill metadata**: update the `<!-- pr-msg-meta`
   comment's `submitted:` fields with the assigned
   PR/issue numbers.

2. **Uncomment cross-refs**: uncomment the
   `### Cross-references` section and the
   ref-link stubs, filling in actual PR numbers.

3. **Copy to service subdirs**: for each service
   the PR was submitted to, copy the file:
   ```
   msgs/<service>/<pr-num>_pr_msg.md
   ```
   e.g. `msgs/github/42_pr_msg.md`,
   `msgs/gitea/17_pr_msg.md`.

4. **Cross-link**: each service-specific copy gets
   its own PR number filled in AND links to the
   other services' copies. This enables any copy
   to reference the PR on every other service.

This mirrors the `gish` skill's
`<backend>/<num>.md` local-file pattern — see
`modden/.claude/skills/gish/` for the full
cross-service issue/PR management workflow.

**Abbreviations** (same as commit-msg style):
msg, bg, ctx, impl, mod, obvi, tn, fn, bc, var,
prolly, ep, OW, rn, sig, deps, iface, subproc,
tho, ofc

Keep it terse but detailed. Match the project's
casual-technical tone. Every bullet should carry
signal — no filler.

## TODO: factor into generic + repo-specific layers

Currently this skill lives in-repo and bakes in
`tractor`-specific conventions (Python namespace
scoping syntax, abbreviation vocabulary, tone
guidelines). This should be split similar to how
`/commit-msg` has a generic `SKILL.md` in
`~/.claude/skills/commit-msg/` with a per-repo
`style-guide-reference.md`:

- **Generic layer** (`~/.claude/skills/pr-msg/`):
  the structural template (metadata comment,
  Summary/Motivation/Scopes sections, cross-service
  ref-link strategy, post-submission workflow,
  file-output conventions). Shared across all repos.

- **Repo-specific layer** (`.claude/skills/pr-msg/`
  per repo): a `style-guide-reference.md` containing
  scope-naming conventions (e.g. Python namespace
  syntax vs Go pkg paths), abbreviation glossary,
  tone/slang preferences, and any repo-specific
  section additions.

The generic skill would `See` the local style-guide
the same way `/commit-msg` does, falling back to
sane defaults when no repo-specific guide exists.
