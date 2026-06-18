# Prompt input for NLnet grant draft rework

## User prompt (verbatim)

> ok now i want you to fillout and/or rework a couple sections to
> match the new content,
>
> - abstract: i expanded to 6 goals, try to scan the repo content
>   including all issues and PRs to add links (or issue #s is
>   shorter) for all the milestones mentioned.
>
> - given the above expansion of scope^ adjust the budget section
>   including optional reduced fee-per-hr for sub-collaborator
>   delegation.
>
> - given the expanded abstract extend the "technical challenges"
>   section, again from any info you can glean from the repo's
>   surrounding info base to further expand the hurdle set.

## Model & tool

- **Model:** Claude Opus 4.6 (Anthropic)
- **Tool:** `claude-code` CLI (https://github.com/anthropics/claude-code)
- **Date:** 2026-04-01

## What the AI did

1. Launched an exploration agent to scan the full codebase for
   issue/PR references relevant to each of the 6 milestones:
   - grep'd for `#NNN` patterns across source, tests, docs, and
     `gh/*.md` planning files
   - cross-referenced `docs/README.rst` TODO section, inline
     `XXX`/`TODO` comments, and `pyproject.toml` notes
   - catalogued findings per-milestone

2. **Abstract**: added issue refs to milestones 1, 4, 5, 6:
   - typed messaging: added #410 (`Address` types in msgspec)
   - discovery + addressing: added #216, #367, #410, #424
   - encrypted transports: added #420 (transport-matrix CI)
   - high-perf IPC: added #423 (macOS shm key parity)

3. **Budget**: redistributed EUR 50k across 6 WPs (was 4),
   introduced dual hourly rate (EUR 50/hr maintainer,
   EUR 35/hr vetted contributors), and aligned each WP to
   its corresponding abstract milestone with per-WP issue refs.

4. **Technical challenges**: expanded from 5 to 7 items,
   adding:
   - discovery split-brain / bootstrap problem (#216, #367)
   - cross-platform shared-memory ring buffer safety (#423)
   - tightened existing items with issue-specific detail
     (#410 codec overhead, exception groups in supervision)
   - linked docs (WP2) as a forcing function for API
     stabilization

## Sources consulted (by the AI)

- Full codebase grep for `#[0-9]+` issue references
- `tractor/ipc/_ringbuf.py`, `tractor/ipc/_linux.py` -
  eventfd/shm implementation details
- `tractor/discovery/` subpackage - registrar, multiaddr
- `tractor/msg/types.py`, `tractor/msg/_ops.py` - msg codec
- `gh/*.md` - per-issue/PR planning documents
- `docs/README.rst` - TODO/roadmap section
- `tests/test_ringbuf.py` - shm test coverage
