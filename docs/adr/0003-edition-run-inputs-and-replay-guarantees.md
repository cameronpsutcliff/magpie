# ADR 0003: Edition Run Inputs and Replay Guarantees

- **Status:** Accepted
- **Date:** 2026-11-20

## Context
`magpie/__main__.py` and `magpie/backfill.py` separately sequence collection, follow-up marking, editor cache replay, model selection, and publishing. Individual stage tests cannot prove end-to-end ordering or historical novelty guarantees.

## Decision
1. **Edition Run Seam:** Daily runs and backfills must share one `build_edition(request, inputs) -> candidate + receipt` interface, keeping acquisition and publication at the edges.
2. **Replay & Novelty Guarantee:** Historical/backfill runs may only consult prior sightings strictly earlier than the target edition date so novelty and follow-up flags remain deterministic.
3. **Centralized Editorial Model:** Model invocations across editorial modules will route through one `request_json(prompt, policy, accept)` seam (with Claude, Codex, and future local Studio/Spark endpoints as explicit adapters).
