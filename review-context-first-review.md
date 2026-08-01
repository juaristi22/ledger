An adversarial sol review of the first commit returned **BLOCK** with six findings; the two follow-ups respond to all of them. Review highlights and dispositions:

1. **Untracked docket seed / bare `--check` broken** → the seed is now committed at `ledger/seeds/thesis_docket_series.json` and digest-bound in the header (`docket_seed_sha256`); bare `--check` covers the full input set and runs in CI.
2. **29 live period spellings unrecognized, already splitting UUIDs** (`2026_06`, `2026_06_18`, `feb_2026`, `week_ending_…`, `week_2026_06_13`, `after_june_2026`, `after_mpc_june_2026`, `february_to_april_2026`) → the token grammar covers every one, plus a semantic pass derives expected tokens from each row's own `period`, and any surviving year-bearing segment lands in `suspect_segments` (committed catalog: zero). The M3 and initial-claims duplicate UUIDs heal; BoE's two rate spellings merge.
3. **Rename/curation/collision loses or remints identity; `--check` trusts blindly** → UUIDs inherit by identity key, then by unique concept/alias match; curated aliases persist across regeneration and keep the prior row's canonical concept; ambiguous matches and UUID collisions are hard errors; `--check` validates UUID syntax/version/uniqueness.
4. **Concept-only key collapses 54 geography subseries (vs the fact-identity ADR)** → identity is now (concept, geography, entity): FNS error rates split into national + per-state rows; Eurostat flash (EA21/economy) and final (EA/household) separate. 141 rows → 201.
5. **Modal masking** → `_modal` is gone; unit/cadence conflicts within an identity are hard errors, never a silent pick.
6. **Provenance** → header binds observations digest + seed digest; the PR-body claim about field derivation is corrected: UUIDs are minted state preserved across regenerations, not derived from inputs; docket rows without a declared country carry null geography rather than a fabricated default.

Regression tests (`tests/test_build_series_catalog.py`, 34 cases incl. the review's full token table) + CI step added. A focused re-review of the v2 diff runs next; merge only on green + agreement.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
