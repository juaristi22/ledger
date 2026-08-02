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
        (
            "fns.snap.error_rate",
            bsc._geo_key(US),
            bsc._entity_key({"name": "economy", "role": "aggregate"}),
        )
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
    key = ("a.one", bsc._geo_key(None), bsc._entity_key(None))
    assert registry.binding(key) == rebind["uuid"]
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
    with pytest.raises(SystemExit, match="requires a substantive note"):
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


def test_main_dropped_identity_retires_then_revives(
    tmp_path: pathlib.Path,
) -> None:
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    assert bsc.main(argv) == 0
    original = json.loads((tmp_path / "catalog.json").read_text())
    docket_uuid = next(
        r["uuid"] for r in original["series"] if r["status"] == "docket-only"
    )
    (tmp_path / "seed.json").write_text(
        json.dumps({"series": []}), encoding="utf-8"
    )
    assert bsc.main(argv) == 1  # docket-only row would vanish
    assert bsc.main(argv + ["--allow-remint", "--remint-note", "seed cut"]) == 0
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    assert len(catalog["series"]) == 1
    # The drop is an explicit retire event; no line was edited or removed.
    lines = (tmp_path / "registry.jsonl").read_text().splitlines()
    assert len(lines) == 3
    retire = json.loads(lines[-1])
    assert retire["retired"] is True and retire["uuid"] == docket_uuid
    assert retire["note"] == "seed cut"
    assert bsc.main(argv + ["--check"]) == 0
    # Re-seeding the identity revives the SAME uuid — minted once, ever.
    (tmp_path / "seed.json").write_text(json.dumps(SEED), encoding="utf-8")
    assert bsc.main(argv) == 0
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    revived_row = next(
        r for r in catalog["series"] if r["status"] == "docket-only"
    )
    assert revived_row["uuid"] == docket_uuid
    lines = (tmp_path / "registry.jsonl").read_text().splitlines()
    assert len(lines) == 4
    revive = json.loads(lines[-1])
    assert revive["revived"] is True and revive["uuid"] == docket_uuid
    assert bsc.main(argv + ["--check"]) == 0


def test_partial_catalog_deletion_cannot_silently_remint(
    tmp_path: pathlib.Path,
) -> None:
    # The v3-review follow-up: deleting SOME rows (not the whole catalog)
    # must not let their identities re-mint silently — every live binding's
    # uuid has to stay in the catalog or be explicitly retired.
    argv = _repo(
        tmp_path,
        [_row("bls.cps.unemployment_rate"), _row("bea.real_gdp.saar")],
    )
    assert bsc.main(argv) == 0
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    kept = [r for r in catalog["series"] if r["concept"] != "bea.real_gdp.saar"]
    removed_uuid = next(
        r["uuid"] for r in catalog["series"]
        if r["concept"] == "bea.real_gdp.saar"
    )
    catalog["series"] = kept
    (tmp_path / "catalog.json").write_text(
        json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
    )
    # The observations still exist, so rebuilding restores the row with its
    # registry uuid — but a tampered catalog alone must fail --check on the
    # liveness rule before any rebuild.
    problems = bsc.registry_agreement_problems(
        catalog, bsc.UuidRegistry.load(tmp_path / "registry.jsonl")
    )
    assert any(removed_uuid in p and "no catalog row" in p for p in problems)
    assert bsc.main(argv + ["--check"]) == 1
    # Rebuild heals: the registry still holds the binding.
    assert bsc.main(argv) == 0
    healed = json.loads((tmp_path / "catalog.json").read_text())
    assert any(r["uuid"] == removed_uuid for r in healed["series"])


