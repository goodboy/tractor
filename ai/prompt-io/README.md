# AI Prompt I/O Log

This directory tracks prompt inputs and model outputs for
AI-assisted development across harnesses.

## Policy

Prompt logging follows the [NLNet generative AI policy][nlnet-ai].
All substantive AI contributions are logged with:

- Original harness, and provider/model when known
- Timestamps
- The prompts that produced the output
- Unedited model output (`.raw.md` files)

[nlnet-ai]: https://nlnet.nl/foundation/policies/generativeAI/

## Usage

Entries are created by the `/prompt-io` skill or automatically via
`/commit-msg` integration.

Human contributors remain accountable for all code decisions.
AI-generated content is never presented as human-authored work.
