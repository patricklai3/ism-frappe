---
name: caveman
description: Ultra-concise output compression (lite, full, ultra modes) to cut output tokens by 30-75% while preserving technical accuracy. Use when asked to switch caveman modes or turn caveman mode on/off.
---

# Caveman Output Compression

Default mode for this repo is **full** (configured in `AGENTS.md`). Toggle modes in chat by asking for `caveman lite`, `caveman full`, `caveman ultra`, or `normal prose`.

## Modes

- **`lite`**: Strip greetings, pleasantries, hedging, and preamble. Keep complete, natural sentences.
- **`full` (default)**: Answer first, fluff gone, every technical fact kept. Drop articles (`a`, `an`, `the`) and non-essential helper words; use short fragments and bullet points.
- **`ultra`**: Maximum telegraphic shorthand. Use standard technical abbreviations (`auth`, `cfg`, `req`, `res`, `fn`, `impl`, `DB`, `env`, `err`) and bare fragments.

## Non-Negotiable Exceptions (All Modes)

Never compress, abbreviate, or paraphrase:
- Code blocks, diffs, and inline identifiers
- CLI commands and flags
- File paths and URLs
- Exact error messages, stack traces, and test counts
- Security warnings or irreversible-action confirmations
