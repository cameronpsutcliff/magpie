# ADR 0001: Edition Publication and Precedence

- **Status:** Accepted (baseline record of current contract & target seam)
- **Date:** 2026-11-20

## Context
Daily edition writes currently happen in `magpie/publish.py`, while eight repair/generator modules (`followups.py`, `constraints.py`, `threats.py`, `possibilities.py`, `read.py`, `viewing.py`, `rising.py`, `views.py`) directly rewrite day files, `latest`, or derived projections. This lets repair paths bypass `pages.py` and `schema.py` validation.

## Decision
1. **Single Publication Seam:** All daily writes and repairs must flow through one **Edition Publication** interface (`publish(candidate)` and `revise(date, change)`), returning a publication receipt.
2. **Precedence:** An enriched (home-tier) edition takes precedence over an unenriched (cloud-tier) edition for the same Chicago date unless `--force` is explicitly passed.
3. **Contract Stability:** Existing `data/v1` file layouts and page IDs are a **one-way door** contract; changing either requires a versioned migration.
4. **Stack Fit:** Keep Magpie core in Python 3.12+ (zero external package dependencies) for editorial/JSON pipeline clarity; keep Git transport in thin launchers.
