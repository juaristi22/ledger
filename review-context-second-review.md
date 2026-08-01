# Focused re-review — series-catalog v2

**Reviewed range:** `9b4329e..HEAD` (`6ab4fbe`, `2859ecb`)  
**Verdict:** **BLOCK**  
**Risk:** **CRITICAL** — the committed artifact has discarded every previously minted UUID, the checker accepts multiple UUID-disjoint catalogs for the same inputs, and the new alias fallback can move or merge identities across the dimensions that are supposed to define them.

The seed, current period spellings, FNS geography split, BoE merge, M3 normalization, observed-row unit/cadence rejection, and ordinary stale-catalog CI check all improved. Those passes do not offset the identity failures below.

## Ranked findings

### 1. [CRITICAL] The commits wholesale remint the registry, and `--check` cannot detect the remint

The catalog promises that a UUID is “minted once and never re-minted” (`ledger/series_catalog.json:2`; also `scripts/build_series_catalog.py:5-7,41-44`). The committed history does the opposite:

- From `9b4329e` to `6ab4fbe`, the builder's own identity projection `(concept, geography level/id, entity name/role)` has 116 common identities; **0 of 116 UUIDs survive**. The total UUID-value intersection is **0 of 141**.
- From `6ab4fbe` to `2859ecb`, all 201 identity keys are unchanged and **0 of 201 UUIDs survive**. The catalog diff consists only of 201 removed UUID lines and 201 added UUID lines; observations hash, seed hash, row counts, concepts, and metadata are unchanged.
- Example: unchanged ABS building approvals is `0a67f2eb-…` at `9b4329e:ledger/series_catalog.json:8`, `e344ff46-…` at `6ab4fbe:ledger/series_catalog.json:18`, and `efbb2901-…` now (`ledger/series_catalog.json:18`). The unchanged docket-only `abs.labour.unemployment_rate` similarly moves from `843e5bab-…` (`9b4329e:ledger/series_catalog.json:168-183`) to `443cb1bf-…` (`ledger/series_catalog.json:183-198`).
- The merged BoE row now uses `8fb890ac-…` (`ledger/series_catalog.json:1619-1650`), preserving neither prior BoE identity. M3 orders now uses `294d82e8-…` (`ledger/series_catalog.json:1805-1835`), preserving neither the prior docket nor observed UUID.

This is not forced by the final algorithm. Running the HEAD builder against a temporary copy of the `6ab4fbe` catalog reports `catalog current: 201 series` and exits 0; running it against HEAD also exits 0. Thus two completely UUID-disjoint 201-row catalogs are accepted for the same observation and seed bytes.

The reason is circular state: the file under check first supplies the prior UUIDs (`scripts/build_series_catalog.py:555-557`, reused at `:387-390`), and the generated result is then compared with that same file (`:565-577`). Syntax/version validation cannot establish historical continuity. This directly fails disposition 3 and means the new CI step would not have caught the actual 201-ID remint in `2859ecb`.

### 2. [HIGH] Alias fallback bypasses geography/entity identity and omits geography vintage

An exact identity match uses the full derived key (`scripts/build_series_catalog.py:290-294`), but the fallback searches global concept and alias indexes without filtering candidates to the incoming geography/entity (`:295-300`). The matched prior UUID is then applied to the incoming geography/entity (`:329-332,387-390`).

Adversarial results:

- With one prior US row and a later California-only row of the same name, the California row silently inherits the US UUID: the UUID moves to a different geography.
- If both old US and new California rows are present, `claim_uuid` eventually errors because both claim the same raw UUID. That prevents corruption but also prevents an ordinary new geography.
- Once a concept has several prior geographies (for example the 54 FNS rows), adding a new geography fails earlier as an ambiguous global name match.

The fallback is useful for enriching a docket-only placeholder whose entity is not yet known, but it is too broad for already observed identities. There is no regression test for geography movement or incremental geography addition; `tests/test_build_series_catalog.py:119-130` only builds two geographies from an empty catalog.

The key is also not actually the full geography object claimed by the disposition. `_geo_key` includes only `level|id` (`scripts/build_series_catalog.py:193-195`), while the observation object also carries `vintage`. Two synthetic rows with the same level/id and vintages `v1` and `v2` merged into one bucket; reversing input order changed which vintage was emitted. The first geography object is retained at `scripts/build_series_catalog.py:230,344,404`. This conflicts with the ADR requirement that relevant boundary vintage participate in identity (`docs/adr-arch-fact-identity-v2.md:175,350-351`). Disposition 4 therefore passes for current IDs but not for the claimed identity mechanics.