def test_dimension_keys_are_injective() -> None:
    # Delimiter-joined keys let crafted values collide across fields.
    assert bsc._geo_key(
        {"level": "a|b", "id": "c", "vintage": None}
    ) != bsc._geo_key({"level": "a", "id": "b|c", "vintage": None})
    assert bsc._geo_key({"level": "None"}) != bsc._geo_key(None)
    assert bsc._entity_key(
        {"name": 'x", "y', "role": None}
    ) != bsc._entity_key({"name": "x", "role": "y"})


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
    # EVERY stripped spelling is auditable, mapped to the canonical
    # concepts it touched — a statute or edition label colliding with a
    # period spelling can only be caught here.
    assert sorted(committed["stripped_segments"]) == [
        "2026-05", "2026-06", "2026-06-18", "2026-07", "2026_05",
        "2026_06", "2026_06_18", "2026_q2", "after_june_2026",
        "after_mpc_june_2026", "april_2026", "feb_2026",
        "february_to_april_2026", "fy2024", "fy2025", "june_2026",
        "may_2026", "q1_2026", "week_2026-06-13", "week_2026-06-20",
        "week_2026-06-27", "week_2026-07-04", "week_2026-07-11",
        "week_2026-07-18", "week_2026-07-25", "week_2026_06_13",
        "week_ending_2026_06_06",
    ]
    assert committed["stripped_segments"]["after_mpc_june_2026"] == [
        "boe.bank_rate"
    ]
    assert all(
        occurrences and occurrences == sorted(set(occurrences))
        for occurrences in committed["stripped_segments"].values()
    )
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


def _mint(concept: str, uuid: str, geography=None, entity=None) -> dict:
    return {
        "concept": concept,
        "geography": geography,
        "entity": entity,
        "uuid": uuid,
    }


U1 = "aaaaaaaa-1111-4111-8111-111111111111"
U2 = "bbbbbbbb-2222-4222-8222-222222222222"


def test_mint_never_reuses_a_bound_uuid(tmp_path: pathlib.Path) -> None:
    # Third-review repro: re-key identities and swap their prior-catalog
    # UUIDs — both "new" identities would previously mint the swapped
    # values as ordinary mints.
    registry_entries = [
        _mint("old.one", U1, entity={"name": "economy", "role": "aggregate"}),
        _mint("old.two", U2, entity={"name": "economy", "role": "aggregate"}),
    ]
    existing = {
        "series": [
            {
                "uuid": U2,  # swapped
                "concept": "new.one",
                "geography": dict(US),
                "entity": {"name": "economy", "role": "aggregate"},
                "aliases": [],
                "status": "observed",
            },
            {
                "uuid": U1,  # swapped
                "concept": "new.two",
                "geography": dict(US),
                "entity": {"name": "economy", "role": "aggregate"},
                "aliases": [],
                "status": "observed",
            },
        ]
    }
    with pytest.raises(SystemExit, match="already binds"):
        _build(
            tmp_path,
            [_row("new.one"), _row("new.two")],
            existing=existing,
            registry_entries=registry_entries,
        )


