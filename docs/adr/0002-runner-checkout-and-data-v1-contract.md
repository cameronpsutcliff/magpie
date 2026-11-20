# ADR 0002: Runner Checkout and the `data/v1` Contract

- **Status:** Accepted
- **Date:** 2026-11-20

## Context
Two local checkouts (`~/masterportfolio/magpie` at `27d1e61` and `~/masterportfolio/magpie-runner` at `41db2a0`) share the same GitHub origin, but live consumers (YouTube pipeline, morning brief, scheduled home runs) execute against `magpie-runner`, which carries `pages.py` and `schema.py`.

## Decision
1. **Single Authoritative Checkout:** `magpie-runner` is the live operational checkout; `magpie` must be fast-forwarded or retired so work never diverges across two copies of the same repo.
2. **Consumer Contract:** Readers consume `data/v1/days/<date>.json`, `data/v1/rollups/<date>.json`, and generated page links. Any schema change must be validated by `schema.py` and `test_schema.py` before merge.
3. **CI Gate:** `.github/workflows/edition.yml` (or a dedicated test workflow) must run the unit test suite (`pytest`) on every PR.