### 3. [HIGH] Automatically generated aliases can merge distinct concepts and flip the canonical concept without curation

The fallback treats aliases as identity-authoritative, but the catalog does not distinguish curated aliases from mechanically copied `measure.source_concept` values:

- `source_concepts` participate in matching (`scripts/build_series_catalog.py:329`).
- A unique global name hit is accepted (`:295-300`).
- The prior concept becomes canonical (`:330-332`), and buckets landing on that key merge (`:333-368`).
- Raw concepts, source concepts, and curated aliases are all unioned into the same alias list (`:391-395`).
- `claim_uuid` runs only after this merge (`:373-390`), so it sees one bucket and cannot report that another concept was absorbed.

Reproduction without any hand edit:

1. Build `agency.rate_a` with source concept `OFFICIAL_SHARED`; the generator automatically records `OFFICIAL_SHARED` as an alias.
2. On the next build, supply only genuinely different `agency.rate_b` with that same source concept. The output silently remains canonical `agency.rate_a` and inherits its UUID.
3. Supply A and B together. They silently become one A row with `observation_count: 2`.

So the answers to both adversarial questions are **yes**: a bad unique alias can merge distinct identities, and canonicalization can flip a concept without curation. This contradicts the manual-curation claim at `scripts/build_series_catalog.py:25-33` and `ledger/series_catalog.json:2`. The current tests cover a manually inserted alias with one incoming bucket (`tests/test_build_series_catalog.py:145-155`), not an automatically derived alias, two-bucket merge, or uncurated concept flip.

The committed data demonstrate that `source_concept` is not necessarily a synonym. Raw row 104 is the derived concept `fns.snap.share_jurisdictions_at_or_above_6pct` but declares `fns.snap.total_payment_error_rate` as its source concept (`ledger/official_observations.jsonl:104`). The catalog emits the base measure as an alias on the derived share (`ledger/series_catalog.json:2813-2836`). That name is also canonical for 54 different FNS rows, yet it is absent from the six-item `ambiguous_aliases` header (`ledger/series_catalog.json:8-14`) because ambiguity counts alias occurrences only, not alias-versus-canonical collisions (`scripts/build_series_catalog.py:481-484`).

Alias healing is also incomplete in the opposite direction. Exact identity wins before aliases are considered (`scripts/build_series_catalog.py:292-294`), so two prior rows remain separate even when one explicitly aliases the other's canonical concept. Three live same-geography/entity pairs do this:

- initial claims: `ledger/series_catalog.json:2181-2210` versus `:5553-5584`;
- housing starts: `:1739-1769` versus `:5487-5518` (raw row 31 calls this a duplicate Thesis target ID);
- industrial production: `:2648-2678` versus `:5618-5649` (raw row 27 calls this a duplicate Thesis target ID).

This is why the initial-claims “heal” is only partial, not complete.

### 4. [HIGH] Logical UUID collisions pass `claim_uuid`, validation, and CI

Both collision mechanisms key on the UUID's raw JSON string (`scripts/build_series_catalog.py:373-381,518-531`). `uuid.UUID` accepts uppercase, hyphenless, and braced representations, but the uniqueness map never keys on the parsed 128-bit value and never requires canonical `str(parsed)` spelling.

In temporary copies, row 2 was assigned an uppercase, hyphenless, and then braced spelling of row 1's UUID. Each variant represented the same UUID after parsing; bare `--check` nevertheless printed `catalog current: 201 series` and exited 0. `tests/test_build_series_catalog.py:198-208` covers only byte-identical duplicate strings.

The current 201 committed values are canonical lowercase UUIDv4 strings and unique by parsed value, so this is a verifier/collision-surface defect rather than a current catalog collision.

### 5. [MEDIUM] The seed digest is load-bearing, but the default seed is not required to exist

Positive result: the tracked seed hashes to `930424fb48c0be4c9e2ce17d4e0f2a6be886408e814c80324174a7a303fa0271`, exactly matching `ledger/series_catalog.json:6`. A one-byte seed change makes `--check` fail. The digest is therefore genuinely load-bearing (`scripts/build_series_catalog.py:414-417,500-504,565-576`).

