"""Regression tests for the series-catalog generator.

Sections mirror the adversarial reviews of the first two generators: the
token-grammar findings (live period spellings, identifiers that must never
be stripped), and the v2 identity findings (wholesale reminting invisible
to --check, cross-dimension alias inheritance, source labels treated as
identity, string-keyed UUID uniqueness, silent seed loss, mismatched
period tokens silently stripped).
"""

from __future__ import annotations

import doctest
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_series_catalog as bsc  # noqa: E402

PERIOD_SEGMENTS = [
    "fy2026",
    "2026-05",
    "2026_05",
    "2026-05-02",
    "2026_06_18",
    "may_2026",
    "feb_2026",
    "sept_2026",
    "q1_2026",
    "2026_q2",
    "week_2026-06-13",
    "week_2026_06_13",
    "week_ending_2026_06_06",
    "february_to_april_2026",
    "after_june_2026",
    "after_mpc_june_2026",
]

NON_PERIOD_SEGMENTS = [
    "36-10-0434-01",  # StatCan table id
    "g17",
    "adv44x72",
    "j5ii",
    "m3",
    "2026_13",  # date-shaped noise: no such month
    "2026-02-30",  # date-shaped noise: no such day
    "first_print",
    "third_estimate",
    "original_submission",
    "australia",
    "total_nonfarm_payroll_change",
]


@pytest.mark.parametrize("segment", PERIOD_SEGMENTS)
def test_period_segments_recognized(segment: str) -> None:
    assert bsc.is_period_segment(segment), segment


@pytest.mark.parametrize("segment", NON_PERIOD_SEGMENTS)
def test_non_period_segments_retained(segment: str) -> None:
    assert not bsc.is_period_segment(segment), segment


def test_abbreviated_months_index_correctly() -> None:
    # Generator v2 kept "sept" inside MONTHS_ABBREV, shifting the derived
    # abbreviations for October-December.
    variants = bsc.period_token_variants({"type": "month", "value": "2026-10"})
    assert "oct_2026" in variants and "october_2026" in variants
    variants = bsc.period_token_variants({"type": "month", "value": "2026-12"})
    assert "dec_2026" in variants


MONTH_2026_06 = {"type": "month", "value": "2026-06"}


def test_semantic_pass_strips_unseen_spelling() -> None:
    # A spelling derivable from the row period is the period.
    assert (
        bsc.family_pattern("agency.rate.june_2026", MONTH_2026_06)
        == "agency.rate.{P}"
    )


@pytest.mark.parametrize(
    ("identifier", "period", "expected"),
    [
        # Finer-grained tokens inside the row's declared period strip.
        (
            "dol.eta.initial_claims.sa.week_ending_2026_06_06",
            MONTH_2026_06,
            "dol.eta.initial_claims.sa.{P}",
        ),
        ("boe.bank_rate.2026-06-18", MONTH_2026_06, "boe.bank_rate.{P}"),
        (
            "boe.bank_rate.after_mpc_june_2026",
            MONTH_2026_06,
            "boe.bank_rate.{P}",
        ),
        # A month-range whose window covers the row period strips.
        (
            "ons.labour.unemployment_rate.february_to_april_2026",
            {"type": "month", "value": "2026-04"},
            "ons.labour.unemployment_rate.{P}",
        ),
        (
            "bls.eci.private_wages_salaries_qoq.2026_q2.first_print",
            {"type": "quarter", "value": "2026-04"},
            "bls.eci.private_wages_salaries_qoq.{P}.first_print",
        ),
        ("fns.snap.rate.fy2024", {"type": "fiscal_year", "value": 2024},
         "fns.snap.rate.{P}"),
    ],
)
def test_tokens_matching_row_period_strip(identifier, period, expected) -> None:
    assert bsc.family_pattern(identifier, period) == expected


@pytest.mark.parametrize(
    ("identifier", "period"),
    [
        # Disjoint from the row period: a real date, but not THIS row's.
        ("agency.rate.2025_12", MONTH_2026_06),
        ("agency.rate.week_ending_2026_01_03", MONTH_2026_06),
        ("fns.snap.rate.fy2023", {"type": "fiscal_year", "value": 2024}),
    ],
)
def test_mismatched_tokens_flagged_not_stripped(identifier, period) -> None:
    pattern = bsc.family_pattern(identifier, period)
    assert pattern == identifier  # kept in the identity
    assert bsc.suspect_segments(pattern) == [identifier.rsplit(".", 1)[1]]


