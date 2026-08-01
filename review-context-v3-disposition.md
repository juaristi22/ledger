# Disposition — series-catalog v3 (`3cb0c8a`)

Response to the second adversarial review (BLOCK). Core change: UUID authority moves out of the catalog file into **`ledger/series_uuid_registry.jsonl`, an append-only minting ledger** (one JSON line per identity→UUID binding; re-bindings must chain via `supersedes` + `note`). The catalog is now a derived view that embeds the registry digest. The circular-state defect — "the file under check first supplies the prior UUIDs, and the generated result is then compared with that same file" — is gone: the builder inherits from the registry (`scripts/build_series_catalog.py:840-866`), and `--check` verifies catalog↔registry agreement binding-for-binding (`:1063-1083`).

## Finding-by-finding

**1. [CRITICAL] Wholesale remint undetectable by `--check` — fixed, and the committed UUIDs are frozen.**
The registry was bootstrapped from the `2859ecb` catalog's 201 bindings (the prior committed registry state); v3 regeneration preserved **201/201 UUIDs** (identity-key → UUID map verified equal). Your exact repro now fails: swapping the `6ab4fbe` catalog in and running the HEAD checker exits 1 with per-identity `registry agreement` errors (one per reminted UUID). Layers, all exercised by tests:
- catalog row ≠ registry binding → `--check` fails (`registry_agreement_problems`, test `test_check_rejects_uuid_disjoint_catalog`);
- registry edited in a working tree → `--check` fails the git-HEAD byte-prefix check (`:1113-1125`);
- registry edited across commits → the new CI step fails the PR (`.github/workflows/ci.yml:41-63`, `--verify-registry-append-only` against `github.event.pull_request.base.sha`);
- write mode refuses to change or drop any existing identity's UUID absent `--allow-remint --remint-note "..."`; a permitted remint appends a chained supersede line, so every identity change is a reviewable event (`:1245-1263`, tests `test_main_remint_guard_and_ceremony`, `test_main_dropped_identity_requires_allow_remint`).

**2. [HIGH] Alias fallback bypasses geography/entity; vintage missing from key — fixed.**
`ExistingCatalog.match` searches only rows with the same `(geography, entity)` key (`:554-609`); your cross-geography repro now mints fresh (test `test_cross_geography_name_match_never_inherits`), and incremental geography addition no longer trips the global-ambiguity error (test `test_new_geography_added_incrementally`). The documented exception is docket-placeholder enrichment: docket-only row, entity `None`, geography absent or equal on `(level, id)` (tests `test_docket_placeholder_enrichment_keeps_uuid`, `test_docket_placeholder_never_enriches_across_country`). Geography **vintage** joins the identity key (`_geo_key`, `:421-423`) and the registry stores it per binding; same level/id with different vintages now yields two identities, order-independent (test `test_geography_vintage_splits_identity`). All 168 live observations carry `vintage: "current"`, so no live identity moved.

**3. [HIGH] Auto source-concept aliases merge/flip concepts — fixed.**
`measure.source_concept` values are provenance now: recorded per row in a new `source_concepts` field, excluded from `aliases`, excluded from match names (`:817`, `:894`). Your `OFFICIAL_SHARED` repro: rate_b mints fresh, and A+B together stay two rows with no canonical flip (test `test_source_concept_never_drives_inheritance`). Row 104's derived share no longer aliases `fns.snap.total_payment_error_rate` — it cites it as `source_concepts` provenance. The one-time cleanup of the 85 machine aliases was done as reviewed data curation (not version-gated code); the two docket links that genuinely were identity statements — `bls.ces.average_hourly_earnings_private`, `statcan.employment_insurance.regular_beneficiaries` — were re-added as explicit curated aliases. `ambiguous_aliases` is now empty. The three alias-linked live pairs (initial claims, housing starts, industrial production) **deliberately remain separate rows**: with source labels demoted to provenance, no alias relation links them any more; folding each pair is a curation judgment (delete absorbed row + curated alias + `--allow-remint`, per the module docstring recipe) that should be its own reviewed change, not a mechanical side effect of this one.

