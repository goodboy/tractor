# Commit Message Style Guide for `tractor`

Analysis based on 500 recent commits from the `tractor` repository.

## Core Principles

Write commit messages that are technically precise yet casual in
tone. Use abbreviations and informal language while maintaining
clarity about what changed and why.

## Subject Line Format

### Length and Structure
- Target: ~49 chars (avg: 48.7, max: 50 preferred)
- Use backticks around code elements (72.2% of commits)
- Rarely use colons (5.2%), except for file prefixes
- End with '?' for uncertain changes (rare: 0.8%)
- End with '!' for important changes (rare: 2.0%)

### Opening Verbs (Present Tense)

Most common verbs from analysis:
- `Add` (14.4%) - wholly new features/functionality
- `Use` (4.4%) - adopt new approach/tool
- `Drop` (3.6%) - remove code/feature
- `Fix` (2.4%) - bug fixes
- `Move`/`Mv` (3.6%) - relocate code
- `Adjust` (2.0%) - minor tweaks
- `Update` (1.6%) - enhance existing feature
- `Bump` (1.2%) - dependency updates
- `Rename` (1.2%) - identifier changes
- `Set` (1.2%) - configuration changes
- `Handle` (1.0%) - add handling logic
- `Raise` (1.0%) - add error raising
- `Pass` (0.8%) - pass parameters/values
- `Support` (0.8%) - add support for something
- `Hide` (1.4%) - make private/internal
- `Always` (1.4%) - enforce consistent behavior
- `Mk` (1.4%) - make/create (abbreviated)
- `Start` (1.0%) - begin implementation

Other frequent verbs: `More`, `Change`, `Extend`, `Disable`, `Log`,
`Enable`, `Ensure`, `Expose`, `Allow`

### Backtick Usage

Always use backticks for:
- Module names: `trio`, `asyncio`, `msgspec`, `greenback`, `stackscope`
- Class names: `Context`, `Actor`, `Address`, `PldRx`, `SpawnSpec`
- Method names: `.pause_from_sync()`, `._pause()`, `.cancel()`
- Function names: `breakpoint()`, `collapse_eg()`, `open_root_actor()`
- Decorators: `@acm`, `@context`
- Exceptions: `Cancelled`, `TransportClosed`, `MsgTypeError`
- Keywords: `finally`, `None`, `False`
- Variable names: `tn`, `debug_mode`
- Complex expressions: `trio.Cancelled`, `asyncio.Task`

Most backticked terms in tractor:
`trio`, `asyncio`, `Context`, `.pause_from_sync()`, `tn`,
`._pause()`, `breakpoint()`, `collapse_eg()`, `Actor`, `@acm`,
`.cancel()`, `Cancelled`, `open_root_actor()`, `greenback`

### Examples

Good subject lines:
```
Add `uds` to `._multiaddr`, tweak typing
Drop `DebugStatus.shield` attr, add `.req_finished`
Use `stackscope` for all actor-tree rendered "views"
Fix `.to_asyncio` inter-task-cancellation!
Bump `ruff.toml` to target py313
Mv `load_module_from_path()` to new `._code_load` submod
Always use `tuple`-cast for singleton parent addrs
```

## Body Format

### General Structure
- 43.2% of commits have no body (simple changes)
- Use blank line after subject
- Max line length: 67 chars
- Use `-` bullets for lists (28.0% of commits)
- Rarely use `*` bullets (2.4%)

### Section Markers

Use these markers to organize longer commit bodies:
- `Also,` (most common: 26 occurrences)
- `Other,` (13 occurrences)
- `Deats,` (11 occurrences) - for implementation details
- `Further,` (7 occurrences)
- `TODO,` (3 occurrences)
- `Impl details,` (2 occurrences)
- `Notes,` (1 occurrence)

### Common Abbreviations

Use these freely (sorted by frequency):
- `msg` (63) - message
- `bg` (37) - background
- `ctx` (30) - context
- `impl` (27) - implementation
- `mod` (26) - module
- `obvi` (17) - obviously
- `tn` (16) - task name
- `fn` (15) - function
- `vs` (15) - versus
- `bc` (14) - because
- `var` (14) - variable
- `prolly` (9) - probably
- `ep` (6) - entry point
- `OW` (5) - otherwise
- `rn` (4) - right now
- `sig` (4) - signal/signature
- `deps` (3) - dependencies
- `iface` (2) - interface
- `subproc` (2) - subprocess
- `tho` (2) - though
- `ofc` (2) - of course

### Tone and Style

- Casual but technical (use `XD` for humor: 23 times)
- Use `..` for trailing thoughts (108 occurrences)
- Use `Woops,` to acknowledge mistakes (4 subject lines)
- Don't be afraid to show personality while being precise

### Example Bodies

Simple with bullets:
```
Add `multiaddr` and bump up some deps

Since we're planning to use it for (discovery)
addressing, allowing replacement of the hacky (pretend)
attempt in `tractor._multiaddr` Bp

Also pin some deps,
- make us py312+
- use `pdbp` with my frame indexing fix.
- mv to latest `xonsh` for fancy cmd/suggestion injections.

Bump lock file to match obvi!
```

With section markers:
```
Use `stackscope` for all actor-tree rendered "views"

Instead of the (much more) limited and hacky `.devx._code`
impls, move to using the new `.devx._stackscope` API which
wraps the `stackscope` project.

Deats,
- make new `stackscope.extract_stack()` wrapper
- port over frame-descing to `_stackscope.pformat_stack()`
- move `PdbREPL` to use `stackscope` render approach
- update tests for new stack output format

Also,
- tweak log formatting for consistency
- add typing hints throughout
```

## Special Patterns

### WIP Commits
Rare (0.2%) - avoid committing WIP if possible

### Merge Commits
Auto-generated (4.4%), don't worry about style

### File References
- Use `module.py` or `.submodule` style
- Rarely use `file.py:line` references (0 in analysis)

### Links
- GitHub links used sparingly (3 total)
- Prefer code references over external links

## Footer

The default footer should credit `claude` (you) for helping generate
the commit msg content:

```
(this commit msg was generated in some part by [`claude-code`][claude-code-gh])

[claude-code-gh]: https://github.com/anthropics/claude-code
```

Further, if the patch was solely or in part written by `claude`, instead
add:

```
(this patch was generated in some part by [`claude-code`][claude-code-gh])

[claude-code-gh]: https://github.com/anthropics/claude-code
```

## Summary Checklist

Before committing, verify:
- [ ] Subject line uses present tense verb
- [ ] Subject line ~50 chars (hard max 67)
- [ ] Code elements wrapped in backticks
- [ ] Body lines ≤67 chars
- [ ] Abbreviations used where natural
- [ ] Casual yet precise tone
- [ ] Section markers if body >3 paragraphs
- [ ] Technical accuracy maintained

## Analysis Metadata

```
Source: tractor repository
Commits analyzed: 500
Date range: 2019-2025
Analysis date: 2026-02-08
```

---

(this style guide was generated by [`claude-code`][claude-code-gh]
analyzing commit history)

[claude-code-gh]: https://github.com/anthropics/claude-code
