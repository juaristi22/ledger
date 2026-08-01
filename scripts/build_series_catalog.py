#!/usr/bin/env python3
"""Build ledger/series_catalog.json — the canonical series registry.

The observation file records facts; this catalog records the SERIES those
facts belong to, one row per family, keyed by a UUID that is minted exactly
once and preserved across regenerations. Consumers (Thesis's docket, bill
mappers, permalink surfaces) refer to series by catalog UUID or canonical
concept and never mint parallel identities.

Family derivation is deterministic: strip period tokens (and nothing else)
from ``source_record_id`` and ``measure.concept``, replacing each with a
``{P}`` placeholder. Release-vintage segments such as ``first_print`` or
``third_estimate`` are preserved — collapsing across vintages is a curation
judgment, done by hand-merging catalog rows (the surviving row keeps its
UUID; absorbed spellings move into ``aliases``). The same applies to
concept-spelling drift (e.g. ``abs.cpi.all_groups.yoy`` vs
``abs.cpi_indicator.allgroups.yoy``): the generator never merges distinct
spellings mechanically.

Recognized period tokens (dotted segments):
  fy2026 | 2026-05 | may_2026 | q1_2026 | 2026_q1 | week_2026-05-02

Usage:
  python3 scripts/build_series_catalog.py                    # regenerate
  python3 scripts/build_series_catalog.py --docket PATH      # seed docket-only rows
  python3 scripts/build_series_catalog.py --check            # verify committed file is current

Families whose post-strip concept is identical (patterns differing only in
``{P}`` placement, e.g. ``bls.cps.unemployment_rate`` observed both bare and
period-suffixed) are the same series and merge into one row listing every
observed pattern.

Idempotent: same observations + same existing catalog -> byte-identical
output. New series mint fresh UUIDv4s; existing series keep theirs
(looked up by ``concept``, the stable identity key).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import uuid
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
OBSERVATIONS = ROOT / "ledger" / "official_observations.jsonl"
CATALOG = ROOT / "ledger" / "series_catalog.json"

GENERATOR_VERSION = 1

MONTHS = [
    "",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

# Docket cadence words -> ledger period types.
CADENCE_TO_PERIOD_TYPE = {
    "weekly": "week_ending",
    "monthly": "month",
    "quarterly": "quarter",
    "annual": "year",
    "fiscal_year": "fiscal_year",
}

_PERIOD_SEGMENT = re.compile(
    r"^(?:"
    r"fy\d{4}"                      # fy2026
    r"|\d{4}-\d{2}(?:-\d{2})?"      # 2026-05, 2026-05-02
    r"|(?:%s)_\d{4}"                # may_2026
    r"|q[1-4]_\d{4}"                # q1_2026
    r"|\d{4}_q[1-4]"                # 2026_q1
    r"|week_\d{4}-\d{2}-\d{2}"      # week_2026-05-02
    r")$" % "|".join(m for m in MONTHS if m)
)


def is_period_segment(segment: str) -> bool:
    """Whether one dotted segment is a period token."""
    return bool(_PERIOD_SEGMENT.fullmatch(segment))


def family_pattern(identifier: str) -> str:
    """Replace every period-token segment with ``{P}``.

    >>> family_pattern("bls.eci.private_wages_salaries_qoq.2026_q2.first_print")
    'bls.eci.private_wages_salaries_qoq.{P}.first_print'
    >>> family_pattern("abs.labour.employment_change.australia.june_2026")
    'abs.labour.employment_change.australia.{P}'
    >>> family_pattern("abs.cpi.all_groups.yoy")
    'abs.cpi.all_groups.yoy'
    >>> family_pattern("usda.fsa.snap.participation.fy2026.october_2025")
    'usda.fsa.snap.participation.{P}.{P}'
    """
    segments = identifier.split(".")
    return ".".join("{P}" if is_period_segment(s) else s for s in segments)


def concept_for(pattern: str) -> str:
    """The human-facing canonical concept: the pattern minus its placeholders.

    >>> concept_for("bls.eci.private_wages_salaries_qoq.{P}.first_print")
    'bls.eci.private_wages_salaries_qoq.first_print'
    >>> concept_for("abs.cpi.all_groups.yoy")
    'abs.cpi.all_groups.yoy'
    """
    return ".".join(s for s in pattern.split(".") if s != "{P}")


def _modal(counter: Counter) -> tuple[object, list]:
    """Most common value plus the sorted list of variants (if more than one)."""
    if not counter:
        return None, []
    ranked = counter.most_common()
    modal = ranked[0][0]
    variants = sorted(str(v) for v, _ in ranked)
    return modal, variants if len(ranked) > 1 else []


def build_families(rows: list[dict]) -> dict[str, dict]:
    """Group observation rows into families keyed by family_pattern."""
    families: dict[str, dict] = {}
    for row in rows:
        rid = row.get("source_record_id")
        measure = row.get("measure") or {}
        concept_raw = measure.get("concept")
        if not isinstance(rid, str) or not isinstance(concept_raw, str):
            raise SystemExit(
                "observation row missing source_record_id or measure.concept: "
                f"{json.dumps(row)[:200]}"
            )
        pattern = family_pattern(concept_raw)
        fam = families.setdefault(
            pattern,
            {
                "concepts": set(),
                "source_concepts": set(),
                "rid_patterns": set(),
                "units": Counter(),
                "period_types": Counter(),
                "geographies": Counter(),
                "entities": Counter(),
                "sources": set(),
                "period_values": [],
                "count": 0,
            },
        )
        fam["concepts"].add(concept_raw)
        source_concept = measure.get("source_concept")
        if isinstance(source_concept, str):
            fam["source_concepts"].add(source_concept)
        fam["rid_patterns"].add(family_pattern(rid))
        fam["units"][measure.get("unit")] += 1
        period = row.get("period") or {}
        fam["period_types"][period.get("type")] += 1
        geography = row.get("geography") or {}
        fam["geographies"][
            json.dumps(
                {k: geography.get(k) for k in ("level", "id", "name")},
                sort_keys=True,
            )
        ] += 1
        entity = row.get("entity") or {}
        fam["entities"][json.dumps(entity, sort_keys=True)] += 1
        source = row.get("source") or {}
        if isinstance(source.get("source_name"), str):
            fam["sources"].add(source["source_name"])
        if period.get("value") is not None:
            fam["period_values"].append(str(period["value"]))
        fam["count"] += 1
    return families


def load_existing_uuids(path: pathlib.Path) -> dict[str, str]:
    """concept -> uuid from the committed catalog, if present."""
    if not path.exists():
        return {}
    catalog = json.loads(path.read_text(encoding="utf-8"))
    return {row["concept"]: row["uuid"] for row in catalog.get("series", [])}


def build_catalog(
    observations_path: pathlib.Path,
    docket_path: pathlib.Path | None,
    existing_uuids: dict[str, str],
) -> dict:
    raw = observations_path.read_bytes()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    families = build_families(rows)

    # Merge families whose post-strip concept is identical: patterns that
    # differ only in {P} placement are one series observed under two period
    # formattings, not two series.
    by_concept_key: dict[str, dict] = {}
    for pattern in sorted(families):
        fam = families[pattern]
        key = concept_for(pattern)
        merged = by_concept_key.setdefault(
            key,
            {
                "patterns": set(),
                "concepts": set(),
                "source_concepts": set(),
                "rid_patterns": set(),
                "units": Counter(),
                "period_types": Counter(),
                "geographies": Counter(),
                "entities": Counter(),
                "sources": set(),
                "period_values": [],
                "count": 0,
            },
        )
        merged["patterns"].add(pattern)
        for field in ("concepts", "source_concepts", "rid_patterns", "sources"):
            merged[field] |= fam[field]
        for field in ("units", "period_types", "geographies", "entities"):
            merged[field] += fam[field]
        merged["period_values"] += fam["period_values"]
        merged["count"] += fam["count"]

    series: list[dict] = []
    for key in sorted(by_concept_key):
        fam = by_concept_key[key]
        unit, unit_variants = _modal(fam["units"])
        period_type, period_variants = _modal(fam["period_types"])
        geography_json, _ = _modal(fam["geographies"])
        entity_json, _ = _modal(fam["entities"])
        row = {
            "uuid": existing_uuids.get(key) or str(uuid.uuid4()),
            "concept": key,
            "family_patterns": sorted(fam["patterns"]),
            "status": "observed",
            "unit": unit,
            "cadence": period_type,
            "geography": json.loads(geography_json) if geography_json else None,
            "entity": json.loads(entity_json) if entity_json else None,
            "sources": sorted(fam["sources"]),
            "aliases": sorted(
                (fam["concepts"] | fam["source_concepts"]) - {key}
            ),
            "rid_patterns": sorted(fam["rid_patterns"]),
            "first_observed_period": min(fam["period_values"], default=None),
            "last_observed_period": max(fam["period_values"], default=None),
            "observation_count": fam["count"],
        }
        if unit_variants:
            row["unit_variants"] = unit_variants
        if period_variants:
            row["cadence_variants"] = period_variants
        series.append(row)

    if docket_path is not None:
        docket = json.loads(docket_path.read_text(encoding="utf-8"))
        by_concept = {row["concept"]: row for row in series}
        alias_index = {
            alias: row for row in series for alias in row["aliases"]
        }
        for entry in docket["series"]:
            concept = entry["series"]
            cadence_word = entry.get("cadence")
            if cadence_word not in CADENCE_TO_PERIOD_TYPE:
                raise SystemExit(
                    f"docket cadence {cadence_word!r} for {concept} has no "
                    "period-type mapping; extend CADENCE_TO_PERIOD_TYPE"
                )
            target_unit = (entry.get("extras") or {}).get("targetUnit")
            hit = by_concept.get(concept) or alias_index.get(concept)
            if hit is not None:
                if concept != hit["concept"] and concept not in hit["aliases"]:
                    hit["aliases"] = sorted(hit["aliases"] + [concept])
                if hit["unit"] is None and target_unit is not None:
                    hit["unit"] = target_unit
                continue
            row = {
                "uuid": existing_uuids.get(concept) or str(uuid.uuid4()),
                "concept": concept,
                "family_patterns": [family_pattern(concept)],
                "status": "docket-only",
                "unit": target_unit,
                "cadence": CADENCE_TO_PERIOD_TYPE[cadence_word],
                "geography": None,
                "entity": None,
                "sources": [],
                "aliases": [],
                "rid_patterns": [],
                "first_observed_period": None,
                "last_observed_period": None,
                "observation_count": 0,
            }
            series.append(row)
            by_concept[concept] = row

    series.sort(key=lambda row: row["concept"])
    concepts = [row["concept"] for row in series]
    if len(concepts) != len(set(concepts)):
        dupes = sorted({c for c in concepts if concepts.count(c) > 1})
        raise SystemExit(f"duplicate concepts in catalog output: {dupes}")
    return {
        "comment": (
            "Canonical series catalog. One row per series family; uuid is "
            "minted once and never re-minted (regeneration preserves it by "
            "concept). Consumers reference series by uuid or concept "
            "only. Regenerate with scripts/build_series_catalog.py; verify "
            "with --check. Cross-spelling merges are manual curation: keep "
            "the surviving row's uuid, move absorbed spellings to aliases."
        ),
        "generator_version": GENERATOR_VERSION,
        "observations_sha256": hashlib.sha256(raw).hexdigest(),
        "observation_rows": len(rows),
        "series": series,
    }


def render(catalog: dict) -> str:
    return json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=pathlib.Path, default=OBSERVATIONS)
    parser.add_argument("--catalog", type=pathlib.Path, default=CATALOG)
    parser.add_argument(
        "--docket",
        type=pathlib.Path,
        default=None,
        help="optional Thesis docket_series.json to seed docket-only rows",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed catalog is not current for these inputs",
    )
    args = parser.parse_args(argv)

    existing = load_existing_uuids(args.catalog)
    catalog = build_catalog(args.observations, args.docket, existing)
    body = render(catalog)

    if args.check:
        current = args.catalog.read_text(encoding="utf-8") if args.catalog.exists() else ""
        if current != body:
            sys.stderr.write(
                "series_catalog.json is stale for these inputs; regenerate "
                "with scripts/build_series_catalog.py\n"
            )
            return 1
        print(f"catalog current: {len(catalog['series'])} series")
        return 0

    args.catalog.write_text(body, encoding="utf-8")
    observed = sum(1 for r in catalog["series"] if r["status"] == "observed")
    docket_only = sum(1 for r in catalog["series"] if r["status"] == "docket-only")
    print(
        f"wrote {args.catalog}: {len(catalog['series'])} series "
        f"({observed} observed, {docket_only} docket-only)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
