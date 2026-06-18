# Prompt input for NLnet grant draft generation

## User prompt (verbatim)

> read the grant submission form here,
>
> https://nlnet.nl/propose/
>
> see if you can take each field and generate a first-draft set of
> responses for `tractor`. ensure you write up a plan in md which
> quotes each form field's header and then produce a submission
> (matching the respective character limits if you can detect them)
> for each field. write it all up for me to review and refine in a
> single md document under a new ./funding/ subdir.

## Model & tool

- **Model:** Claude Opus 4.6 (Anthropic)
- **Tool:** `claude-code` CLI (https://github.com/anthropics/claude-code)
- **Date:** 2026-03-31

## What the AI did

1. Fetched and parsed the NLnet proposal form at
   `https://nlnet.nl/propose/` to extract all field headers,
   descriptions, and guidance text.
2. Fetched the NGI Zero Commons Fund page at
   `https://nlnet.nl/commonsfund/` and the themes overview at
   `https://nlnet.nl/themes/` to identify the best-fit funding call.
3. Read the project's `docs/README.rst`, `pyproject.toml`, and
   explored the codebase structure, git history (~4,700 commits),
   issue tracker references, and test suite to gather context.
4. Generated a first-draft response for every form field, structured
   as quoted field headers followed by draft content.
5. Wrote the output to `funding/nlnet_ngi0_commons_draft.md`.

## Sources consulted (by the AI)

- `https://nlnet.nl/propose/` - submission form fields
- `https://nlnet.nl/commonsfund/` - fund details, budget range,
  deadline
- `https://nlnet.nl/themes/` - available funding calls
- `docs/README.rst` - project description, features, roadmap
- `pyproject.toml` - metadata, dependencies, license
- Git log and codebase exploration - commit counts, contributors,
  architecture
