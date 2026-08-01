"""Regression tests for the series-catalog generator.

The token-grammar cases come from the 2026-08-01 adversarial review of the
first generator: every live period spelling it found unrecognized, plus the
non-period identifiers it confirmed must never be stripped.
"""

from __future__ import annotations

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


def test_semantic_pass_strips_unseen_spelling() -> None:
    # A spelling the grammar does not know, derivable from the row period.
    period = {"type": "month", "value": "2026-06"}
    assert (
        bsc.family_pattern("agency.rate.june_2026", period)
        == "agency.rate.{P}"
    )


def test_suspect_segments_flag_but_never_strip() -> None:
    pattern = bsc.family_pattern("agency.series.mid2026wave")
    assert pattern == "agency.series.mid2026wave"
    assert bsc.suspect_segments(pattern) == ["mid2026wave"]


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
        "geography": geography or {"level": "country", "id": "0100000US"},
        "entity": entity or {"name": "economy", "role": "aggregate"},
        "measure": {
            "concept": concept,
            "unit": unit,
            **({"source_concept": source_concept} if source_concept else {}),
        },
        "source": {"source_name": "test"},
        "source_record_id": rid or f"{concept}.first_print",
    }


def _build(tmp_path: pathlib.Path, rows: list[dict], existing: dict | None = None):
    observations = tmp_path / "obs.jsonl"
    observations.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    catalog_path = tmp_path / "catalog.json"
    if existing is not None:
        catalog_path.write_text(json.dumps(existing) + "\n", encoding="utf-8")
    catalog = bsc.build_catalog(
        observations, None, bsc.ExistingCatalog(catalog_path)
    )
    return catalog


def test_geography_splits_identity(tmp_path: pathlib.Path) -> None:
    rows = [
        _row("fns.snap.error_rate"),
        _row(
            "fns.snap.error_rate",
            geography={"level": "state", "id": "0400000US06"},
        ),
    ]
    catalog = _build(tmp_path, rows)
    assert len(catalog["series"]) == 2
    ids = {(r["geography"] or {}).get("id") for r in catalog["series"]}
    assert ids == {"0100000US", "0400000US06"}


def test_rename_inherits_uuid_via_alias(tmp_path: pathlib.Path) -> None:
    first = _build(tmp_path, [_row("bls.cps.unemployment_rate")])
    (tmp_path / "catalog.json").write_text(bsc.render(first), encoding="utf-8")
    old_uuid = first["series"][0]["uuid"]
    renamed = _build(
        tmp_path,
        [_row("bls.cps.jobless_rate", source_concept="bls.cps.unemployment_rate")],
        existing=first,
    )
    assert renamed["series"][0]["uuid"] == old_uuid


def test_curated_alias_persists_and_inherits(tmp_path: pathlib.Path) -> None:
    first = _build(tmp_path, [_row("abs.cpi.all_groups.yoy")])
    row = first["series"][0]
    row["aliases"] = sorted(row["aliases"] + ["abs.cpi_indicator.allgroups.yoy"])
    merged = _build(
        tmp_path,
        [_row("abs.cpi_indicator.allgroups.yoy")],
        existing=first,
    )
    assert merged["series"][0]["uuid"] == row["uuid"]
    assert "abs.cpi_indicator.allgroups.yoy" in merged["series"][0]["aliases"]


def test_ambiguous_alias_match_is_a_hard_error(tmp_path: pathlib.Path) -> None:
    existing = {
        "series": [
            {
                "uuid": "11111111-1111-4111-8111-111111111111",
                "concept": "a.one",
                "geography": None,
                "entity": None,
                "aliases": ["SHARED"],
            },
            {
                "uuid": "22222222-2222-4222-8222-222222222222",
                "concept": "a.two",
                "geography": None,
                "entity": None,
                "aliases": ["SHARED"],
            },
        ]
    }
    with pytest.raises(SystemExit, match="multiple existing UUIDs"):
        _build(
            tmp_path,
            [_row("a.three", source_concept="SHARED")],
            existing=existing,
        )


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


def test_uuid_validation_catches_bad_and_duplicate() -> None:
    catalog = {
        "series": [
            {"uuid": "not-a-uuid", "concept": "a"},
            {"uuid": "33333333-3333-4333-8333-333333333333", "concept": "b"},
            {"uuid": "33333333-3333-4333-8333-333333333333", "concept": "c"},
        ]
    }
    problems = bsc.validate_uuids(catalog)
    assert any("does not parse" in p for p in problems)
    assert any("duplicated" in p for p in problems)


def test_committed_catalog_is_current_and_valid() -> None:
    """The committed artifact must regenerate byte-identically (seeded) and
    carry unique, parseable UUIDv4s."""
    committed = bsc.CATALOG.read_text(encoding="utf-8")
    catalog = bsc.build_catalog(
        bsc.OBSERVATIONS, bsc.DOCKET_SEED, bsc.ExistingCatalog(bsc.CATALOG)
    )
    assert bsc.render(catalog) == committed
    assert bsc.validate_uuids(json.loads(committed)) == []
    assert json.loads(committed)["suspect_segments"] == []