def test_registry_rejects_forged_shared_uuid_mint(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "registry.jsonl"
    path.write_text(
        json.dumps(_mint("a.one", U1)) + "\n"
        + json.dumps(_mint("a.two", U1)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="reuses uuid"):
        bsc.UuidRegistry.load(path)


def test_registry_rejects_duplicate_json_members(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "registry.jsonl"
    line = (
        '{"concept": "a.one", "geography": null, "entity": null, '
        f'"uuid": "{U1}", "uuid": "{U2}"}}'
    )
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not strict JSON"):
        bsc.UuidRegistry.load(path)


def test_registry_requires_lf_discipline(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "registry.jsonl"
    body = json.dumps(_mint("a.one", U1))
    path.write_bytes(body.encode())  # no trailing newline
    with pytest.raises(SystemExit, match="end with a newline"):
        bsc.UuidRegistry.load(path)
    path.write_bytes(body.encode() + b"\r\n")
    with pytest.raises(SystemExit, match="LF-only"):
        bsc.UuidRegistry.load(path)


def test_registry_rejects_hollow_notes_and_noop_supersedes(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "registry.jsonl"
    mint = _mint("a.one", U1)
    hollow = dict(_mint("a.one", U2), supersedes=U1, note="​")
    path.write_text(
        json.dumps(mint) + "\n" + json.dumps(hollow) + "\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="substantive note"):
        bsc.UuidRegistry.load(path)
    noop = dict(_mint("a.one", U1), supersedes=U1, note="says nothing changed")
    path.write_text(
        json.dumps(mint) + "\n" + json.dumps(noop) + "\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="no-op"):
        bsc.UuidRegistry.load(path)


def test_agreement_is_identity_aware(tmp_path: pathlib.Path) -> None:
    # Rows carrying live UUIDs under the WRONG identities must fail even
    # though every UUID is "present somewhere" in the catalog.
    entries = [_mint("a.one", U1), _mint("a.two", U2)]
    registry = _registry(tmp_path, entries)
    catalog = {
        "series": [
            {"uuid": U1, "concept": "b.one", "geography": None, "entity": None},
            {"uuid": U2, "concept": "b.two", "geography": None, "entity": None},
        ]
    }
    problems = bsc.registry_agreement_problems(catalog, registry)
    assert sum("no catalog row at its identity" in p for p in problems) == 2


def test_enrichment_records_retire_and_succeeds_events(
    tmp_path: pathlib.Path,
) -> None:
    seed = {
        "series": [
            {
                "series": "census.m3.new_orders",
                "cadence": "monthly",
                "extras": {"country": "US", "targetUnit": "percent"},
            }
        ]
    }
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")], seed=seed)
    assert bsc.main(argv) == 0
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    placeholder_uuid = next(
        r["uuid"] for r in catalog["series"] if r["status"] == "docket-only"
    )
    (tmp_path / "obs.jsonl").write_text(
        "".join(
            json.dumps(r) + "\n"
            for r in [
                _row("bls.cps.unemployment_rate"),
                _row("census.m3.new_orders"),
            ]
        ),
        encoding="utf-8",
    )
    assert bsc.main(argv) == 0  # enrichment needs no ceremony flag
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    enriched = next(
        r for r in catalog["series"] if r["concept"] == "census.m3.new_orders"
    )
    assert enriched["status"] == "observed"
    assert enriched["uuid"] == placeholder_uuid
    lines = [
        json.loads(line)
        for line in (tmp_path / "registry.jsonl").read_text().splitlines()
    ]
    retire = next(line for line in lines if line.get("retired"))
    succeed = next(line for line in lines if line.get("succeeds"))
    assert retire["uuid"] == succeed["uuid"] == placeholder_uuid
    assert succeed["succeeds"]["concept"] == "census.m3.new_orders"
    assert bsc.main(argv + ["--check"]) == 0


def test_placeholder_with_conflicting_vintage_never_enriches(
    tmp_path: pathlib.Path,
) -> None:
    existing = {
        "series": [
            {
                "uuid": U1,
                "concept": "labour.rate",
                "geography": {"level": "country", "id": "0100000US",
                              "vintage": "2020"},
                "entity": None,
                "aliases": [],
                "status": "docket-only",
            }
        ]
    }
    catalog, plan = _build(
        tmp_path,
        [_row("labour.rate")],  # arrives with vintage "current"
        existing=existing,
        registry_entries=[
            _mint(
                "labour.rate",
                U1,
                geography={"level": "country", "id": "0100000US",
                           "vintage": "2020"},
            )
        ],
    )
    observed = next(r for r in catalog["series"] if r["status"] == "observed")
    assert observed["uuid"] != U1
    # The stranded placeholder binding is a gated retire, never silent.
    assert [e["uuid"] for e in plan["retire_pending"]] == [U1]


def test_docket_claim_is_dimension_scoped(tmp_path: pathlib.Path) -> None:
    gb_row = _row("boe.bank_rate", geography=dict(BRITAIN))
    us_entry = {
        "series": "boe.bank_rate",
        "cadence": "monthly",
        "extras": {"country": "US", "targetUnit": "percent"},
    }
    catalog, _ = _build(tmp_path, [gb_row], docket={"series": [us_entry]})
    assert len(catalog["series"]) == 2  # GB observed + US docket-only
    statuses = {
        (r["geography"] or {}).get("id"): r["status"] for r in catalog["series"]
    }
    assert statuses == {"GB": "observed", "0100000US": "docket-only"}

    undeclared = {"series": "boe.bank_rate", "cadence": "monthly"}
    catalog, _ = _build(tmp_path, [gb_row], docket={"series": [undeclared]})
    assert len(catalog["series"]) == 1  # no country claim: GB row claims

    with pytest.raises(SystemExit, match="duplicate docket series id"):
        _build(
            tmp_path,
            [gb_row],
            docket={"series": [undeclared, dict(undeclared)]},
        )


def test_statute_spelling_stripped_but_audited(
    tmp_path: pathlib.Path,
) -> None:
    # A statute/edition label spelling the row's own period is
    # indistinguishable from a period label; it strips, but the spelling
    # is published — mapped to every canonical concept it touched, so a
    # second occurrence is visible rather than absorbed into a set.
    catalog, _ = _build(
        tmp_path,
        [
            _row("agency.statute.2026_05.rate",
                 rid="agency.statute.2026_05.rate.first_print"),
            _row("other.report.2026_05.level",
                 rid="other.report.2026_05.level.first_print"),
        ],
    )
    concepts = {r["concept"] for r in catalog["series"]}
    assert concepts == {"agency.statute.rate", "other.report.level"}
    assert catalog["stripped_segments"]["2026_05"] == [
        "agency.statute.rate", "other.report.level",
    ]


@pytest.mark.parametrize(
    "period",
    [
        {"type": "month", "value": "2026-13"},
        {"type": "quarter", "value": "2026-13"},
        {"type": "week_ending", "value": "2026-13-40"},
    ],
)
def test_malformed_periods_are_hard_errors(
    tmp_path: pathlib.Path, period: dict
) -> None:
    with pytest.raises(SystemExit, match="malformed period"):
        _build(tmp_path, [_row("agency.rate", period=period)])


def test_remint_of_retired_identity_stages_valid_chain(
    tmp_path: pathlib.Path,
) -> None:
    # Third-review repro: retire an identity, then bring it back with an
    # edited prior-catalog UUID under --allow-remint. The writer must
    # stage revive THEN supersede (a valid chain) — and staging always
    # revalidates the whole registry before anything is written.
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    assert bsc.main(argv) == 0
    (tmp_path / "seed.json").write_text(
        json.dumps({"series": []}), encoding="utf-8"
    )
    assert bsc.main(
        argv + ["--allow-remint", "--remint-note", "seed entry cut"]
    ) == 0
    (tmp_path / "seed.json").write_text(json.dumps(SEED), encoding="utf-8")
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    # Hand-plant a divergent uuid for the returning docket identity.
    replacement = "cccccccc-3333-4333-8333-333333333333"
    lines = (tmp_path / "registry.jsonl").read_text().splitlines()
    retired_uuid = json.loads(lines[-1])["uuid"]
    catalog["series"].append(
        {
            "uuid": replacement,
            "concept": "abs.labour.unemployment_rate",
            "geography": bsc.COUNTRY_GEOGRAPHY["AU"],
            "entity": None,
            "aliases": [],
            "status": "docket-only",
        }
    )
    (tmp_path / "catalog.json").write_text(
        json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
    )
    assert bsc.main(argv) == 1  # gated
    assert (
        bsc.main(argv + ["--allow-remint", "--remint-note", "planned swap"])
        == 0
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "registry.jsonl").read_text().splitlines()
    ]
    revive = events[-2]
    supersede = events[-1]
    assert revive["revived"] is True and revive["uuid"] == retired_uuid
    assert supersede["supersedes"] == retired_uuid
    assert supersede["uuid"] == replacement
    # The staged file reloads cleanly and the catalog checks green.
    bsc.UuidRegistry.load(tmp_path / "registry.jsonl")
    assert bsc.main(argv + ["--check"]) == 0


def test_identity_uuid_map_matches_reviewed_anchor() -> None:
    """The registry's introduction commit cannot be continuity-checked by
    the append-only gate (there is no prior registry to extend), so the
    identity->uuid map verified by the adversarial review of PR #128 is
    pinned here. Changing ANY of the 201 bindings — or adding/retiring
    one — must edit this constant in the same diff, making wholesale
    remints impossible to slip through as regeneration noise.

    Update the constant only alongside registry events that justify it.
    """
    import hashlib

    catalog = json.loads(bsc.CATALOG.read_text(encoding="utf-8"))
    lines = sorted(
        json.dumps(
            [
                row["uuid"],
                row["concept"],
                bsc._geo_key(row.get("geography")),
                bsc._entity_key(row.get("entity")),
            ]
        )
        for row in catalog["series"]
    )
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert digest == (
        "2d2552e88662eb654597999ac92f750cadbed882703324c6f9ca1f1e3ac15345"
    )


def test_supersede_never_recycles_a_historical_uuid(
    tmp_path: pathlib.Path,
) -> None:
    # Fourth-review repro: U1 -> U2 -> U1 restored the original map while
    # hiding the excursion in two "valid" events.
    path = tmp_path / "registry.jsonl"
    mint = _mint("a.one", U1)
    away = dict(_mint("a.one", U2), supersedes=U1, note="planned change one")
    back = dict(_mint("a.one", U1), supersedes=U2, note="planned change two")
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in (mint, away, back)),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="recycles uuid"):
        bsc.UuidRegistry.load(path)


def test_succeeds_lineage_is_consumed_once_and_dimension_bound(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "registry.jsonl"
    mint = _mint("a.one", U1)
    retire = dict(_mint("a.one", U1), retired=True, note="placeholder done")
    succ = {"concept": "a.one", "geography": None, "entity": None}

    def entry(concept, uuid, **kw):
        return dict(_mint(concept, uuid), **kw)

    # Fork: two successors of one predecessor.
    b = entry("a.one", U1, succeeds=succ,
              entity={"name": "economy", "role": "aggregate"})
    b_away = dict(
        entry("a.one", U2, entity={"name": "economy", "role": "aggregate"}),
        supersedes=U1, note="moved along again",
    )
    c = entry("a.one", U1, succeeds=succ,
              entity={"name": "person", "role": "aggregate"})
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in (mint, retire, b, b_away, c)),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="consumed exactly once"):
        bsc.UuidRegistry.load(path)

    # Cross-concept lineage transfer.
    other = entry("b.two", U1, succeeds=succ)
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in (mint, retire, other)),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="never crosses concepts"):
        bsc.UuidRegistry.load(path)

    # Predecessor with a known entity is not a placeholder.
    known = _mint("a.one", U1,
                  entity={"name": "economy", "role": "aggregate"})
    known_retire = dict(known, retired=True, note="placeholder done")
    successor = entry(
        "a.one", U1,
        succeeds={"concept": "a.one", "geography": None,
                  "entity": {"name": "economy", "role": "aggregate"}},
    )
    path.write_text(
        "".join(
            json.dumps(e) + "\n" for e in (known, known_retire, successor)
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="entity-less placeholders"):
        bsc.UuidRegistry.load(path)


def test_literal_placeholder_segment_is_reserved(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(SystemExit, match="reserved placeholder"):
        _build(tmp_path, [_row("agency.statute.{P}.rate")])
    with pytest.raises(SystemExit, match="reserved placeholder"):
        _build(
            tmp_path,
            [_row("agency.rate", rid="agency.rate.{P}.first_print")],
        )


@pytest.mark.parametrize(
    "segment",
    ["fy0000", "fy0001", "0000_01", "week_ending_0001_01_01", "q1_0000"],
)
def test_out_of_window_calendar_tokens_neither_strip_nor_crash(
    segment: str,
) -> None:
    assert bsc.parse_period_token(segment) is None
    pattern = bsc.family_pattern(
        f"agency.rate.{segment}", {"type": "month", "value": "2026-05"}
    )
    assert pattern == f"agency.rate.{segment}"


def test_bare_year_variant_strips_only_for_annual_rows(
    tmp_path: pathlib.Path,
) -> None:
    rows = [
        _row("agency.annual.total.2025",
             rid="agency.annual.total.2025.final",
             period={"type": "year", "value": "2025"}),
        _row("agency.annual.total.2026",
             rid="agency.annual.total.2026.final",
             period={"type": "year", "value": "2026"}),
    ]
    catalog, _ = _build(tmp_path, rows)
    assert [r["concept"] for r in catalog["series"]] == [
        "agency.annual.total"
    ]
    assert catalog["series"][0]["observation_count"] == 2
    # A bare year that is NOT the row's own period never strips.
    pattern = bsc.family_pattern(
        "agency.annual.total.2019", {"type": "year", "value": "2025"}
    )
    assert pattern == "agency.annual.total.2019"


def test_consumed_lineage_is_terminal(tmp_path: pathlib.Path) -> None:
    # Fifth-review repro: retire A -> B succeeds A -> B moves to U2 ->
    # retire B -> revive A restored the banned U1 excursion via detour.
    path = tmp_path / "registry.jsonl"
    succ = {"concept": "a.one", "geography": None, "entity": None}
    events = [
        _mint("a.one", U1),
        dict(_mint("a.one", U1), retired=True, note="placeholder done"),
        dict(_mint("a.one", U1), succeeds=succ,
             entity={"name": "economy", "role": "aggregate"}),
        dict(_mint("a.one", U2,
                   entity={"name": "economy", "role": "aggregate"}),
             supersedes=U1, note="moved along deliberately"),
        dict(_mint("a.one", U2,
                   entity={"name": "economy", "role": "aggregate"}),
             retired=True, note="successor done too"),
        dict(_mint("a.one", U1), revived=True),
    ]
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="terminal"):
        bsc.UuidRegistry.load(path)


def test_builder_mints_fresh_for_consumed_identity(
    tmp_path: pathlib.Path,
) -> None:
    # Observations reappearing under a handed-over identity are a NEW
    # series claim: fresh uuid, no revival of the terminal lineage.
    succ = {"concept": "census.m3.new_orders", "geography": None,
            "entity": None}
    entries = [
        _mint("census.m3.new_orders", U1),
        dict(_mint("census.m3.new_orders", U1), retired=True,
             note="placeholder enriched"),
        dict(_mint("census.m3.new_orders", U1), succeeds=succ,
             geography={"level": "country", "id": "0100000US",
                        "vintage": "current"},
             entity={"name": "economy", "role": "aggregate"}),
    ]
    rows = [
        _row("census.m3.new_orders"),
        _row("census.m3.new_orders",
             geography=None if False else {"level": "country", "id": "GB",
                                           "vintage": "current"},
             rid="census.m3.new_orders.gb.first_print"),
    ]
    catalog, plan = _build(tmp_path, rows, registry_entries=entries)
    us_row = next(
        r for r in catalog["series"]
        if (r["geography"] or {}).get("id") == "0100000US"
    )
    gb_row = next(
        r for r in catalog["series"]
        if (r["geography"] or {}).get("id") == "GB"
    )
    assert us_row["uuid"] == U1  # the enriched successor identity
    assert gb_row["uuid"] not in (U1, U2)  # fresh, never the old lineage
    assert len(plan["revives"]) == 0


def test_live_uuid_uniqueness_holds_per_event_prefix(
    tmp_path: pathlib.Path,
) -> None:
    # Two live holders of one uuid mid-sequence must fail even if a later
    # event would "fix" the final state.
    path = tmp_path / "registry.jsonl"
    events = [
        _mint("a.one", U1),
        _mint("a.two", U2),
        # a.two jumps onto U1 while a.one still holds it live...
        dict(_mint("a.two", U1), supersedes=U2, note="deliberate theft"),
        # ...and a.one is retired only afterwards.
        dict(_mint("a.one", U1), retired=True, note="too late to matter"),
    ]
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="after every event"):
        bsc.UuidRegistry.load(path)


def test_reserved_placeholder_rejected_in_docket_and_registry(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(SystemExit, match="reserved placeholder"):
        _build(
            tmp_path,
            [_row("agency.rate")],
            docket={"series": [{"series": "agency.{P}.rate",
                                "cadence": "monthly"}]},
        )
    path = tmp_path / "registry.jsonl"
    path.write_text(
        json.dumps(_mint("agency.{P}.rate", U1)) + "\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="reserved placeholder"):
        bsc.UuidRegistry.load(path)


def test_registry_rejects_json_constants_and_empty_dimensions(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "registry.jsonl"
    nan_line = (
        '{"concept": "a.one", "geography": {"level": "country", '
        f'"id": "US", "vintage": NaN}}, "entity": null, "uuid": "{U1}"}}'
    )
    path.write_text(nan_line + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not strict JSON"):
        bsc.UuidRegistry.load(path)
    empty_vintage = _mint(
        "a.one", U1,
        geography={"level": "country", "id": "US", "vintage": ""},
    )
    path.write_text(json.dumps(empty_vintage) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="nonempty string or null"):
        bsc.UuidRegistry.load(path)


def test_observation_dimension_domains_are_enforced(
    tmp_path: pathlib.Path,
) -> None:
    bad = _row("agency.rate")
    bad["geography"] = {"level": "country", "id": "0100000US", "vintage": ""}
    with pytest.raises(SystemExit, match="nonempty string or null"):
        _build(tmp_path, [bad])


@pytest.mark.parametrize(
    ("identifier", "period"),
    [
        ("agency.rate.january_1899", MONTH_2026_06),
        ("agency.rate.january_3000", MONTH_2026_06),
    ],
)
def test_boundary_year_month_names_neither_strip_nor_crash(
    identifier: str, period: dict
) -> None:
    assert bsc.family_pattern(identifier, period) == identifier


@pytest.mark.parametrize("value", ["1899-01", "3000-01"])
def test_out_of_window_quarter_periods_are_malformed(
    tmp_path: pathlib.Path, value: str
) -> None:
    with pytest.raises(SystemExit, match="malformed period"):
        _build(
            tmp_path,
            [_row("agency.rate", period={"type": "quarter", "value": value})],
        )


@pytest.mark.parametrize("segment", ["1899_13", "2999_13", "3000_13"])
def test_out_of_window_impossible_tokens_are_flagged(segment: str) -> None:
    pattern = bsc.family_pattern(f"agency.rate.{segment}", MONTH_2026_06)
    assert pattern == f"agency.rate.{segment}"
    assert bsc.suspect_segments(pattern) == [segment]


def test_reclaimed_mint_grammar(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "registry.jsonl"
    succ = {"concept": "a.one", "geography": None, "entity": None}
    base = [
        _mint("a.one", U1),
        dict(_mint("a.one", U1), retired=True, note="placeholder done"),
        dict(_mint("a.one", U1), succeeds=succ,
             entity={"name": "economy", "role": "aggregate"}),
    ]
    fresh = "dddddddd-4444-4444-8444-444444444444"
    good = dict(_mint("a.one", fresh), reclaimed=True,
                note="re-established after handover")
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in base + [good]),
        encoding="utf-8",
    )
    registry = bsc.UuidRegistry.load(path)
    assert registry.binding(("a.one", bsc._geo_key(None),
                             bsc._entity_key(None))) == fresh
    # Reclaim of a merely-retired (not consumed) key is invalid.
    unconsumed = [
        _mint("b.two", U2),
        dict(_mint("b.two", U2), retired=True, note="ordinary retirement"),
        dict(_mint("b.two", fresh), reclaimed=True,
             note="not a handover case"),
    ]
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in unconsumed), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="not handed over"):
        bsc.UuidRegistry.load(path)
    # Reclaim must mint fresh, never reuse.
    reuse = dict(_mint("a.one", U1), reclaimed=True,
                 note="tries to take U1 back")
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in base + [reuse]),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="mints fresh"):
        bsc.UuidRegistry.load(path)