def test_suspect_segments_flag_but_never_strip() -> None:
    pattern = bsc.family_pattern("agency.series.mid2026wave")
    assert pattern == "agency.series.mid2026wave"
    assert bsc.suspect_segments(pattern) == ["mid2026wave"]


US = {"level": "country", "id": "0100000US", "vintage": "current",
      "name": "United States"}
CALIFORNIA = {"level": "state", "id": "0400000US06", "vintage": "current",
              "name": "California"}
BRITAIN = {"level": "country", "id": "GB", "vintage": "current", "name": None}


def _row(
    concept: str,
    *,
    rid: str | None = None,
    unit: str = "percent",
    period: dict | None = None,
    geography: dict | None = None,
    entity: dict | None = None,
    source_concept: str | None = None,
) -> dict:
    period = period or {"type": "month", "value": "2026-05"}
    return {
        "value": 1.0,
        "observed_at": "2026-06-01",
        "period": period,
        "geography": geography or dict(US),
        "entity": entity or {"name": "economy", "role": "aggregate"},
        "measure": {
            "concept": concept,
            "unit": unit,
            **({"source_concept": source_concept} if source_concept else {}),
        },
        "source": {"source_name": "test"},
        "source_record_id": rid or f"{concept}.first_print",
    }


def _registry(tmp_path: pathlib.Path, entries: list[dict] | None = None):
    path = tmp_path / "registry.jsonl"
    if not path.exists() or entries is not None:
        body = "".join(
            bsc.UuidRegistry.render_entry(e) + "\n" for e in (entries or [])
        )
        path.write_text(body, encoding="utf-8")
    return bsc.UuidRegistry.load(path)


def _build(
    tmp_path: pathlib.Path,
    rows: list[dict],
    existing: dict | None = None,
    registry_entries: list[dict] | None = None,
    docket: dict | None = None,
):
    observations = tmp_path / "obs.jsonl"
    observations.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    catalog_path = tmp_path / "catalog.json"
    if existing is not None:
        catalog_path.write_text(json.dumps(existing) + "\n", encoding="utf-8")
    docket_path = None
    if docket is not None:
        docket_path = tmp_path / "seed.json"
        docket_path.write_text(json.dumps(docket), encoding="utf-8")
    return bsc.build_catalog(
        observations,
        docket_path,
        bsc.ExistingCatalog(catalog_path),
        _registry(tmp_path, registry_entries),
    )


def test_geography_splits_identity(tmp_path: pathlib.Path) -> None:
    rows = [
        _row("fns.snap.error_rate"),
        _row("fns.snap.error_rate", geography=dict(CALIFORNIA)),
    ]
    catalog, plan = _build(tmp_path, rows)
    assert len(catalog["series"]) == 2
    ids = {(r["geography"] or {}).get("id") for r in catalog["series"]}
    assert ids == {"0100000US", "0400000US06"}
    assert len(plan["mints"]) == 2


def test_geography_vintage_splits_identity(tmp_path: pathlib.Path) -> None:
    v1 = {"level": "state", "id": "0400000US06", "vintage": "2020"}
    v2 = {"level": "state", "id": "0400000US06", "vintage": "2024"}
    rows = [
        _row("fns.snap.error_rate", geography=v1),
        _row("fns.snap.error_rate", geography=v2,
             period={"type": "month", "value": "2026-06"}),
    ]
    catalog, _ = _build(tmp_path, rows)
    assert len(catalog["series"]) == 2
    forward = [r["geography"]["vintage"] for r in catalog["series"]]
    catalog_reversed, _ = _build(tmp_path, list(reversed(rows)))
    assert forward == [
        r["geography"]["vintage"] for r in catalog_reversed["series"]
    ]
    assert sorted(forward) == ["2020", "2024"]


