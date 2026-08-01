#!/usr/bin/env python3
"""Build ledger/series_catalog.json — the canonical series registry.

The observation file records facts; this catalog records the SERIES those
facts belong to, one row per (concept, geography, entity) identity — the
same identity dimensions the fact ADR uses — keyed by a UUID that is minted
exactly once and preserved across regenerations. Consumers (Thesis's docket,
bill mappers, permalink surfaces) refer to series by catalog UUID or concept
and never mint parallel identities.

Family derivation strips period tokens (and nothing else) from
``source_record_id`` and ``measure.concept``, replacing each with ``{P}``.
Two passes strip a segment: a shape pass (the recognized token grammar
below) and a semantic pass (tokens derived from the row's own ``period``,
which catches spellings the grammar has not met yet). Segments that still
contain a four-digit year after both passes are reported in
``suspect_segments`` for curation — flagged, never silently stripped.

Recognized token grammar (dotted segments):
  fy2026 | 2026-05 | 2026_05 | 2026-05-02 | 2026_05_02 | may_2026 | feb_2026
  | q1_2026 | 2026_q1 | week_2026-05-02 | week_2026_05_02
  | week_ending_2026_05_02 | february_to_april_2026
  | after_june_2026 | after_mpc_june_2026   (period-qualifier compounds)

Release-vintage segments (``first_print``, ``third_estimate``,
``original_submission``) are preserved: collapsing across vintages is a
curation judgment, done by hand-merging catalog rows (the surviving row
keeps its UUID; absorbed spellings move into ``aliases``). The same applies
to concept-spelling drift (``abs.cpi.all_groups.yoy`` vs
``abs.cpi_indicator.allgroups.yoy``): never merged mechanically. Curated
aliases persist across regeneration — the existing catalog's aliases are
unioned with derived ones, and alias matches inherit the existing UUID, so
a hand merge or an upstream concept rename does not remint identity.

Inputs are pinned: the observation JSONL (digest in ``observations_sha256``)
and the committed docket seed at ``ledger/seeds/thesis_docket_series.json``
(digest in ``docket_seed_sha256``), which contributes docket-only rows for
Thesis docket series not yet observed. Bare ``--check`` uses both committed
inputs, so CI needs no external files.

Idempotent: same inputs + same existing catalog -> byte-identical output.
New identities mint fresh UUIDv4s; existing identities keep theirs (looked
up by identity key, then by concept/alias). Unit or cadence conflicts
within one identity are a hard error, never a silent modal pick.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import uuid as uuid_module
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
OBSERVATIONS = ROOT / "ledger" / "official_observations.jsonl"
CATALOG = ROOT / "ledger" / "series_catalog.json"
DOCKET_SEED = ROOT / "ledger" / "seeds" / "thesis_docket_series.json"

GENERATOR_VERSION = 2

MONTHS_FULL = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTHS_ABBREV = [
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec",
]
_MONTH_ALT = "|".join(sorted(set(MONTHS_FULL + MONTHS_ABBREV), key=len, reverse=True))

# Docket cadence words -> ledger period types.
CADENCE_TO_PERIOD_TYPE = {
    "weekly": "week_ending",
    "monthly": "month",
    "quarterly": "quarter",
    "annual": "year",
    "fiscal_year": "fiscal_year",
}

# ISO country codes as used by observed non-US rows; US uses the Census id.
COUNTRY_GEOGRAPHY = {
    "US": {"level": "country", "id": "0100000US", "name": "United States"},
    "CA": {"level": "country", "id": "CA", "name": None},
    "GB": {"level": "country", "id": "GB", "name": None},
    "AU": {"level": "country", "id": "AU", "name": None},
    "JP": {"level": "country", "id": "JP", "name": None},
    "BE": {"level": "country", "id": "BE", "name": None},
}

_PERIOD_SEGMENT = re.compile(
    r"^(?:after_(?:[a-z]+_)?)?"          # after_june_2026, after_mpc_june_2026
    r"(?:"
    r"fy\d{4}"                            # fy2026
    r"|\d{4}[-_]\d{2}(?:[-_]\d{2})?"      # 2026-05, 2026_05, 2026-05-02, 2026_05_02
    r"|(?:%(m)s)_\d{4}"                   # may_2026, feb_2026
    r"|(?:%(m)s)_to_(?:%(m)s)_\d{4}"      # february_to_april_2026
    r"|q[1-4]_\d{4}"                      # q1_2026
    r"|\d{4}_q[1-4]"                      # 2026_q1
    r"|week_(?:ending_)?\d{4}[-_]\d{2}[-_]\d{2}"  # week_2026-05-02, week_ending_2026_06_06
    r")$" % {"m": _MONTH_ALT}
)

_YEAR_HINT = re.compile(r"(?:19|20)\d{2}")


def is_period_segment(segment: str) -> bool:
    """Whether one dotted segment matches the period-token grammar.

    >>> [is_period_segment(s) for s in (
    ...     "2026_05", "2026-05", "feb_2026", "week_ending_2026_06_06",
    ...     "after_june_2026", "after_mpc_june_2026", "2026_06_18",
    ...     "february_to_april_2026", "week_2026-06-13", "fy2026", "2026_q2",
    ... )]
    [True, True, True, True, True, True, True, True, True, True, True]
    >>> [is_period_segment(s) for s in (
    ...     "36-10-0434-01", "g17", "adv44x72", "j5ii", "m3",
    ...     "first_print", "third_estimate", "original_submission",
    ...     "total_nonfarm_payroll_change", "australia",
    ... )]
    [False, False, False, False, False, False, False, False, False, False]
    """
    return bool(_PERIOD_SEGMENT.fullmatch(segment))


def period_token_variants(period: dict) -> set[str]:
    """Every spelling of ``period`` that may appear as an id segment."""
    ptype, value = period.get("type"), period.get("value")
    tokens: set[str] = set()
    if value is None:
        return tokens
    value = str(value)
    if ptype == "fiscal_year":
        tokens.add(f"fy{value}")
    elif ptype == "month":
        m = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if m:
            year, month = m.group(1), int(m.group(2))
            tokens.update({f"{year}-{month:02d}", f"{year}_{month:02d}"})
            tokens.add(f"{MONTHS_FULL[month - 1]}_{year}")
            tokens.add(f"{MONTHS_ABBREV[month - 1]}_{year}")
    elif ptype == "quarter":
        m = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if m:
            year, quarter = m.group(1), (int(m.group(2)) - 1) // 3 + 1
            tokens.update({f"q{quarter}_{year}", f"{year}_q{quarter}"})
    elif ptype == "week_ending":
        iso = value
        tokens.update({
            f"week_{iso}", f"week_{iso.replace('-', '_')}",
            f"week_ending_{iso}", f"week_ending_{iso.replace('-', '_')}",
        })
    return tokens


def family_pattern(identifier: str, period: dict | None = None) -> str:
    """Replace period-token segments with ``{P}``.

    >>> family_pattern("bls.eci.private_wages_salaries_qoq.2026_q2.first_print")
    'bls.eci.private_wages_salaries_qoq.{P}.first_print'
    >>> family_pattern("census.m3.durable_goods_new_orders_mom.2026_06")
    'census.m3.durable_goods_new_orders_mom.{P}'
    >>> family_pattern("boe.bank_rate.after_mpc_june_2026")
    'boe.bank_rate.{P}'
    >>> family_pattern("ons.labour.unemployment_rate.february_to_april_2026")
    'ons.labour.unemployment_rate.{P}'
    >>> family_pattern("abs.cpi.all_groups.yoy")
    'abs.cpi.all_groups.yoy'
    """
    derived = period_token_variants(period or {})
    segments = identifier.split(".")
    return ".".join(
        "{P}" if (is_period_segment(s) or s in derived) else s for s in segments
    )


def concept_for(pattern: str) -> str:
    """The human-facing canonical concept: the pattern minus placeholders."""
    return ".".join(s for s in pattern.split(".") if s != "{P}")


def suspect_segments(pattern: str) -> list[str]:
    """Post-strip segments that still smell of a date — flagged, not stripped."""
    return [
        s for s in pattern.split(".")
        if s != "{P}" and _YEAR_HINT.search(s)
    ]


def _geo_key(geography: dict | None) -> str:
    g = geography or {}
    return f"{g.get('level')}|{g.get('id')}"


def _entity_key(entity: dict | None) -> str:
    e = entity or {}
    return f"{e.get('name')}|{e.get('role')}"


def build_identities(rows: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Group observation rows by (concept, geography, entity) identity."""
    identities: dict[tuple[str, str, str], dict] = {}
    for index, row in enumerate(rows):
        rid = row.get("source_record_id")
        measure = row.get("measure") or {}
        concept_raw = measure.get("concept")
        if not isinstance(rid, str) or not isinstance(concept_raw, str):
            raise SystemExit(
                f"observation row {index} missing source_record_id or "
                f"measure.concept: {json.dumps(row)[:200]}"
            )
        period = row.get("period") or {}
        pattern = family_pattern(concept_raw, period)
        geography = row.get("geography") or None
        entity = row.get("entity") or None
        key = (concept_for(pattern), _geo_key(geography), _entity_key(entity))
        ident = identities.setdefault(
            key,
            {
                "patterns": set(),
                "concepts": set(),
                "source_concepts": set(),
                "rid_patterns": set(),
                "suspects": set(),
                "units": Counter(),
                "period_types": Counter(),
                "geography": geography,
                "entity": entity,
                "sources": set(),
                "period_values": [],
                "count": 0,
            },
        )
        ident["patterns"].add(pattern)
        ident["concepts"].add(concept_raw)
        source_concept = measure.get("source_concept")
        if isinstance(source_concept, str):
            ident["source_concepts"].add(source_concept)
        rid_pattern = family_pattern(rid, period)
        ident["rid_patterns"].add(rid_pattern)
        ident["suspects"].update(suspect_segments(pattern))
        ident["suspects"].update(suspect_segments(rid_pattern))
        ident["units"][measure.get("unit")] += 1
        ident["period_types"][period.get("type")] += 1
        source = row.get("source") or {}
        if isinstance(source.get("source_name"), str):
            ident["sources"].add(source["source_name"])
        if period.get("value") is not None:
            ident["period_values"].append(str(period["value"]))
        ident["count"] += 1
    return identities