def test_consumed_key_reclaims_fresh_lineage(tmp_path: pathlib.Path) -> None:
    # Sixth-review repro: a docket entry re-forming a handed-over key must
    # take an explicit reclaim path (previously it staged a markerless
    # mint that its own validator rejected).
    succ = {"concept": "census.m3.new_orders", "geography": None,
            "entity": None}
    entries = [
        _mint("census.m3.new_orders", U1),
        dict(_mint("census.m3.new_orders", U1), retired=True,
             note="placeholder enriched"),
        dict(_mint("census.m3.new_orders", U1), succeeds=succ,
             geography={"level": "country", "id": "0100000US",
                        "vintage": "current"},
             entity={"name": "economy", "role": "aggregate"}),
        _mint("bls.cps.unemployment_rate", U2,
              geography={"level": "country", "id": "0100000US",
                         "vintage": "current"},
              entity={"name": "economy", "role": "aggregate"}),
    ]
    existing = {
        "series": [
            {
                "uuid": U2,
                "concept": "bls.cps.unemployment_rate",
                "geography": dict(US),
                "entity": {"name": "economy", "role": "aggregate"},
                "aliases": [],
                "status": "observed",
            }
        ]
    }
    observations = tmp_path / "obs.jsonl"
    observations.write_text(
        json.dumps(_row("bls.cps.unemployment_rate")) + "\n",
        encoding="utf-8",
    )
    docket_path = tmp_path / "seed.json"
    docket_path.write_text(
        json.dumps({"series": [{"series": "census.m3.new_orders",
                                "cadence": "monthly"}]}),
        encoding="utf-8",
    )
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(existing) + "\n", encoding="utf-8")
    registry = _registry(tmp_path, entries)
    catalog, plan = bsc.build_catalog(
        observations, docket_path, bsc.ExistingCatalog(catalog_path), registry
    )
    docket_row = next(
        r for r in catalog["series"] if r["status"] == "docket-only"
    )
    assert docket_row["uuid"] != U1  # fresh lineage, old uuid stays put
    reclaim = next(e for e in plan["mints"] if e.get("reclaimed"))
    assert reclaim["uuid"] == docket_row["uuid"]
    # The enriched identity has no row in this synthetic catalog, so its
    # live binding is a gated retire — and the staged whole must reload.
    staged = registry.stage(
        plan["enrich_retires"] + plan["mints"] + plan["revives"]
        + plan["supersedes"]
        + [dict(e, note="synthetic gate approval")
           for e in plan["retire_pending"]]
    )
    assert staged.entries[-1] is not None