**4. [HIGH] Logical UUID collisions pass — fixed.**
Canonical lowercase form is required everywhere (`canonical_uuid_problem`, `:447-460`) and uniqueness keys on the parsed 128-bit value (`validate_uuids`, `:1035-1060`; `claim_uuid`, `:824-838`; registry load). Your uppercase/hyphenless/braced variants each produce two findings — non-canonical form and same-128-bit-value duplicate (test `test_uuid_validation_requires_canonical_and_parsed_uniqueness`).

**5. [MEDIUM] Missing seed silently accepted — fixed.**
A missing seed path is a hard error in write and check modes alike (`:1190-1195`, test `test_main_missing_seed_is_a_hard_error`), and the committed-catalog test additionally pins `docket_seed_sha256 is not None`, seed existence, and the 201-row count.

**6. [MEDIUM] Shape-pass strips mismatched/impossible tokens — fixed.**
Stripping is no longer "shape OR derived": a segment strips only when it is a direct spelling of the row's own period or parses to a calendar window **overlapping** that period (`family_pattern` + `_matches_period`, `:337-398`) — this covers all eight live shape-only strips (day-in-month, week-overlapping-month, `after_`-qualified month, month-range covering the period month; fiscal-year tokens must match the fiscal-year period exactly). `2025_12` on a 2026-06 row and impossible tokens like `2026_13` are **kept in the identity and flagged** in `suspect_segments` (tests `test_mismatched_tokens_flagged_not_stripped`, doctests). The false test comment about `june_2026` is gone. Bonus: fixed a latent v2 bug where `MONTHS_ABBREV` held 13 entries, silently shifting derived abbreviations for October–December rows.

## Regenerated artifact (all v2 heals retained)

201 series — 155 observed, 46 docket-only; FNS 54-way split (national row with FY2024+FY2025, 53 state rows); BoE one row, count 2; both M3 rows observed; Eurostat May-final (EA/household) and June-flash (EA21/economy) separate; `suspect_segments: []`; `ambiguous_aliases: []`; `minted=0, superseded=0` on the migration build.

## Validation record

```text
uv run pytest tests/test_build_series_catalog.py -q        63 passed
uv run ruff check script + tests                           clean
python3 -m doctest scripts/build_series_catalog.py         clean
python3 scripts/build_series_catalog.py --check            PASS (201)
identity continuity 2859ecb -> 3cb0c8a                     201/201 UUIDs preserved
HEAD checker vs 6ab4fbe catalog copy                       FAILS (registry agreement, exit 1)
missing seed (write / check)                               FAILS (hard error, both)
uppercase / hyphenless / braced duplicate uuid             FAILS validation
registry line edited                                       FAILS --check + --verify-registry-append-only
catalog uuid edited, no flag                               build REFUSES, nothing written
--allow-remint without --remint-note                       REFUSES
--allow-remint + note                                      writes chained supersede line; --check green after
seed entry dropped, no flag                                build REFUSES
byte idempotence (catalog + registry)                      PASS
```

Registry bootstrap is reproducible: one mint line per `2859ecb` catalog row, in row order (script preserved at `~/thesis-wave-0731/catalog-v3-migration.py`; the agreement check makes the equivalence machine-verifiable).

A third adversarial review is being dispatched against `3cb0c8a`.

---

## Addendum (45fcc10, self-found before the third review)

Rebuilding from a bare registry (committed catalog deleted/empty) loses
curated naming/alias memory; after any future rename curation the renamed
identity would re-key away from its binding and silently fresh-mint,
passing agreement and append-only checks. The builder now refuses a
bare-registry rebuild absent --allow-remint --remint-note
(test_rebuild_without_prior_catalog_is_gated; suite is 64 tests).

---

## Addendum 2 (2c31ae4 — four findings from your predecessor's crashed run, all fixed)

Attempt 1 of the third review crashed before its REPORT after finding:
(1) lossy pipe-joined dimension keys (now JSON-encoded, injective);
(2) partial-catalog deletion silently re-minting (registry now tracks
liveness with retired/revived events; live-binding uuids must appear in
the catalog — both directions checked); (3) silent overlap strips (new
overlap_stripped_segments audit header, currently the 8 live segments);
(4) CI append-only gate PR-only (now also push events via
github.event.before, loud on unfetchable base). Also: the migration
removed 82 machine alias instances (disposition said 85 — prose error).