Residual failure: a missing path is silently treated as “no docket” (`scripts/build_series_catalog.py:414-417`), with a null digest (`:502-504`). In a temporary checkout with the seed absent, bare regeneration succeeded and wrote 155 observed/0 docket-only rows; the subsequent bare `--check` passed against that reduced catalog. The committed-catalog test does not assert seed existence, a non-null seed digest, or 201 rows (`tests/test_build_series_catalog.py:211-220`). Thus deleting/losing the seed and committing the regenerated reduced artifact reopens the original omission path while CI remains green.

### 6. [MEDIUM] Period coverage is fixed for current data, but “only period tokens” is still not enforced

All previously missed live spellings now normalize and the committed `suspect_segments` list is empty (`ledger/series_catalog.json:7`). BoE and M3 demonstrate useful fixes. However, stripping remains `shape OR derived` (`scripts/build_series_catalog.py:173-177`), and the shape grammar is independent of `row.period` (`:94-105`). It still strips impossible or mismatched strings such as `2026_13` or `2025_12` on a row whose period is 2026-06, with no suspect signal because suspect scanning only sees surviving segments (`:185-190`). A legitimate table, statute, cohort, or edition segment equal to a recognized period spelling is therefore silently removed.

The claimed semantic safety pass does not mitigate current normal forms: every token derived for normal fiscal-year/month/quarter/week values is already accepted by the shape grammar. In particular, the test comment saying the grammar does not know `june_2026` (`tests/test_build_series_catalog.py:63-69`) is false; the month regex already recognizes it (`scripts/build_series_catalog.py:99`).

This is an acceptable residual only if date-shaped dotted segments are explicitly reserved for periods by contract. Under the present categorical “period tokens (and nothing else)” statement (`scripts/build_series_catalog.py:11-17`), it is not acceptable: a mismatch should at least fail/flag, or the format needs an escape/curation mechanism.

## Catalog audit

The committed artifact contains 201 rows: 155 observed and 46 docket-only. All 168 observations are accounted for in observed-row counts. The 75 seed entries yield 46 docket-only rows; 29 match observed names. All 46 direct seed rows match their declared cadence, target unit, and country mapping; entries without a country retain null geography. Both input digests are exact, and all current UUIDs are parseable canonical UUIDv4 values unique by parsed value.

### Twelve-row raw-data spot-check

| Catalog identity | Raw/seed evidence | Result |
|---|---|---|
| BoE Bank Rate (`ledger/series_catalog.json:1619-1650`) | raw `ledger/official_observations.jsonl:39-40` | Correct GB/government/bank-rate identity; both spellings merge, count 2. |
| Census M3 orders (`ledger/series_catalog.json:1805-1835`) | raw `:157`; seed `ledger/seeds/thesis_docket_series.json:546-568` | Correct US/economy identity; dated concept and docket seed heal to one observed row. |
| Census M3 shipments (`ledger/series_catalog.json:1838-1868`) | raw `:158`; seed `:571-592` | Correct US/economy identity; one observed row. |
| FNS national (`ledger/series_catalog.json:2845-2873`) | raw `ledger/official_observations.jsonl:6,103` | Correct US-country/household identity; FY2024+FY2025, count 2. |
| FNS California (`ledger/series_catalog.json:2995-3024`) | raw `:54` | Exact state ID/name and household entity. |
| FNS District of Columbia (`ledger/series_catalog.json:3115-3144`) | raw `:58` | Exact state-level DC ID/name and household entity. |
| FNS Guam (`ledger/series_catalog.json:4405-4434`) | raw `:61` | Faithfully retains the raw state-level Guam ID/entity. |
| Eurostat May final (`ledger/series_catalog.json:2312-2341`) | raw `:33` | Correct EA/household/HICP-all-items identity. |
| Eurostat June flash (`ledger/series_catalog.json:2344-2374`) | raw `:134` | Correct EA21/economy identity; no longer collapsed with May final. |
| Old DOL initial claims (`ledger/series_catalog.json:2181-2210`) | raw `:18` | Faithful US/ui_initial_claimant/month metadata. |
| Standard weekly initial claims (`ledger/series_catalog.json:5520-5551`) | raw `:105-106,144,148,162` | Correct US/ui_claimant/week-ending identity, count 5. |
| Second old initial-claims spelling (`ledger/series_catalog.json:5552-5584`) | raw `:44` | Period token strips, but row remains separate despite aliasing the first old-DOL concept. |