def test_docket_seed_rejects_json_constants(tmp_path: pathlib.Path) -> None:
    argv = _repo(tmp_path, [_row("bls.cps.unemployment_rate")])
    (tmp_path / "seed.json").write_text(
        '{"series": [{"series": "a.b", "cadence": "monthly", '
        '"extras": {"valueScale": NaN}}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not allowed"):
        bsc.main(argv)


def test_observation_duplicate_members_rejected(
    tmp_path: pathlib.Path,
) -> None:
    row = _row("agency.rate")
    line = json.dumps(row)
    line = line.replace(
        '"geography": {', '"geography": {"level": "state", ', 1
    )
    observations = tmp_path / "obs.jsonl"
    observations.write_text(
        line.replace('"geography": {"level": "state", "level"',
                     '"geography": {"level": "state", "level"') + "\n",
        encoding="utf-8",
    )
    registry = _registry(tmp_path)
    with pytest.raises(ValueError, match="duplicate JSON member"):
        bsc.build_catalog(
            observations, None,
            bsc.ExistingCatalog(tmp_path / "catalog.json"), registry,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("geography", ""), ("geography", []), ("entity", []), ("entity", "x")],
)
def test_falsey_or_nonobject_dimensions_rejected(
    tmp_path: pathlib.Path, field: str, value
) -> None:
    bad = _row("agency.rate")
    bad[field] = value
    with pytest.raises(SystemExit, match="must be an object or null"):
        _build(tmp_path, [bad])


