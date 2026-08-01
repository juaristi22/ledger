#!/usr/bin/env python3
"""Build ledger/series_catalog.json — the canonical series registry.

The observation file records facts; this catalog records the SERIES those
facts belong to, one row per (concept, geography level/id/vintage, entity
name/role) identity — the same identity dimensions the fact ADR uses,
including the geography boundary vintage — keyed by a UUID that is minted
exactly once. Consumers (Thesis's docket, bill mappers, permalink surfaces)
refer to series by catalog UUID or concept and never mint parallel
identities.

UUID authority is NOT this file: it is the append-only minting ledger at
``ledger/series_uuid_registry.jsonl``. Every binding the catalog has ever
shipped is a line there; the builder inherits UUIDs from the registry (then
from the existing catalog row for identities the registry has not met), and
appends a mint line for every new identity it binds. The catalog embeds the
registry digest (``uuid_registry_sha256``), ``--check`` verifies that every
catalog row agrees with its registry binding, and the registry itself may
only grow:

* in a git checkout, the working-tree registry must extend the HEAD blob
  byte-for-byte (checked on every build and ``--check``);
* in CI, ``--verify-registry-append-only BASE_FILE`` proves the PR keeps the
  base branch's registry as an exact byte prefix;
* an identity may change or lose its UUID only through an explicit
  ``--allow-remint --remint-note "..."`` run, which appends a supersede line
  (``supersedes`` = the replaced UUID, chained per identity) so every remint
  is a reviewable event, never a silent regeneration.

Wholesale remints therefore fail ``--check`` twice over: the reminted
catalog disagrees with the committed registry, and any registry rewrite
breaks the append-only prefix.

Family derivation replaces period segments of ``source_record_id`` and
``measure.concept`` with ``{P}``. A dotted segment is treated as a period
segment only when it denotes the row's own declared period: either it is a
direct spelling of that period (``period_token_variants``), or it parses as
a calendar window (``fy2026``, ``2026-05``, ``2026_05_02``, ``may_2026``,
``february_to_april_2026``, ``q1_2026``, ``week_ending_2026_05_02``,
``after_mpc_june_2026``) that overlaps the row's period. Date-shaped
segments that do NOT match the row's period — a disjoint window, or an
impossible token like ``2026_13`` — are never stripped: they stay in the
identity and are reported in ``suspect_segments`` for curation.

Aliases are curated identity statements, not derived data. Observed concept
spellings of one identity become aliases automatically; everything else in
``aliases`` is hand curation and persists across regeneration. Source
labels (``measure.source_concept``) are provenance, recorded per row in
``source_concepts``, and never drive identity inheritance — a source label
may name a different series entirely (a derived share can cite its base
series). Generator versions < 3 mixed source labels into ``aliases``;
purging them (and re-adding the few that are genuine identity statements)
was a one-time curated migration of the committed catalog, reviewed line by
line in the generator-v3 change.

Alias/concept inheritance is scoped to the SAME (geography, entity): a name
match can heal a rename within one dimension slice but can never move a
UUID across geographies or entities. The one documented exception is
docket-placeholder enrichment: a docket-only row (no observations; entity
unknown; geography absent or matching on level/id) may be claimed by the
first observed rows of that series, which upgrade it in place, keep its
UUID, and register the enriched identity.

Curated merges (two committed rows that are the same series) are performed
by hand: delete the absorbed row, add its concept to the survivor's
``aliases``, regenerate with ``--allow-remint --remint-note``; the absorbed
identity's observations inherit the survivor's UUID and the registry gains
the supersede line.

Inputs are pinned: the observation JSONL (``observations_sha256``), the
committed docket seed at ``ledger/seeds/thesis_docket_series.json``
(``docket_seed_sha256``) — a MISSING seed is a hard error, never a silent
shrink — and the UUID registry (``uuid_registry_sha256``). Bare ``--check``
uses the committed inputs, so CI needs no external files.

Idempotent: same inputs + same registry + same existing catalog ->
byte-identical output and an unchanged registry. Unit or cadence conflicts
within one identity are a hard error, never a silent modal pick. UUIDs must
be canonical lowercase UUIDv4 text, and uniqueness is enforced on the
parsed 128-bit value, not the string spelling.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import uuid as uuid_module
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
OBSERVATIONS = ROOT / "ledger" / "official_observations.jsonl"
CATALOG = ROOT / "ledger" / "series_catalog.json"
DOCKET_SEED = ROOT / "ledger" / "seeds" / "thesis_docket_series.json"
UUID_REGISTRY = ROOT / "ledger" / "series_uuid_registry.jsonl"

GENERATOR_VERSION = 3

MONTHS_FULL = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
# Exactly twelve, indexable by month-1. "sept" is an accepted alternate
# spelling for parsing only — generator v2 kept it inside this list, which
# silently shifted the derived abbreviations for October-December.
MONTHS_ABBREV = [
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
]
_MONTH_NUM: dict[str, int] = {}
for _i, _name in enumerate(MONTHS_FULL):
    _MONTH_NUM[_name] = _i + 1
    _MONTH_NUM[MONTHS_ABBREV[_i]] = _i + 1
_MONTH_NUM["sept"] = 9
_MONTH_ALT = "|".join(sorted(_MONTH_NUM, key=len, reverse=True))

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

_YEAR_HINT = re.compile(r"(?:19|20)\d{2}")

_FY_RE = re.compile(r"fy(\d{4})")
_NUMERIC_DATE_RE = re.compile(r"(\d{4})[-_](\d{2})(?:[-_](\d{2}))?")
_MONTH_NAME_RE = re.compile(r"(%s)_(\d{4})" % _MONTH_ALT)
_MONTH_RANGE_RE = re.compile(r"(%s)_to_(%s)_(\d{4})" % (_MONTH_ALT, _MONTH_ALT))
_QUARTER_RE = re.compile(r"q([1-4])_(\d{4})|(\d{4})_q([1-4])")
_WEEK_RE = re.compile(r"week_(?:ending_)?(\d{4})[-_](\d{2})[-_](\d{2})")


def _month_span(year: int, month: int) -> tuple[dt.date, dt.date] | None:
    if not 1 <= month <= 12:
        return None
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, last)


def _day(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_period_token(segment: str) -> tuple | None:
    """Parse one dotted segment as a period token.

    Returns ``("fiscal_year", year)`` or ``("span", (start, end))`` with
    inclusive ``datetime.date`` bounds, or ``None`` when the segment is not
    a well-formed period token. ``after_``-qualified compounds
    (``after_june_2026``, ``after_mpc_june_2026``) parse as their base
    token. Date-shaped strings that denote no real window — ``2026_13``,
    ``2026-02-30`` — return ``None``.

    >>> parse_period_token("fy2026")
    ('fiscal_year', 2026)
    >>> parse_period_token("2026_05")
    ('span', (datetime.date(2026, 5, 1), datetime.date(2026, 5, 31)))
    >>> parse_period_token("after_mpc_june_2026")
    ('span', (datetime.date(2026, 6, 1), datetime.date(2026, 6, 30)))
    >>> parse_period_token("week_ending_2026_06_06")
    ('span', (datetime.date(2026, 5, 31), datetime.date(2026, 6, 6)))
    >>> parse_period_token("2026_13") is None
    True
    >>> parse_period_token("m3") is None
    True
    """
    if segment.startswith("after_"):
        rest = segment[len("after_"):]
        while rest:
            parsed = parse_period_token(rest)
            if parsed is not None:
                return parsed
            if "_" not in rest:
                return None
            rest = rest.split("_", 1)[1]
        return None
    m = _FY_RE.fullmatch(segment)
    if m:
        return ("fiscal_year", int(m.group(1)))
    m = _WEEK_RE.fullmatch(segment)
    if m:
        end = _day(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if end is None:
            return None
        return ("span", (end - dt.timedelta(days=6), end))
    m = _NUMERIC_DATE_RE.fullmatch(segment)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if m.group(3) is None:
            span = _month_span(year, month)
            return ("span", span) if span else None
        day = _day(year, month, int(m.group(3)))
        return ("span", (day, day)) if day else None
    m = _MONTH_RANGE_RE.fullmatch(segment)
    if m:
        first, last = _MONTH_NUM[m.group(1)], _MONTH_NUM[m.group(2)]
        year = int(m.group(3))
        if first > last:
            return None
        start = dt.date(year, first, 1)
        end = _month_span(year, last)[1]
        return ("span", (start, end))
    m = _MONTH_NAME_RE.fullmatch(segment)
    if m:
        return ("span", _month_span(int(m.group(2)), _MONTH_NUM[m.group(1)]))
    m = _QUARTER_RE.fullmatch(segment)
    if m:
        quarter = int(m.group(1) or m.group(4))
        year = int(m.group(2) or m.group(3))
        start = dt.date(year, 3 * quarter - 2, 1)
        end = _month_span(year, 3 * quarter)[1]
        return ("span", (start, end))
    return None


def is_period_segment(segment: str) -> bool:
    """Whether one dotted segment parses as a real period token.

    >>> [is_period_segment(s) for s in (
    ...     "2026_05", "2026-05", "feb_2026", "week_ending_2026_06_06",
    ...     "after_june_2026", "after_mpc_june_2026", "2026_06_18",
    ...     "february_to_april_2026", "week_2026-06-13", "fy2026", "2026_q2",
    ... )]
    [True, True, True, True, True, True, True, True, True, True, True]
    >>> [is_period_segment(s) for s in (
    ...     "36-10-0434-01", "g17", "adv44x72", "j5ii", "m3", "2026_13",
    ...     "first_print", "third_estimate", "original_submission",
    ...     "total_nonfarm_payroll_change", "australia",
    ... )]
    [False, False, False, False, False, False, False, False, False, False, False]
    """
    return parse_period_token(segment) is not None


def period_token_variants(period: dict) -> set[str]:
    """Every direct spelling of ``period`` that may appear as an id segment."""
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


def period_descriptor(period: dict | None) -> tuple | None:
    """The row period as a comparable descriptor (same shapes as tokens)."""
    if not period:
        return None
    ptype = period.get("type")
    value = period.get("value")
    if value is None:
        return None
    value = str(value)
    if ptype == "fiscal_year":
        if value.isdigit():
            return ("fiscal_year", int(value))
        return None
    if ptype == "month":
        m = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if m:
            span = _month_span(int(m.group(1)), int(m.group(2)))
            return ("span", span) if span else None
        return None
    if ptype == "quarter":
        m = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
            if not 1 <= month <= 12:
                return None
            quarter = (month - 1) // 3 + 1
            start = dt.date(year, 3 * quarter - 2, 1)
            return ("span", (start, _month_span(year, 3 * quarter)[1]))
        return None
    if ptype == "week_ending":
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
        if m:
            end = _day(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if end is None:
                return None
            return ("span", (end - dt.timedelta(days=6), end))
        return None
    if ptype == "year":
        if value.isdigit():
            year = int(value)
            return ("span", (dt.date(year, 1, 1), dt.date(year, 12, 31)))
        return None
    return None


def _fiscal_year_span(year: int) -> tuple[dt.date, dt.date]:
    # US federal fiscal year, the only fiscal calendar in the ledger today.
    return dt.date(year - 1, 10, 1), dt.date(year, 9, 30)


def _matches_period(token: tuple, period_desc: tuple) -> bool:
    """Whether a parsed token denotes the row's own period (window overlap).

    Fiscal-year tokens match a fiscal-year period only on equal years; mixed
    fiscal/calendar comparisons use the US federal fiscal calendar.
    """
    if token[0] == "fiscal_year" and period_desc[0] == "fiscal_year":
        return token[1] == period_desc[1]
    a = _fiscal_year_span(token[1]) if token[0] == "fiscal_year" else token[1]
    b = (
        _fiscal_year_span(period_desc[1])
        if period_desc[0] == "fiscal_year"
        else period_desc[1]
    )
    return a[0] <= b[1] and b[0] <= a[1]


def family_pattern(identifier: str, period: dict | None = None) -> str:
    """Replace segments denoting the row's own period with ``{P}``.

    A segment is stripped only when it is a direct spelling of the row
    period or parses to a calendar window overlapping it. Date-shaped
    segments disjoint from the row period survive (and are flagged by
    ``suspect_segments``), so a statute year, cohort, or edition can never
    be silently deleted from an identity.

    >>> family_pattern("bls.eci.private_wages_salaries_qoq.2026_q2.first_print",
    ...                {"type": "quarter", "value": "2026-04"})
    'bls.eci.private_wages_salaries_qoq.{P}.first_print'
    >>> family_pattern("census.m3.durable_goods_new_orders_mom.2026_06",
    ...                {"type": "month", "value": "2026-06"})
    'census.m3.durable_goods_new_orders_mom.{P}'
    >>> family_pattern("boe.bank_rate.after_mpc_june_2026",
    ...                {"type": "month", "value": "2026-06"})
    'boe.bank_rate.{P}'
    >>> family_pattern("dol.eta.initial_claims.sa.week_ending_2026_06_06",
    ...                {"type": "month", "value": "2026-06"})
    'dol.eta.initial_claims.sa.{P}'
    >>> family_pattern("agency.rate.2025_12", {"type": "month", "value": "2026-06"})
    'agency.rate.2025_12'
    >>> family_pattern("abs.cpi.all_groups.yoy", {"type": "month", "value": "2026-05"})
    'abs.cpi.all_groups.yoy'
    """
    derived = period_token_variants(period or {})
    period_desc = period_descriptor(period)
    out = []
    for segment in identifier.split("."):
        if segment in derived:
            out.append("{P}")
            continue
        token = parse_period_token(segment)
        if token is not None and period_desc is not None:
            if _matches_period(token, period_desc):
                out.append("{P}")
                continue
        out.append(segment)
    return ".".join(out)


def concept_for(pattern: str) -> str:
    """The human-facing canonical concept: the pattern minus placeholders."""
    return ".".join(s for s in pattern.split(".") if s != "{P}")


def suspect_segments(pattern: str) -> list[str]:
    """Surviving segments that smell of a date — flagged, never stripped.

    Covers both period-shaped segments that contradicted the row's own
    period (kept in the identity by ``family_pattern``) and free-form
    year-bearing segments.

    >>> suspect_segments("agency.rate.2025_12")
    ['2025_12']
    >>> suspect_segments("agency.series.mid2026wave")
    ['mid2026wave']
    >>> suspect_segments("bls.eci.private_wages_salaries_qoq.{P}.first_print")
    []
    """
    return [
        s for s in pattern.split(".")
        if s != "{P}" and (is_period_segment(s) or _YEAR_HINT.search(s))
    ]


def _geo_key(geography: dict | None) -> str:
    g = geography or {}
    return f"{g.get('level')}|{g.get('id')}|{g.get('vintage')}"


def _entity_key(entity: dict | None) -> str:
    e = entity or {}
    return f"{e.get('name')}|{e.get('role')}"


def _identity_geography(geography: dict | None) -> dict | None:
    if geography is None:
        return None
    return {
        "level": geography.get("level"),
        "id": geography.get("id"),
        "vintage": geography.get("vintage"),
    }


def _identity_entity(entity: dict | None) -> dict | None:
    if entity is None:
        return None
    return {"name": entity.get("name"), "role": entity.get("role")}


def canonical_uuid_problem(value: object) -> str | None:
    """Why ``value`` is not a canonical lowercase UUIDv4 string, else None."""
    if not isinstance(value, str):
        return f"uuid {value!r} is not a string"
    try:
        parsed = uuid_module.UUID(value)
    except ValueError:
        return f"uuid {value!r} does not parse"
    if str(parsed) != value:
        return f"uuid {value!r} is not canonical lowercase form ({parsed})"
    if parsed.version != 4:
        return f"uuid {value} is not UUIDv4"
    return None


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
    """Canonical-concept and curated-alias memory from the committed catalog."""

    def __init__(self, path: pathlib.Path) -> None:
        self.rows: list[dict] = []
        self.by_identity: dict[tuple[str, str, str], dict] = {}
        self.by_dim: dict[tuple[str, str], list[dict]] = {}
        self.docket_rows: list[dict] = []
        if not path.exists():
            return
        catalog = json.loads(path.read_text(encoding="utf-8"))
        for row in catalog.get("series", []):
            self.rows.append(row)
            key = (
                row["concept"],
                _geo_key(row.get("geography")),
                _entity_key(row.get("entity")),
            )
            self.by_identity[key] = row
            self.by_dim.setdefault((key[1], key[2]), []).append(row)
            if row.get("status") == "docket-only":
                self.docket_rows.append(row)

    def _row_names(self, row: dict) -> set[str]:
        return {row["concept"], *row.get("aliases", [])}

    def match(
        self,
        key: tuple[str, str, str],
        names: set[str],
        geography: dict | None,
        entity: dict | None,
    ) -> dict | None:
        """The existing row for this identity.

        Exact identity key first; else a unique concept/curated-alias hit
        WITHIN the same (geography, entity) — names heal renames inside one
        dimension slice, never across dimensions; else a unique docket-only
        placeholder whose declared dimensions do not contradict the incoming
        row (the placeholder-enrichment exception).
        """
        row = self.by_identity.get(key)
        if row is not None:
            return row
        hits: dict[str, dict] = {}
        for candidate in self.by_dim.get((key[1], key[2]), []):
            if names & self._row_names(candidate):
                hits[candidate["uuid"]] = candidate
        if len(hits) > 1:
            raise SystemExit(
                f"identity {key} matches multiple existing UUIDs via "
                f"concept/alias names {sorted(names)}: {sorted(hits)} — "
                "curate the existing rows (merge or disambiguate aliases) "
                "before regenerating"
            )
        if hits:
            return next(iter(hits.values()))
        placeholder_hits: dict[str, dict] = {}
        for candidate in self.docket_rows:
            if not names & self._row_names(candidate):
                continue
            if candidate.get("entity") is not None:
                continue
            cand_geo = candidate.get("geography")
            if cand_geo is not None:
                incoming = geography or {}
                if (cand_geo.get("level"), cand_geo.get("id")) != (
                    incoming.get("level"),
                    incoming.get("id"),
                ):
                    continue
            placeholder_hits[candidate["uuid"]] = candidate
        if len(placeholder_hits) > 1:
            raise SystemExit(
                f"identity {key} matches multiple docket-only placeholders "
                f"via names {sorted(names)}: {sorted(placeholder_hits)} — "
                "curate the seed/catalog before regenerating"
            )
        if placeholder_hits:
            return next(iter(placeholder_hits.values()))
        return None


class UuidRegistry:
    """The append-only UUID minting ledger.

    One JSON object per line. A line binds one identity (concept, geography
    level/id/vintage, entity name/role) to a UUID. The first line for an
    identity is its mint; every later line for the same identity must carry
    ``supersedes`` (the previous UUID) and a non-empty ``note``, so identity
    changes are chained, explicit events. Lines are never edited or removed;
    ``--verify-registry-append-only`` and the git-HEAD prefix check enforce
    that the file only grows. Multiple identities may share a UUID (a
    curated merge moves an identity onto the survivor's UUID; an enriched
    docket placeholder registers its observed identity beside the seed
    one) — the catalog still enforces one ROW per UUID.
    """

    def __init__(self, path: pathlib.Path, raw: bytes) -> None:
        self.path = path
        self.raw = raw
        self.entries: list[dict] = []
        self.latest: dict[tuple[str, str, str], dict] = {}
        problems: list[str] = []
        for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                problems.append(f"line {lineno}: blank line")
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"line {lineno}: not JSON ({exc})")
                continue
            if not isinstance(entry, dict) or not isinstance(
                entry.get("concept"), str
            ):
                problems.append(f"line {lineno}: missing concept")
                continue
            problem = canonical_uuid_problem(entry.get("uuid"))
            if problem:
                problems.append(f"line {lineno}: {problem}")
                continue
            key = self.entry_key(entry)
            previous = self.latest.get(key)
            supersedes = entry.get("supersedes")
            if previous is None:
                if supersedes is not None:
                    problems.append(
                        f"line {lineno}: {key} supersedes {supersedes} but "
                        "has no prior binding"
                    )
            else:
                if supersedes is None:
                    problems.append(
                        f"line {lineno}: {key} re-binds without supersedes "
                        f"(prior uuid {previous['uuid']})"
                    )
                elif supersedes != previous["uuid"]:
                    problems.append(
                        f"line {lineno}: {key} supersedes {supersedes} but "
                        f"prior binding is {previous['uuid']}"
                    )
                if not (
                    isinstance(entry.get("note"), str) and entry["note"].strip()
                ):
                    problems.append(
                        f"line {lineno}: supersede for {key} requires a note"
                    )
            self.entries.append(entry)
            self.latest[key] = entry
        if problems:
            raise SystemExit(
                "uuid registry invalid:\n"
                + "\n".join(f"  {p}" for p in problems)
            )

    @staticmethod
    def entry_key(entry: dict) -> tuple[str, str, str]:
        return (
            entry["concept"],
            _geo_key(entry.get("geography")),
            _entity_key(entry.get("entity")),
        )

    @classmethod
    def load(cls, path: pathlib.Path) -> UuidRegistry:
        if not path.exists():
            raise SystemExit(
                f"uuid registry missing: {path} — the registry is the "
                "append-only UUID authority and must exist (create an empty "
                "file only when initializing a brand-new catalog)"
            )
        return cls(path, path.read_bytes())

    def binding(self, key: tuple[str, str, str]) -> str | None:
        entry = self.latest.get(key)
        return entry["uuid"] if entry else None

    @staticmethod
    def render_entry(entry: dict) -> str:
        ordered = {
            "concept": entry["concept"],
            "geography": entry.get("geography"),
            "entity": entry.get("entity"),
            "uuid": entry["uuid"],
        }
        if entry.get("supersedes") is not None:
            ordered["supersedes"] = entry["supersedes"]
            ordered["note"] = entry["note"]
        return json.dumps(ordered, ensure_ascii=False)

    def stage(self, new_entries: list[dict]) -> None:
        """Add entries to the in-memory registry (no file write yet)."""
        for entry in new_entries:
            self.entries.append(entry)
            self.latest[self.entry_key(entry)] = entry
        addition = "".join(
            self.render_entry(entry) + "\n" for entry in new_entries
        )
        self.raw = self.raw + addition.encode("utf-8")

    def write(self) -> None:
        self.path.write_bytes(self.raw)

    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


def _registry_event(
    key_concept: str,
    geography: dict | None,
    entity: dict | None,
    row_uuid: str,
    supersedes: str | None = None,
) -> dict:
    return {
        "concept": key_concept,
        "geography": _identity_geography(geography),
        "entity": _identity_entity(entity),
        "uuid": row_uuid,
        "supersedes": supersedes,
    }


def build_catalog(
    observations_path: pathlib.Path,
    docket_path: pathlib.Path | None,
    existing: ExistingCatalog,
    registry: UuidRegistry,
) -> tuple[dict, dict]:
    """Build the catalog and the identity plan.

    The plan records every registry-affecting outcome: ``mints`` (new
    identity bindings to append), ``supersedes`` (identities whose UUID
    changes — these require ``--allow-remint``), and ``dropped`` (existing
    rows whose UUID would vanish from the catalog — also gated).
    """
    raw = observations_path.read_bytes()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    identities = build_identities(rows)

    # Canonicalize each observed bucket through the existing catalog: an
    # exact identity hit, a same-dimension curated-alias hit, or a
    # docket-placeholder hit keeps BOTH the prior UUID lineage and the prior
    # canonical concept (curation owns naming; observed spellings become
    # aliases). Buckets landing on the same canonical identity merge.
    canonical: dict[tuple[str, str, str], dict] = {}
    rekeys: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for key in sorted(identities):
        ident = identities[key]
        concept, geo_key, entity_key = key
        names = ident["concepts"] | {concept}
        prior = existing.match(key, names, ident["geography"], ident["entity"])
        canon_concept = prior["concept"] if prior else concept
        canon_key = (canon_concept, geo_key, entity_key)
        if canon_key != key:
            rekeys[key] = canon_key
        bucket = canonical.setdefault(
            canon_key,
            {
                "prior": prior,
                "patterns": set(),
                "concepts": set(),
                "source_concepts": set(),
                "rid_patterns": set(),
                "suspects": set(),
                "units": Counter(),
                "period_types": Counter(),
                "geography": ident["geography"],
                "entity": ident["entity"],
                "sources": set(),
                "period_values": [],
                "count": 0,
            },
        )
        if bucket["prior"] is None:
            bucket["prior"] = prior
        elif prior is not None and prior["uuid"] != bucket["prior"]["uuid"]:
            raise SystemExit(
                f"identity {canon_key} inherits two different UUIDs "
                f"({bucket['prior']['uuid']}, {prior['uuid']}) — curate the "
                "existing catalog before regenerating"
            )
        bucket["patterns"] |= ident["patterns"]
        bucket["concepts"] |= ident["concepts"]
        bucket["source_concepts"] |= ident["source_concepts"]
        bucket["rid_patterns"] |= ident["rid_patterns"]
        bucket["suspects"] |= ident["suspects"]
        bucket["units"] += ident["units"]
        bucket["period_types"] += ident["period_types"]
        bucket["sources"] |= ident["sources"]
        bucket["period_values"] += ident["period_values"]
        bucket["count"] += ident["count"]

    series: list[dict] = []
    used_uuids: dict[int, tuple] = {}
    plan: dict[str, list] = {"mints": [], "supersedes": [], "dropped": []}

    def claim_uuid(row_uuid: str, key: tuple) -> str:
        problem = canonical_uuid_problem(row_uuid)
        if problem:
            raise SystemExit(f"identity {key}: {problem}")
        parsed = uuid_module.UUID(row_uuid).int
        if parsed in used_uuids:
            raise SystemExit(
                f"UUID collision: {row_uuid} claimed by both "
                f"{used_uuids[parsed]} and {key} — curate the existing "
                "catalog before regenerating"
            )
        used_uuids[parsed] = key
        return row_uuid

    def resolve_uuid(
        canon_key: tuple[str, str, str],
        prior: dict | None,
        geography: dict | None,
        entity: dict | None,
    ) -> str:
        binding = registry.binding(canon_key)
        prior_uuid = prior["uuid"] if prior else None
        if prior_uuid and binding and prior_uuid != binding:
            # The catalog row disagrees with the registry: an explicit,
            # gated remint (the curator edited the row's uuid on purpose).
            plan["supersedes"].append(
                _registry_event(
                    canon_key[0], geography, entity, prior_uuid, binding
                )
            )
            return prior_uuid
        if binding:
            return binding
        row_uuid = prior_uuid if prior_uuid else str(uuid_module.uuid4())
        plan["mints"].append(
            _registry_event(canon_key[0], geography, entity, row_uuid)
        )
        return row_uuid

    all_suspects: set[str] = set()
    for canon_key in sorted(canonical):
        bucket = canonical[canon_key]
        concept, _, _ = canon_key
        prior = bucket["prior"]
        row_uuid = claim_uuid(
            resolve_uuid(canon_key, prior, bucket["geography"], bucket["entity"]),
            canon_key,
        )
        curated_aliases = set(prior.get("aliases", [])) if prior else set()
        aliases = sorted((bucket["concepts"] | curated_aliases) - {concept})
        all_suspects.update(bucket["suspects"])
        series.append({
            "uuid": row_uuid,
            "concept": concept,
            "family_patterns": sorted(bucket["patterns"]),
            "status": "observed",
            "unit": _sole(bucket["units"], "unit", canon_key),
            "cadence": _sole(bucket["period_types"], "cadence", canon_key),
            "geography": bucket["geography"],
            "entity": bucket["entity"],
            "sources": sorted(bucket["sources"]),
            "aliases": aliases,
            "source_concepts": sorted(bucket["source_concepts"]),
            "rid_patterns": sorted(bucket["rid_patterns"]),
            "first_observed_period": min(bucket["period_values"], default=None),
            "last_observed_period": max(bucket["period_values"], default=None),
            "observation_count": bucket["count"],
        })

    docket_raw = b""
    if docket_path is not None:
        docket_raw = docket_path.read_bytes()
        docket = json.loads(docket_raw.decode())
        alias_tally = Counter(alias for row in series for alias in row["aliases"])
        claimed_names: set[str] = set()
        for row in series:
            claimed_names.add(row["concept"])
            for alias in row["aliases"]:
                if alias_tally[alias] == 1:
                    claimed_names.add(alias)
        for entry in docket["series"]:
            concept = entry["series"]
            cadence_word = entry.get("cadence")
            if cadence_word not in CADENCE_TO_PERIOD_TYPE:
                raise SystemExit(
                    f"docket cadence {cadence_word!r} for {concept} has no "
                    "period-type mapping; extend CADENCE_TO_PERIOD_TYPE"
                )
            if concept in claimed_names:
                continue
            extras = entry.get("extras") or {}
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
            prior = existing.match(key, {concept}, geography, None)
            row_uuid = claim_uuid(
                resolve_uuid(key, prior, geography, None), key
            )
            curated_aliases = set(prior.get("aliases", [])) if prior else set()
            series.append({
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
                "source_concepts": [],
                "rid_patterns": [],
                "first_observed_period": None,
                "last_observed_period": None,
                "observation_count": 0,
            })
            claimed_names.add(concept)

    series.sort(key=lambda row: (
        row["concept"],
        _geo_key(row.get("geography")),
        _entity_key(row.get("entity")),
    ))

    # Absorbed identities: an observed bucket that canonicalized onto a
    # different identity moves that identity's registry binding onto the
    # surviving UUID (a supersede event) if it pointed elsewhere.
    row_uuid_by_key = {
        (r["concept"], _geo_key(r.get("geography")), _entity_key(r.get("entity"))):
            r["uuid"]
        for r in series
    }
    for original_key, canon_key in sorted(rekeys.items()):
        old_binding = registry.binding(original_key)
        surviving = row_uuid_by_key.get(canon_key)
        if old_binding and surviving and old_binding != surviving:
            ident = identities[original_key]
            plan["supersedes"].append(
                _registry_event(
                    original_key[0],
                    ident["geography"],
                    ident["entity"],
                    surviving,
                    old_binding,
                )
            )

    # Existing rows whose UUID would vanish from the catalog entirely.
    new_uuids = {r["uuid"] for r in series}
    for prior_key, prior_row in sorted(existing.by_identity.items()):
        if prior_key in row_uuid_by_key:
            continue
        if prior_row["uuid"] in new_uuids:
            continue  # lineage survives on another identity (merge/enrich)
        plan["dropped"].append((prior_key, prior_row["uuid"]))

    for kind in ("mints", "supersedes"):
        plan[kind].sort(
            key=lambda e: (e["concept"], _geo_key(e["geography"]),
                           _entity_key(e["entity"]))
        )

    alias_counts = Counter(alias for row in series for alias in row["aliases"])
    ambiguous_aliases = sorted(a for a, n in alias_counts.items() if n > 1)

    catalog = {
        "comment": (
            "Canonical series catalog. One row per (concept, geography "
            "level/id/vintage, entity) identity. UUID authority is the "
            "append-only ledger/series_uuid_registry.jsonl (digest below): "
            "a uuid is minted once, inherited from the registry on every "
            "regeneration, and changes only through an explicit "
            "--allow-remint supersede event recorded there. Consumers "
            "reference series by uuid or concept only. Regenerate with "
            "scripts/build_series_catalog.py; verify with --check. Aliases "
            "are curated identity statements (plus observed spellings of "
            "the same identity) and inherit only within one (geography, "
            "entity) slice; aliases listed in ambiguous_aliases match "
            "multiple rows and never drive inheritance. source_concepts "
            "are publisher labels — provenance, never identity. Cross-"
            "spelling and cross-vintage merges are manual curation: delete "
            "the absorbed row, alias its concept on the survivor, "
            "regenerate with --allow-remint."
        ),
        "generator_version": GENERATOR_VERSION,
        "observations_sha256": hashlib.sha256(raw).hexdigest(),
        "observation_rows": len(rows),
        "docket_seed_sha256": (
            hashlib.sha256(docket_raw).hexdigest() if docket_raw else None
        ),
        "uuid_registry_sha256": None,
        "suspect_segments": sorted(all_suspects),
        "ambiguous_aliases": ambiguous_aliases,
        "series": series,
    }
    return catalog, plan


def render(catalog: dict) -> str:
    return json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"


def validate_uuids(catalog: dict) -> list[str]:
    """Canonical-form, version, and parsed-value-uniqueness problems."""
    problems: list[str] = []
    seen: dict[int, str] = {}
    for row in catalog.get("series", []):
        value = row.get("uuid", "")
        concept = row.get("concept", "?")
        problem = canonical_uuid_problem(value)
        if problem:
            problems.append(f"{concept}: {problem}")
            if not isinstance(value, str):
                continue
            try:
                parsed = uuid_module.UUID(value).int
            except ValueError:
                continue
        else:
            parsed = uuid_module.UUID(value).int
        if parsed in seen:
            problems.append(
                f"uuid {value} duplicates {seen[parsed]} (same 128-bit "
                f"value) on {concept}"
            )
        else:
            seen[parsed] = concept
    return problems


def registry_agreement_problems(
    catalog: dict, registry: UuidRegistry
) -> list[str]:
    """Catalog rows whose identity is unbound or disagrees with the registry."""
    problems = []
    for row in catalog.get("series", []):
        key = (
            row["concept"],
            _geo_key(row.get("geography")),
            _entity_key(row.get("entity")),
        )
        binding = registry.binding(key)
        if binding is None:
            problems.append(f"{key}: no registry binding for uuid {row['uuid']}")
        elif binding != row["uuid"]:
            problems.append(
                f"{key}: catalog uuid {row['uuid']} != registry binding "
                f"{binding}"
            )
    return problems


def git_head_bytes(path: pathlib.Path) -> bytes | None:
    """The file's committed HEAD content, or None when unavailable."""
    try:
        toplevel = subprocess.run(
            ["git", "-C", str(path.resolve().parent), "rev-parse",
             "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        rel = path.resolve().relative_to(pathlib.Path(toplevel)).as_posix()
        shown = subprocess.run(
            ["git", "-C", toplevel, "show", f"HEAD:{rel}"],
            capture_output=True, check=True,
        )
        return shown.stdout
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None


def append_only_problem(base: bytes, current: bytes) -> str | None:
    """Why ``current`` is not an append-only extension of ``base``."""
    if current.startswith(base):
        return None
    return (
        "registry is not an append-only extension of its prior committed "
        "content: existing lines were edited or removed"
    )


def _check_registry_vs_head(registry: UuidRegistry) -> None:
    head = git_head_bytes(registry.path)
    if head is None:
        print(
            "note: no committed HEAD version of the registry to compare "
            "against (new file or not a git checkout)",
            file=sys.stderr,
        )
        return
    problem = append_only_problem(head, registry.raw)
    if problem:
        raise SystemExit(f"uuid registry vs HEAD: {problem}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=pathlib.Path, default=OBSERVATIONS)
    parser.add_argument("--catalog", type=pathlib.Path, default=CATALOG)
    parser.add_argument("--registry", type=pathlib.Path, default=UUID_REGISTRY)
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
    parser.add_argument(
        "--allow-remint",
        action="store_true",
        help=(
            "permit identities to change or lose their UUID; every change "
            "is recorded as a supersede event in the registry"
        ),
    )
    parser.add_argument(
        "--remint-note",
        default=None,
        help="required with --allow-remint: why the identity change is right",
    )
    parser.add_argument(
        "--verify-registry-append-only",
        type=pathlib.Path,
        default=None,
        metavar="BASE_FILE",
        help=(
            "verify the registry extends BASE_FILE byte-for-byte (CI runs "
            "this against the PR base's registry), then exit"
        ),
    )
    args = parser.parse_args(argv)

    if args.verify_registry_append_only is not None:
        registry = UuidRegistry.load(args.registry)
        base = (
            args.verify_registry_append_only.read_bytes()
            if args.verify_registry_append_only.exists()
            else b""
        )
        problem = append_only_problem(base, registry.raw)
        if problem:
            sys.stderr.write(problem + "\n")
            return 1
        print(
            f"registry append-only vs base: ok "
            f"({len(registry.entries)} entries)"
        )
        return 0

    if not args.observations.exists():
        raise SystemExit(f"observations missing: {args.observations}")
    if args.docket is None or not args.docket.exists():
        raise SystemExit(
            f"docket seed missing: {args.docket} — the seed is a pinned "
            "input; a catalog regenerated without it silently drops every "
            "docket-only series, so this is always a hard error"
        )
    registry = UuidRegistry.load(args.registry)
    _check_registry_vs_head(registry)

    existing = ExistingCatalog(args.catalog)
    catalog, plan = build_catalog(args.observations, args.docket, existing, registry)

    identity_changes = [
        f"remint {UuidRegistry.entry_key(e)}: {e['supersedes']} -> {e['uuid']}"
        for e in plan["supersedes"]
    ] + [
        f"dropped {key}: uuid {row_uuid} would vanish from the catalog"
        for key, row_uuid in plan["dropped"]
    ]

    if args.check:
        failures: list[str] = []
        if plan["mints"]:
            failures.append(
                f"{len(plan['mints'])} identities missing from the registry "
                "(regenerate to mint them)"
            )
        failures.extend(identity_changes)
        catalog["uuid_registry_sha256"] = registry.sha256()
        body = render(catalog)
        current = (
            args.catalog.read_text(encoding="utf-8")
            if args.catalog.exists()
            else ""
        )
        if current != body:
            failures.append(
                "series_catalog.json is stale for these inputs; regenerate "
                "with scripts/build_series_catalog.py"
            )
        if current:
            committed = json.loads(current)
            for problem in validate_uuids(committed):
                failures.append(f"uuid validation: {problem}")
            for problem in registry_agreement_problems(committed, registry):
                failures.append(f"registry agreement: {problem}")
            if committed.get("docket_seed_sha256") is None:
                failures.append("committed catalog has no docket seed digest")
        if failures:
            for failure in failures:
                sys.stderr.write(failure + "\n")
            return 1
        print(f"catalog current: {len(catalog['series'])} series")
        return 0

    if identity_changes and not args.allow_remint:
        for change in identity_changes:
            sys.stderr.write(f"identity change requires --allow-remint: "
                             f"{change}\n")
        sys.stderr.write(
            "refusing to write: existing identities would change or lose "
            "their UUID; rerun with --allow-remint --remint-note '...' if "
            "this is deliberate curation\n"
        )
        return 1
    if args.allow_remint:
        if not (args.remint_note and args.remint_note.strip()):
            raise SystemExit("--allow-remint requires --remint-note")
        for event in plan["supersedes"]:
            event["note"] = args.remint_note.strip()

    problems = validate_uuids(catalog)
    if problems:
        for problem in problems:
            sys.stderr.write(f"uuid validation: {problem}\n")
        return 1

    registry.stage(plan["mints"] + plan["supersedes"])
    catalog["uuid_registry_sha256"] = registry.sha256()
    body = render(catalog)

    agreement = registry_agreement_problems(catalog, registry)
    if agreement:
        for problem in agreement:
            sys.stderr.write(f"registry agreement: {problem}\n")
        return 1

    registry.write()
    args.catalog.write_text(body, encoding="utf-8")
    observed = sum(1 for r in catalog["series"] if r["status"] == "observed")
    docket_only = sum(1 for r in catalog["series"] if r["status"] == "docket-only")
    print(
        f"wrote {args.catalog}: {len(catalog['series'])} series "
        f"({observed} observed, {docket_only} docket-only); "
        f"suspects={len(catalog['suspect_segments'])}, "
        f"ambiguous_aliases={len(catalog['ambiguous_aliases'])}, "
        f"minted={len(plan['mints'])}, superseded={len(plan['supersedes'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