def test_cross_geography_name_match_never_inherits(
    tmp_path: pathlib.Path,
) -> None:
    # v2 repro: a prior US row plus a later California-only observation of
    # the same concept silently moved the US UUID to California.
    first, _ = _build(tmp_path, [_row("fns.snap.error_rate")])
    us_uuid = first["series"][0]["uuid"]
    later, plan = _build(
        tmp_path,
        [_row("fns.snap.error_rate", geography=dict(CALIFORNIA))],
        existing=first,
    )
    assert later["series"][0]["uuid"] != us_uuid
    # The US identity's UUID vanishing is loud, not silent.
    assert [key for key, _ in plan["dropped"]] == [
        ("fns.snap.error_rate", "country|0100000US|current",
         "economy|aggregate")
    ]


def test_new_geography_added_incrementally(tmp_path: pathlib.Path) -> None:
    # v2 failed this with an ambiguous global name match once a concept had
    # several prior geographies.
    states = [dict(CALIFORNIA), {"level": "state", "id": "0400000US36",
                                 "vintage": "current"}]
    rows = [_row("fns.snap.error_rate")] + [
        _row("fns.snap.error_rate", geography=g) for g in states
    ]
    first, first_plan = _build(tmp_path, rows)
    assert len(first["series"]) == 3
    added = rows + [
        _row(
            "fns.snap.error_rate",
            geography={"level": "state", "id": "0400000US48",
                       "vintage": "current"},
        )
    ]
    second, plan = _build(
        tmp_path, added, existing=first,
        registry_entries=first_plan["mints"],
    )
    assert len(second["series"]) == 4
    old = {r["uuid"] for r in first["series"]}
    assert old < {r["uuid"] for r in second["series"]}
    # Only the genuinely new geography mints; the registered three inherit.
    assert len(plan["mints"]) == 1 and not plan["dropped"]
    assert plan["mints"][0]["geography"]["id"] == "0400000US48"


def test_source_concept_never_drives_inheritance(
    tmp_path: pathlib.Path,
) -> None:
    # v2 repro: agency.rate_b with agency.rate_a's source label silently
    # became (or merged into) agency.rate_a.
    first, _ = _build(
        tmp_path, [_row("agency.rate_a", source_concept="OFFICIAL_SHARED")]
    )
    a_uuid = first["series"][0]["uuid"]
    assert first["series"][0]["aliases"] == []
    assert first["series"][0]["source_concepts"] == ["OFFICIAL_SHARED"]
    solo_b, _ = _build(
        tmp_path,
        [_row("agency.rate_b", source_concept="OFFICIAL_SHARED")],
        existing=first,
    )
    assert [r["concept"] for r in solo_b["series"]] == ["agency.rate_b"]
    assert solo_b["series"][0]["uuid"] != a_uuid
    both, _ = _build(
        tmp_path,
        [
            _row("agency.rate_a", source_concept="OFFICIAL_SHARED"),
            _row("agency.rate_b", source_concept="OFFICIAL_SHARED"),
        ],
        existing=first,
    )
    concepts = [r["concept"] for r in both["series"]]
    assert concepts == ["agency.rate_a", "agency.rate_b"]
    assert both["series"][0]["uuid"] == a_uuid
    assert both["series"][0]["observation_count"] == 1  # no silent merge


def test_curated_alias_inherits_within_dimensions(
    tmp_path: pathlib.Path,
) -> None:
    first, _ = _build(tmp_path, [_row("abs.cpi.all_groups.yoy")])
    row = first["series"][0]
    row["aliases"] = ["abs.cpi_indicator.allgroups.yoy"]
    merged, plan = _build(
        tmp_path,
        [_row("abs.cpi_indicator.allgroups.yoy")],
        existing=first,
    )
    assert merged["series"][0]["uuid"] == row["uuid"]
    assert merged["series"][0]["concept"] == "abs.cpi.all_groups.yoy"
    assert "abs.cpi_indicator.allgroups.yoy" in merged["series"][0]["aliases"]
    assert not plan["dropped"] and not plan["supersedes"]


def test_curated_alias_does_not_inherit_across_geography(
    tmp_path: pathlib.Path,
) -> None:
    first, _ = _build(tmp_path, [_row("boe.bank_rate")])
    first["series"][0]["aliases"] = ["bank_rate.official"]
    stolen, _ = _build(
        tmp_path,
        [_row("bank_rate.official", geography=dict(BRITAIN))],
        existing=first,
    )
    assert stolen["series"][0]["uuid"] != first["series"][0]["uuid"]