def _sole(counter: Counter, what: str, key: tuple) -> object:
    """The single value in ``counter`` — conflicts are a hard error."""
    values = [v for v in counter if v is not None]
    if len(values) > 1:
        raise SystemExit(
            f"{what} conflict within identity {key}: {sorted(map(str, values))} "
            "— resolve by curation (split or correct upstream); the catalog "
            "never picks a modal winner"
        )
    return values[0] if values else None


class ExistingCatalog:
    """UUID and curated-alias memory from the committed catalog."""

    def __init__(self, path: pathlib.Path) -> None:
        self.by_identity: dict[tuple[str, str, str], dict] = {}
        self.by_concept: dict[str, list[dict]] = {}
        self.by_alias: dict[str, list[dict]] = {}
        if not path.exists():
            return
        catalog = json.loads(path.read_text(encoding="utf-8"))
        for row in catalog.get("series", []):
            key = (
                row["concept"],
                _geo_key(row.get("geography")),
                _entity_key(row.get("entity")),
            )
            self.by_identity[key] = row
            self.by_concept.setdefault(row["concept"], []).append(row)
            for alias in row.get("aliases", []):
                self.by_alias.setdefault(alias, []).append(row)

    def match(self, key: tuple[str, str, str], names: set[str]) -> dict | None:
        """The existing row for this identity: exact key, else unique name hit."""
        row = self.by_identity.get(key)
        if row is not None:
            return row
        hits: dict[str, dict] = {}
        for name in names:
            for row in self.by_concept.get(name, []) + self.by_alias.get(name, []):
                hits[row["uuid"]] = row
        if len(hits) == 1:
            return next(iter(hits.values()))
        if len(hits) > 1:
            raise SystemExit(
                f"identity {key} matches multiple existing UUIDs via "
                f"concept/alias names {sorted(names)}: {sorted(hits)} — "
                "curate the existing rows (merge or disambiguate aliases) "
                "before regenerating"
            )
        return None