FNS now splits correctly: 55 observations become 54 geographic identities — one national identity with two periods plus 53 state-level jurisdiction identities. The raw and catalog geography/entity sets agree exactly.

The initial-claims three-row split reflects inconsistent raw metadata rather than three cleanly distinct economic concepts:

1. raw row 18: `dol.eta...`, entity role `ui_initial_claimant`, period type `month`;
2. raw row 44: `us.dol...`, the same `ui_initial_claimant`/`month` dimensions, and an alias back to the first concept;
3. raw rows 105-106, 144, 148, 162: `us.dol...`, role `ui_claimant`, proper `week_ending` cadence.

The year-bearing token defect is fixed, and future observations matching each normalized bucket will not mint a UUID per week. But the existing semantic duplication is not healed: rows 1 and 2 still have separate UUIDs even though one aliases the other's canonical concept.

## Six prior dispositions

| Prior issue | Re-review result |
|---|---|
| 1. Untracked seed / bare check | **Partial pass.** Seed is tracked, hashed, and bare check uses it; a byte change fails. Missing seed plus regenerated reduced catalog still passes. |
| 2. Period spellings | **Qualified pass.** All live spellings normalize; BoE and M3 heal and suspects are zero. Initial claims remains three rows, and false-positive stripping is still possible/unflagged. |
| 3. Rename/curation/collision | **Fail.** The commits remint every UUID; auto aliases can merge/flip concepts; geography can move; parsed-equivalent UUID collisions pass; current alias-linked duplicates persist. |
| 4. Concept-only geography collapse | **Partial pass.** Current FNS and Eurostat rows are correctly split, but fallback ignores dimensions and geography vintage is absent from the key. |
| 5. Modal unit/cadence | **Pass.** `_modal` is gone and synthetic observation unit and cadence conflicts both hard-fail through `_sole` (`scripts/build_series_catalog.py:257-266,402-403`). |
| 6. Provenance / no fabricated default geography | **Pass narrowly.** Both digests match exact bytes; seed-only undeclared countries remain null; UUID state is correctly described as catalog state rather than input derivation. |

## CI and validation

The CI step is wired correctly and unconditional for pushes and pull requests: it runs bare `--check` and the focused tests with no error suppression (`.github/workflows/ci.yml:37-40`). An ordinary stale derived field, one-byte seed change, observation change, or missing seed against the current 201-row catalog exits 1 and fails the step.

Its boundary is material: because the catalog under check supplies UUID/alias/canonical state, valid UUID remints and persisted alias changes are considered current. Both the `6ab4fbe` and HEAD UUID-disjoint catalogs pass the HEAD checker. Missing seed plus a consistently regenerated 155-row catalog also passes. Therefore CI is a derived-data freshness check, not an identity-continuity or required-seed check.

Validation record:

```text
python3 scripts/build_series_catalog.py --check                  PASS (201)
pytest tests/test_build_series_catalog.py -q                    PASS (34)
python3 -m doctest scripts/build_series_catalog.py               PASS
ruff check script + focused tests                               PASS
git diff --check 9b4329e..HEAD                                  PASS
one-byte seed change vs current catalog                         FAIL as stale (correct)
missing seed vs current catalog                                 FAIL as stale (correct)
missing seed + regenerated 155-row catalog                      INCORRECT PASS
uppercase/hyphenless/braced duplicate UUID                      INCORRECT PASS
HEAD checker against 6ab4fbe UUID catalog                       INCORRECT PASS (201)
current FNS split                                                PASS (54 identities / 55 observations)
current UUID syntax/version/parsed uniqueness                   PASS (201)
final worktree                                                   CLEAN
```

## Merge gate

Do not merge until, at minimum:

1. The surviving UUID for every pre-existing/merged identity is explicitly curated and the catalog is rebuilt without wholesale reminting; continuity must be checked against the prior committed registry, not only against itself.
2. Alias fallback is scoped to compatible identity dimensions (with an explicit docket-placeholder enrichment rule), and derived source concepts are not treated as curated synonyms without provenance or review.
3. UUIDs are required to use canonical text and uniqueness is keyed by parsed UUID value.
4. Geography boundary vintage participates in identity and required seed absence is a hard error.
5. Period normalization either reserves date-shaped segments by contract or flags mismatches/false-positive candidates.

**Final recommendation: BLOCK.**