def test_ambiguous_alias_match_is_a_hard_error(tmp_path: pathlib.Path) -> None:
    existing = {
        "series": [
            {
                "uuid": "11111111-1111-4111-8111-111111111111",
                "concept": "a.one",
                "geography": dict(US),
                "entity": {"name": "economy", "role": "aggregate"},
                "aliases": ["SHARED"],
            },
            {
                "uuid": "22222222-2222-4222-8222-222222222222",
                "concept": "a.two",
                "geography": dict(US),
                "entity": {"name": "economy", "role": "aggregate"},
                "aliases": ["SHARED"],
            },
        ]
    }
    with pytest.raises(SystemExit, match="multiple existing UUIDs"):
        _build(tmp_path, [_row("SHARED")], existing=existing)


def test_docket_placeholder_enrichment_keeps_uuid(
    tmp_path: pathlib.Path,
) -> None:
    placeholder_uuid = "33333333-3333-4333-8333-333333333333"
    existing = {
        "series": [
            {
                "uuid": placeholder_uuid,
                "concept": "census.m3.new_orders",
                "geography": {"level": "country", "id": "0100000US",
                              "name": "United States"},
                "entity": None,
                "aliases": [],
                "status": "docket-only",
            }
        ]
    }
    catalog, plan = _build(
        tmp_path, [_row("census.m3.new_orders")], existing=existing
    )
    assert len(catalog["series"]) == 1
    row = catalog["series"][0]
    assert row["uuid"] == placeholder_uuid
    assert row["status"] == "observed"
    assert row["entity"] == {"name": "economy", "role": "aggregate"}
    assert not plan["dropped"]  # lineage survives on the enriched identity


def test_docket_placeholder_never_enriches_across_country(
    tmp_path: pathlib.Path,
) -> None:
    existing = {
        "series": [
            {
                "uuid": "44444444-4444-4444-8444-444444444444",
                "concept": "labour.unemployment_rate",
                "geography": {"level": "country", "id": "0100000US",
                              "name": "United States"},
                "entity": None,
                "aliases": [],
                "status": "docket-only",
            }
        ]
    }
    catalog, _ = _build(
        tmp_path,
        [_row("labour.unemployment_rate", geography=dict(BRITAIN))],
        existing=existing,
    )
    observed = [r for r in catalog["series"] if r["status"] == "observed"]
    assert observed[0]["uuid"] != "44444444-4444-4444-8444-444444444444"


def test_unit_conflict_is_a_hard_error(tmp_path: pathlib.Path) -> None:
    rows = [
        _row("fed.rate", unit="percent"),
        _row(
            "fed.rate",
            unit="index_points",
            period={"type": "month", "value": "2026-06"},
        ),
    ]
    with pytest.raises(SystemExit, match="unit conflict"):
        _build(tmp_path, rows)


def test_uuid_validation_requires_canonical_and_parsed_uniqueness() -> None:
    base = "abcd1234-ab12-4ab1-8ab1-abcd1234abcd"
    catalog = {
        "series": [
            {"uuid": "not-a-uuid", "concept": "a"},
            {"uuid": base, "concept": "b"},
            {"uuid": base, "concept": "c"},
            {"uuid": base.upper(), "concept": "d"},
            {"uuid": base.replace("-", ""), "concept": "e"},
            {"uuid": "{" + base + "}", "concept": "f"},
        ]
    }
    problems = bsc.validate_uuids(catalog)
    assert any("does not parse" in p for p in problems)
    # Every non-canonical spelling is rejected AND still counted as the
    # same 128-bit value.
    assert sum("not canonical lowercase" in p for p in problems) == 3
    assert sum("same 128-bit value" in p for p in problems) == 4


