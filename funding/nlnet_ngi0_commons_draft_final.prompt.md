# Prompt input for NLnet grant final paste-ready pass

## User prompt (verbatim)

> ok for remaining todos, i only really care about the final
> submit, do you think you can make the web request and then
> provide me the final filled-in web form page to click the
> button?
>
> [after AI explained it can't POST forms]
>
> yes to both please. also make sure you fill in any useful
> links for the AI prompt tracking.

## Model & tool

- **Model:** Claude Opus 4.6 (Anthropic)
- **Tool:** `claude-code` CLI (https://github.com/anthropics/claude-code)
- **Date:** 2026-04-01

## What the AI did

1. Read the latest committed draft to pick up all user
   edits (branch link, checked-off TODOs, etc.)

2. Updated AI disclosure section with specific prompt
   file links per revision:
   - `funding/nlnet_ngi0_commons_draft.prompt.md`
   - `funding/nlnet_ngi0_commons_draft_rework.prompt.md`
   - `funding/nlnet_ngi0_commons_draft_pass3.prompt.md`
   - `funding/nlnet_ngi0_commons_draft_final.prompt.md`
   And pointed the repo URL to the `/funding` subdir:
   `https://github.com/goodboy/tractor/tree/ngi0_submission/funding`

3. Generated `funding/nlnet_ngi0_PASTE_READY.txt`:
   a flat plain-text file with one clearly delimited
   section per form field, no markdown, ready to
   copy-paste sequentially into the NLnet web form.
   - Expanded all `#NNN` shorthand issue refs into
     full `https://github.com/goodboy/tractor/issues/NNN`
     URLs in the abstract (form fields are plain text,
     not markdown-rendered).
   - Added `https://github.com/pikers/piker` link in
     the prior-involvement section.
   - Included end-of-file checklist reminder for phone
     number and privacy checkbox.

4. Created this prompt log file for AI disclosure.

## Sources consulted

- `funding/nlnet_ngi0_commons_draft.md` (latest revision)
- `git log --oneline` and `git branch --show-current`
  to confirm branch name `ngi0_submission`
- `funding/*.prompt.md` glob to enumerate existing
  prompt logs