def test_succeeds_schema_is_enforced(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "registry.jsonl"
    base = [
        _mint("a.one", U1),
        dict(_mint("a.one", U1), retired=True, note="placeholder done"),
    ]
    forged = dict(
        _mint("a.one", U1, entity={"name": "economy", "role": "aggregate"}),
        succeeds={"concept": "a.one", "geography": None, "entity": None,
                  "extra": "field"},
    )
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in base + [forged]),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="non-identity fields"):
        bsc.UuidRegistry.load(path)
    bad_geo = dict(
        _mint("a.one", U1, entity={"name": "economy", "role": "aggregate"}),
        succeeds={"concept": "a.one",
                  "geography": {"level": "country", "id": ""},
                  "entity": None},
    )
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in base + [bad_geo]),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="nonempty string or null"):
        bsc.UuidRegistry.load(path)


def test_registry_rejects_hidden_line_separators(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "registry.jsonl"
    two_on_one = (
        json.dumps(_mint("a.one", U1))
        + " "
        + json.dumps(_mint("a.two", U2))
    )
    path.write_text(two_on_one + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not strict JSON"):
        bsc.UuidRegistry.load(path)
    with_vt = (
        json.dumps(_mint("a.one", U1))
        + "\x0b"
        + json.dumps(_mint("a.two", U2))
    )
    path.write_bytes(with_vt.encode("utf-8") + b"\n")
    with pytest.raises(SystemExit, match="not strict JSON"):
        bsc.UuidRegistry.load(path)