def test_registry_chain_validation(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "registry.jsonl"
    mint = {"concept": "a.one", "geography": None, "entity": None,
            "uuid": "11111111-1111-4111-8111-111111111111"}
    rebind = {"concept": "a.one", "geography": None, "entity": None,
              "uuid": "22222222-2222-4222-8222-222222222222"}
    path.write_text(
        json.dumps(mint) + "\n" + json.dumps(rebind) + "\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="re-binds without supersedes"):
        bsc.UuidRegistry.load(path)
    chained = dict(rebind, supersedes=mint["uuid"], note="curated remint")
    path.write_text(
        json.dumps(mint) + "\n" + json.dumps(chained) + "\n", encoding="utf-8"
    )
    registry = bsc.UuidRegistry.load(path)
    assert registry.binding(("a.one", "None|None|None", "None|None")) == (
        rebind["uuid"]
    )
    wrong_chain = dict(
        rebind, supersedes="99999999-9999-4999-8999-999999999999", note="x"
    )
    path.write_text(
        json.dumps(mint) + "\n" + json.dumps(wrong_chain) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="prior binding is"):
        bsc.UuidRegistry.load(path)
    no_note = dict(rebind, supersedes=mint["uuid"])
    path.write_text(
        json.dumps(mint) + "\n" + json.dumps(no_note) + "\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="requires a note"):
        bsc.UuidRegistry.load(path)


def test_registry_append_only_check() -> None:
    base = b'{"concept": "a", "uuid": "x"}\n'
    assert bsc.append_only_problem(base, base) is None
    assert bsc.append_only_problem(base, base + b'{"more": 1}\n') is None
    assert bsc.append_only_problem(base, b'{"edited": true}\n') is not None
    assert bsc.append_only_problem(base, b"") is not None


SEED = {
    "series": [
        {
            "series": "abs.labour.unemployment_rate",
            "cadence": "monthly",
            "extras": {"country": "AU", "targetUnit": "percent"},
        }
    ]
}


def _repo(tmp_path: pathlib.Path, rows: list[dict], seed: dict = SEED):
    (tmp_path / "obs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    (tmp_path / "seed.json").write_text(json.dumps(seed), encoding="utf-8")
    (tmp_path / "registry.jsonl").write_text("", encoding="utf-8")
    return [
        "--observations", str(tmp_path / "obs.jsonl"),
        "--catalog", str(tmp_path / "catalog.json"),
        "--docket", str(tmp_path / "seed.json"),
        "--registry", str(tmp_path / "registry.jsonl"),
    ]


def test_main_builds_and_is_byte_idempotent(
    tmp_path: pathlib.Path, capsys
) -> None:
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    assert bsc.main(argv) == 0
    catalog_1 = (tmp_path / "catalog.json").read_bytes()
    registry_1 = (tmp_path / "registry.jsonl").read_bytes()
    assert bsc.main(argv) == 0
    assert (tmp_path / "catalog.json").read_bytes() == catalog_1
    assert (tmp_path / "registry.jsonl").read_bytes() == registry_1
    assert bsc.main(argv + ["--check"]) == 0
    assert "catalog current: 2 series" in capsys.readouterr().out


def test_main_missing_seed_is_a_hard_error(tmp_path: pathlib.Path) -> None:
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    assert bsc.main(argv) == 0
    (tmp_path / "seed.json").unlink()
    with pytest.raises(SystemExit, match="docket seed missing"):
        bsc.main(argv)
    with pytest.raises(SystemExit, match="docket seed missing"):
        bsc.main(argv + ["--check"])


def test_main_missing_registry_is_a_hard_error(
    tmp_path: pathlib.Path,
) -> None:
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    (tmp_path / "registry.jsonl").unlink()
    with pytest.raises(SystemExit, match="uuid registry missing"):
        bsc.main(argv)


def test_main_remint_guard_and_ceremony(tmp_path: pathlib.Path) -> None:
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    assert bsc.main(argv) == 0
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    observed = next(
        r for r in catalog["series"] if r["status"] == "observed"
    )
    old_uuid = observed["uuid"]
    observed["uuid"] = "55555555-5555-4555-8555-555555555555"
    (tmp_path / "catalog.json").write_text(
        json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
    )
    registry_before = (tmp_path / "registry.jsonl").read_bytes()
    # Refuses without the flag, and writes nothing.
    assert bsc.main(argv) == 1
    assert (tmp_path / "registry.jsonl").read_bytes() == registry_before
    # A pending remint also fails --check.
    assert bsc.main(argv + ["--check"]) == 1
    with pytest.raises(SystemExit, match="requires --remint-note"):
        bsc.main(argv + ["--allow-remint"])
    assert (
        bsc.main(argv + ["--allow-remint", "--remint-note", "test remint"])
        == 0
    )
    lines = (tmp_path / "registry.jsonl").read_text().splitlines()
    event = json.loads(lines[-1])
    assert event["supersedes"] == old_uuid
    assert event["uuid"] == "55555555-5555-4555-8555-555555555555"
    assert event["note"] == "test remint"
    assert bsc.main(argv + ["--check"]) == 0


def test_main_dropped_identity_requires_allow_remint(
    tmp_path: pathlib.Path,
) -> None:
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    assert bsc.main(argv) == 0
    (tmp_path / "seed.json").write_text(
        json.dumps({"series": []}), encoding="utf-8"
    )
    assert bsc.main(argv) == 1  # docket-only row would vanish
    assert bsc.main(argv + ["--allow-remint", "--remint-note", "seed cut"]) == 0
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    assert len(catalog["series"]) == 1
    # The binding stays dormant in the registry: no line was removed.
    lines = (tmp_path / "registry.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert bsc.main(argv + ["--check"]) == 0


def test_check_rejects_uuid_disjoint_catalog(tmp_path: pathlib.Path) -> None:
    # v2 repro: two UUID-disjoint catalogs for the same inputs both passed
    # --check. The registry now pins the bindings.
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    assert bsc.main(argv) == 0
    body = (tmp_path / "catalog.json").read_text()
    catalog = json.loads(body)
    for i, row in enumerate(catalog["series"]):
        row["uuid"] = f"7777777{i}-7777-4777-8777-777777777777"
    (tmp_path / "catalog.json").write_text(
        json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
    )
    assert bsc.main(argv + ["--check"]) == 1


def test_verify_registry_append_only_mode(
    tmp_path: pathlib.Path, capsys
) -> None:
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    assert bsc.main(argv) == 0
    registry = tmp_path / "registry.jsonl"
    base = tmp_path / "base.jsonl"
    base.write_bytes(registry.read_bytes())
    verify = ["--registry", str(registry),
              "--verify-registry-append-only", str(base)]
    assert bsc.main(verify) == 0
    assert "append-only vs base: ok" in capsys.readouterr().out
    # An absent base (registry introduced by this change) is fine.
    assert bsc.main(
        ["--registry", str(registry),
         "--verify-registry-append-only", str(tmp_path / "nope.jsonl")]
    ) == 0
    # Any edit to committed lines fails.
    lines = registry.read_text().splitlines()
    first = json.loads(lines[0])
    first["uuid"] = "66666666-6666-4666-8666-666666666666"
    registry.write_text(
        "\n".join([json.dumps(first)] + lines[1:]) + "\n", encoding="utf-8"
    )
    assert bsc.main(verify) == 1


def test_doctests_pass() -> None:
    results = doctest.testmod(bsc)
    assert results.failed == 0


def test_committed_catalog_is_current_and_valid() -> None:
    """The committed artifact must regenerate byte-identically from the
    committed inputs, agree with the committed registry binding-for-binding,
    and carry canonical, parsed-unique UUIDv4s."""
    committed_text = bsc.CATALOG.read_text(encoding="utf-8")
    committed = json.loads(committed_text)
    registry = bsc.UuidRegistry.load(bsc.UUID_REGISTRY)
    catalog, plan = bsc.build_catalog(
        bsc.OBSERVATIONS,
        bsc.DOCKET_SEED,
        bsc.ExistingCatalog(bsc.CATALOG),
        registry,
    )
    assert not plan["mints"] and not plan["supersedes"] and not plan["dropped"]
    catalog["uuid_registry_sha256"] = registry.sha256()
    assert bsc.render(catalog) == committed_text
    assert bsc.validate_uuids(committed) == []
    assert bsc.registry_agreement_problems(committed, registry) == []
    assert committed["suspect_segments"] == []
    assert committed["docket_seed_sha256"] is not None
    assert committed["uuid_registry_sha256"] == registry.sha256()
    assert bsc.DOCKET_SEED.exists()
    assert len(committed["series"]) == 201


def test_rebuild_without_prior_catalog_is_gated(
    tmp_path: pathlib.Path,
) -> None:
    # Deleting the committed catalog loses curated naming/alias memory: a
    # renamed identity would re-key away from its registry binding and
    # fresh-mint silently. The builder must treat a bare-registry rebuild
    # as an identity event.
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    assert bsc.main(argv) == 0
    (tmp_path / "catalog.json").unlink()
    assert bsc.main(argv) == 1
    assert bsc.main(
        argv + ["--allow-remint", "--remint-note", "rebuild from registry"]
    ) == 0
