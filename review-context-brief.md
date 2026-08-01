# Third adversarial review brief — series-catalog v3 (commits 3cb0c8a + 45fcc10 + 2c31ae4)

You are the third adversarial reviewer of PolicyEngine/ledger PR #128 on
branch thesis-series-catalog. The first two reviews returned BLOCK; v3
claims to answer every finding. Your job is to try to break v3.

Materials (untracked briefing files in this worktree root — ignore them in
any cleanliness assessment, do not commit or delete them):
- review-context-first-review.md   (first BLOCK review)
- review-context-second-review.md  (second BLOCK review — the v3 spec)
- review-context-v3-disposition.md (the disposition you are auditing)

Scope: the v3 change = commits 3cb0c8a + 45fcc10 + 2c31ae4 (diff 2859ecb..2c31ae4, the branch head):
scripts/build_series_catalog.py, tests/test_build_series_catalog.py,
ledger/series_catalog.json, ledger/series_uuid_registry.jsonl (new),
.github/workflows/ci.yml.

A previous run of this review crashed before finishing; its four findings
(lossy pipe-joined keys; partial-catalog silent re-mint; silent overlap
strips; PR-only CI append gate) are claimed FIXED in 2c31ae4 — re-verify
each fix adversarially as part of Section 2.

RUNTIME BUDGET (hard): do NOT launch the full repository test suite (uv run
pytest with no path); it exceeds your session budget and stalled your
predecessor — CI covers it. Run the focused suite
(tests/test_build_series_catalog.py), ruff on the two changed files,
doctest, --check, and targeted experiments only. Prefer many small commands
over any long-running one; nothing you start should run longer than ~90
seconds.

Required work, in order:

1. VERIFY EVERY DISPOSITION CLAIM INDEPENDENTLY. Do not trust the
   disposition's validation record — rerun it: pytest, ruff, doctest,
   --check, byte idempotence (catalog AND registry), the 201/201 UUID
   continuity claim from 2859ecb, the 6ab4fbe catalog-swap repro, missing
   seed both modes, uuid spelling variants, registry line edits vs --check
   and vs --verify-registry-append-only, the remint ceremony (refuse /
   note required / supersede line appended / chain validates / check green
   after), the dropped-identity guard, and each healed case (FNS 54-way
   incl. national count 2, BoE count 2, M3 observed, Eurostat flash/final
   separation, 46 docket-only, zero suspects, empty ambiguous_aliases).

2. ATTACK THE NEW MECHANICS. At minimum:
   - Registry semantics: can you construct a registry state that validates
     but lets an identity change UUIDs silently? Chain forgery? A mint
     line appended for an existing identity under a cosmetically different
     key spelling (geography name/vintage variations, entity null vs
     missing)? Shared-uuid states that corrupt the catalog?
   - Append-only enforcement: bypasses via git states (clean tree after
     committing a rewritten registry — what catches it and when), the CI
     step's base-sha choice, shallow clones, the file not existing at
     base, trailing-newline and encoding edge cases in the byte-prefix
     comparison.
   - The --allow-remint ceremony: can a remint slip through without a
     supersede line? Can supersede lines be written that misdescribe what
     happened? Does --check really fail for every pending identity change?
   - Same-dimension scoping: cross-geography/entity theft via crafted
     aliases, vintage-only mismatches, null-vs-present geography, the
     docket-placeholder enrichment exception (cross-country, entity
     present, multiple placeholders).
   - Interval-overlap stripping: tokens that overlap the row period but
     are semantically NOT period labels (statute years, cohort years,
     table editions shaped like dates); fiscal-year edge cases (fy token
     on month rows, non-US fiscal conventions); quarter/month boundary
     overlaps; impossible tokens; the suspect-flagging contract.
   - Canonical UUID enforcement: any path where a non-canonical or
     duplicate-by-value uuid enters the catalog or registry.
   - The live artifact: spot-check rows against raw observations again
     (your predecessor's 12-row table), confirm no identity moved
     geography/entity/vintage vs 2859ecb, confirm the two new curated
     aliases are the ONLY alias additions and are justified, confirm
     source_concepts fields are faithful.

3. JUDGE THE RESIDUALS the disposition declares deliberate: the three
   alias-linked pairs left separate; dormant registry bindings after
   drops; enrichment minting a second binding for the same uuid; the
   one-time offline migration instead of in-code scrub. Are any of these
   exploitable or dishonest rather than merely conservative?

Rules: read-only with respect to tracked files — run all mutating
experiments on copies under /tmp, never on this worktree's tracked files;
leave `git status` clean apart from the four review-context-*.md files.
Use python3/uv, ruff, pytest as the repo does. Do not push, do not comment
on GitHub; your only output is the report.

Output format — end your final message with exactly this structure:

REPORT
verdict: MERGE | BLOCK
risk: LOW | MEDIUM | HIGH | CRITICAL
findings: (ranked, each with severity, file:line evidence, and a concrete
reproduction; empty section allowed only with verdict MERGE)
disposition-audit: (per disposition claim: CONFIRMED | REFUTED | PARTIAL,
one line each)
residuals-judgment: (per declared residual: ACCEPTABLE | UNACCEPTABLE + why)
validation-record: (commands you ran and their outcomes)
