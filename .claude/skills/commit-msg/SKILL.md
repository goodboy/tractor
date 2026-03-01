---
name: commit-msg
description: Generate git commit messages following project style.
             Use when user wants to create a commit or asks for a commit message.
allowed-tools:
  - Bash
  - Read
  - Write
---

When generating commit messages, always follow this process:

1. **Gather context**: Run, `~/repos/claude_wks/commit_gen.py`
   to get staged changes, recent commits, and style guide rules

2. **Analyze the diff**: Understand what files changed and the nature
   of the changes (new feature, bug fix, refactor, etc.)

3. **Write the commit message** following these rules:

**Use the accompanying style guide:**
- Use the style guide `./style-guide-reference.md` for more details
  on how to write the message based on analysis of past commits in
  this repo.

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

4. **Write to TWO files**:
   - `.claude/<timestamp>_<hash>_commit_msg.md` (timestamp from `date
     --iso-8601=seconds`, hash from `git log -1 --format=%h` first
     7 chars)
   - `.claude/git_commit_msg_LATEST.md` (overwrite)

5. **Always include credit footer**:

When no part of the patch was written by `claude`,
```
(this commit msg was generated in some part by [`claude-code`][claude-code-gh])

[claude-code-gh]: https://github.com/anthropics/claude-code
```

and when some or all of the patch was written by `claude` instead
use,

```
(this patch was generated in some part by [`claude-code`][claude-code-gh])

[claude-code-gh]: https://github.com/anthropics/claude-code
```

Keep it concise. Match the tone of recent commits. For simple
changes, use subject line only.
