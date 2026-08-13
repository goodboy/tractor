# Prompt input for NLnet grant draft pass 3

## User prompt (verbatim)

> some notes,
>
> - you missed linking in the multiaddress pr #429 to the WP4
> - you missed linking in the TIPC issue in WP6
> - actually you missed a buncha issue links XD
>
> ok i've committed a new revision, do a pass on it to learn
> what you missed, check the todo bullets and based on those
> try to complete as many of them as you can before the next
> commit (including saving this prompt and your output).

## Model & tool

- **Model:** Claude Opus 4.6 (Anthropic)
- **Tool:** `claude-code` CLI (https://github.com/anthropics/claude-code)
- **Date:** 2026-04-01

## What the user had already added (that the AI missed)

Issue/PR refs the user manually added between passes:
- Milestone 4 (discovery): #429 (multiaddr PR using
  upstream `py-multiaddr`)
- Milestone 5 (encrypted transports): #136, #353, #382
  (TLS/tunnel protocol issues)
- Milestone 6 (high-perf IPC): #339 (`eventfd` + shm),
  #378 (TIPC kernel transport)
- New milestone 7: #379 (sub-interpreter spawning backend)
- WP6: links to pikers.dev WIP patch and `hotbaud` repo
- Ecosystem: AI model users / "skynet" safety angle
- Supervision milestone: link to GH label filter URL
- AI disclosure: `<link-to-gh-branch>` placeholder

## What the AI did in this pass

1. **Proofread and fixed typos/grammar:**
   - "improved a stabilized" -> "a stabilized"
   - "it take sophistication" -> "it takes"
   - "arguably with with" -> double-word removal
   - "encrypted channels types" -> "encrypted channel types"

2. **Budget redistribution for 7 WPs** (was 6):
   - WP1: EUR 11k -> 10k (typed messaging)
   - WP3: EUR 9k -> 8k (supervision)
   - WP5: EUR 8k -> 7k (encrypted transports)
   - WP6: EUR 8k -> 7k (high-perf IPC)
   - WP7: EUR 6k NEW (sub-interpreter spawning, #379)
   - Total: still EUR 50k

3. **Added issue refs to WP5** body text:
   #136, #353, #382 inline + Refs line

4. **Added WP7** for sub-interpreter spawning backend
   with PEP 554/734 references and #379

5. **Extended technical challenges** from 7 to 8:
   - New #7: sub-interpreter isolation boundaries
   - Renumbered API stabilization to #8

6. **Checked char limits**: NLnet form has no enforced
   field-level character limits (confirmed via form HTML)

7. **Updated TODO checklist**: marked off proofread,
   prompt logs, and char-limits items

## Sources consulted

- NLnet form HTML at `https://nlnet.nl/propose/`
  (char limit check)
- Codebase grep for #136, #339, #353, #378, #379,
  #382, #429 issue references
- `tractor/ipc/_ringbuf.py` - eventfd/shm details
- `tractor/discovery/_multiaddr.py` - multiaddr/wg
- PEP 554/734 context for sub-interpreter support