def build_catalog(
    observations_path: pathlib.Path,
    docket_path: pathlib.Path | None,
    existing: ExistingCatalog,
) -> dict:
    raw = observations_path.read_bytes()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    identities = build_identities(rows)

    series: list[dict] = []
    used_uuids: dict[str, tuple] = {}

    def mint(key: tuple, names: set[str]) -> tuple[str, list[str]]:
        prior = existing.match(key, names)
        row_uuid = prior["uuid"] if prior else str(uuid_module.uuid4())
        curated_aliases = list(prior.get("aliases", [])) if prior else []
        if row_uuid in used_uuids:
            raise SystemExit(
                f"UUID collision: {row_uuid} claimed by both "
                f"{used_uuids[row_uuid]} and {key} — curate the existing "
                "catalog before regenerating"
            )
        used_uuids[row_uuid] = key
        return row_uuid, curated_aliases

    all_suspects: set[str] = set()
    for key in sorted(identities):
        ident = identities[key]
        concept, _, _ = key
        names = ident["concepts"] | ident["source_concepts"] | {concept}
        row_uuid, curated_aliases = mint(key, names)
        aliases = sorted(
            (ident["concepts"] | ident["source_concepts"] | set(curated_aliases))
            - {concept}
        )
        all_suspects.update(ident["suspects"])
        series.append({
            "uuid": row_uuid,
            "concept": concept,
            "family_patterns": sorted(ident["patterns"]),
            "status": "observed",
            "unit": _sole(ident["units"], "unit", key),
            "cadence": _sole(ident["period_types"], "cadence", key),
            "geography": ident["geography"],
            "entity": ident["entity"],
            "sources": sorted(ident["sources"]),
            "aliases": aliases,
            "rid_patterns": sorted(ident["rid_patterns"]),
            "first_observed_period": min(ident["period_values"], default=None),
            "last_observed_period": max(ident["period_values"], default=None),
            "observation_count": ident["count"],
        })

    docket_raw = b""
    if docket_path is not None and docket_path.exists():
        docket_raw = docket_path.read_bytes()
        docket = json.loads(docket_raw.decode())
        alias_tally = Counter(
            alias for row in series for alias in row["aliases"]
        )
        claimed: dict[str, dict] = {}
        for row in series:
            claimed.setdefault(row["concept"], row)
            for alias in row["aliases"]:
                if alias_tally[alias] == 1:
                    claimed.setdefault(alias, row)
        for entry in docket["series"]:
            concept = entry["series"]
            cadence_word = entry.get("cadence")
            if cadence_word not in CADENCE_TO_PERIOD_TYPE:
                raise SystemExit(
                    f"docket cadence {cadence_word!r} for {concept} has no "
                    "period-type mapping; extend CADENCE_TO_PERIOD_TYPE"
                )
            extras = entry.get("extras") or {}
            hit = claimed.get(concept)
            if hit is not None:
                if concept != hit["concept"] and concept not in hit["aliases"]:
                    hit["aliases"] = sorted(hit["aliases"] + [concept])
                continue
            country = extras.get("country")
            geography = None
            if country is not None:
                geography = COUNTRY_GEOGRAPHY.get(country)
                if geography is None:
                    raise SystemExit(
                        f"docket entry {concept} has country {country!r} with "
                        "no geography mapping; extend COUNTRY_GEOGRAPHY"
                    )
            key = (concept, _geo_key(geography), _entity_key(None))
            row_uuid, curated_aliases = mint(key, {concept})
            row = {
                "uuid": row_uuid,
                "concept": concept,
                "family_patterns": [family_pattern(concept)],
                "status": "docket-only",
                "unit": extras.get("targetUnit"),
                "cadence": CADENCE_TO_PERIOD_TYPE[cadence_word],
                "geography": geography,
                "entity": None,
                "sources": [],
                "aliases": sorted(curated_aliases),
                "rid_patterns": [],
                "first_observed_period": None,
                "last_observed_period": None,
                "observation_count": 0,
            }
            series.append(row)
            claimed[concept] = row

    series.sort(key=lambda row: (
        row["concept"],
        _geo_key(row.get("geography")),
        _entity_key(row.get("entity")),
    ))

    alias_counts = Counter(
        alias for row in series for alias in row["aliases"]
    )
    ambiguous_aliases = sorted(a for a, n in alias_counts.items() if n > 1)

    return {
        "comment": (
            "Canonical series catalog. One row per (concept, geography, "
            "entity) identity; uuid is minted once and never re-minted "
            "(regeneration preserves it by identity, then by concept/alias). "
            "Consumers reference series by uuid or concept only. Regenerate "
            "with scripts/build_series_catalog.py; verify with --check. "
            "Cross-spelling and cross-vintage merges are manual curation: "
            "keep the surviving row's uuid, move absorbed spellings to "
            "aliases — curated aliases persist across regeneration. Aliases "
            "listed in ambiguous_aliases match multiple rows and never "
            "drive identity inheritance."
        ),
        "generator_version": GENERATOR_VERSION,
        "observations_sha256": hashlib.sha256(raw).hexdigest(),
        "observation_rows": len(rows),
        "docket_seed_sha256": (
            hashlib.sha256(docket_raw).hexdigest() if docket_raw else None
        ),
        "suspect_segments": sorted(all_suspects),
        "ambiguous_aliases": ambiguous_aliases,
        "series": series,
    }


def render(catalog: dict) -> str:
    return json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"


def validate_uuids(catalog: dict) -> list[str]:
    """UUID syntax, version, and global-uniqueness problems."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for row in catalog.get("series", []):
        value = row.get("uuid", "")
        concept = row.get("concept", "?")
        try:
            parsed = uuid_module.UUID(value)
        except (ValueError, AttributeError, TypeError):
            problems.append(f"{concept}: uuid {value!r} does not parse")
            continue
        if parsed.version != 4:
            problems.append(f"{concept}: uuid {value} is not UUIDv4")
        if value in seen:
            problems.append(f"uuid {value} duplicated: {seen[value]} and {concept}")
        seen[value] = concept
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=pathlib.Path, default=OBSERVATIONS)
    parser.add_argument("--catalog", type=pathlib.Path, default=CATALOG)
    parser.add_argument(
        "--docket",
        type=pathlib.Path,
        default=DOCKET_SEED,
        help=(
            "Thesis docket seed for docket-only rows "
            "(default: the committed seed; pass a path to update it)"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed catalog is not current for these inputs",
    )
    args = parser.parse_args(argv)

    existing = ExistingCatalog(args.catalog)
    catalog = build_catalog(args.observations, args.docket, existing)
    body = render(catalog)

    problems = validate_uuids(catalog)
    if problems:
        for problem in problems:
            sys.stderr.write(f"uuid validation: {problem}\n")
        return 1

    if args.check:
        current = (
            args.catalog.read_text(encoding="utf-8")
            if args.catalog.exists()
            else ""
        )
        if current != body:
            sys.stderr.write(
                "series_catalog.json is stale for these inputs; regenerate "
                "with scripts/build_series_catalog.py\n"
            )
            return 1
        committed_problems = validate_uuids(json.loads(current))
        if committed_problems:
            for problem in committed_problems:
                sys.stderr.write(f"uuid validation: {problem}\n")
            return 1
        print(f"catalog current: {len(catalog['series'])} series")
        return 0

    args.catalog.write_text(body, encoding="utf-8")
    observed = sum(1 for r in catalog["series"] if r["status"] == "observed")
    docket_only = sum(1 for r in catalog["series"] if r["status"] == "docket-only")
    print(
        f"wrote {args.catalog}: {len(catalog['series'])} series "
        f"({observed} observed, {docket_only} docket-only); "
        f"suspects={len(catalog['suspect_segments'])}, "
        f"ambiguous_aliases={len(catalog['ambiguous_aliases'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
