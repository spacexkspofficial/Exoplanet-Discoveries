"""Freeze the P4 known-object regression suite (MASTER_PLAN.md section 4.1).

The research review's soundest recommendation, adopted: a curated cohort of
real catalogued objects plus *deliberate near-miss impostors*, which every
identity/catalog change must resolve correctly before merge. Impostors are the
point. A suite of true positives only proves the matcher says yes; the
failure this project actually suffers from is a matcher that says yes too
often, and only a phase-distinct or period-detuned near-miss can catch that.

Expectations are written down here as *intent*, from the matching rules in
section 4.3, and the builder refuses to freeze an entry whose intent the
current code does not reproduce. Disagreements are printed for a human to
adjudicate rather than being silently adopted -- otherwise the suite would
freeze whatever the code happens to do today, which regresses nothing.

    python scripts/build_p4_known_objects.py --out results/p4/known_objects_v1
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exohunt import adjudicate, snapshots  # noqa: E402
from exohunt.config import (  # noqa: E402
    CURRENT_EPHEMERIS_MATCH,
    CURRENT_IDENTITY,
    module_digest,
    vetting_signature,
)

SUITE_SCHEMA_VERSION = 1

# Cohort sizes. Deliberately dominated by true catalogued objects, with a
# fifth of the suite made of near-misses derived from them.
CONFIRMED_PLANETS = 150
TOIS = 150
ECLIPSING_BINARIES = 100
IMPOSTORS_PER_KIND = 34


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def _int(value: Any) -> int | None:
    text = str(value or "").replace("TIC", "").strip()
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def load(source: str) -> tuple[list[dict[str, str]], str]:
    manifest = snapshots.latest(source)
    if manifest is None:
        raise SystemExit(
            f"No snapshot for {source}. Run scripts/fetch_p4_snapshots.py first."
        )
    return snapshots.load_rows(manifest), manifest.content_hash


def confirmed_planet_entries(limit: int) -> list[dict[str, Any]]:
    rows, snapshot_hash = load("nasa_ps")
    usable = []
    for row in rows:
        if str(row.get("tran_flag")).strip() not in {"1", "1.0"}:
            continue
        tic = _int(row.get("tic_id"))
        period, epoch, duration = (
            _f(row.get("pl_orbper")),
            _f(row.get("pl_tranmid")),
            _f(row.get("pl_trandur")),
        )
        if not (tic and period and epoch and duration and period > 0):
            continue
        usable.append(
            {
                "id": f"ps:{row['pl_name']}",
                "tic_id": tic,
                "source": "nasa_ps",
                "object_class": "confirmed_planet",
                "identifier": row["pl_name"],
                "snapshot_hash": snapshot_hash,
                "period_days": period,
                "epoch_bjd": epoch,
                "duration_hours": duration,
                "period_uncertainty_days": _worst(row, "pl_orbpererr1", "pl_orbpererr2"),
                "epoch_uncertainty_days": _worst(row, "pl_tranmiderr1", "pl_tranmiderr2"),
                "disposition": None,
                "ra_deg": _f(row.get("ra")),
                "dec_deg": _f(row.get("dec")),
                "tmag": _f(row.get("sy_tmag")),
            }
        )
    return _stratify(usable, limit)


def toi_entries(limit: int) -> list[dict[str, Any]]:
    rows, snapshot_hash = load("nasa_toi")
    usable = []
    for row in rows:
        tic = _int(row.get("tid"))
        period, epoch, duration = (
            _f(row.get("pl_orbper")),
            _f(row.get("pl_tranmid")),
            _f(row.get("pl_trandurh")),
        )
        if not (tic and period and epoch and duration and period > 0):
            continue
        usable.append(
            {
                "id": f"toi:{row['toi']}",
                "tic_id": tic,
                "source": "nasa_toi",
                "object_class": "toi",
                "identifier": str(row["toi"]),
                "snapshot_hash": snapshot_hash,
                "period_days": period,
                "epoch_bjd": epoch,
                "duration_hours": duration,
                "period_uncertainty_days": _worst(row, "pl_orbpererr1", "pl_orbpererr2"),
                "epoch_uncertainty_days": _worst(row, "pl_tranmiderr1", "pl_tranmiderr2"),
                "disposition": (str(row.get("tfopwg_disp")).strip() or None),
                "ra_deg": _f(row.get("ra")),
                "dec_deg": _f(row.get("dec")),
                "tmag": _f(row.get("st_tmag")),
            }
        )
    return _stratify(usable, limit)


def eclipsing_binary_entries(limit: int) -> list[dict[str, Any]]:
    rows, snapshot_hash = load("tess_eb")
    usable = []
    for row in rows:
        tic = _int(row.get("TIC"))
        period, epoch = _f(row.get("Per")), _f(row.get("BJD0"))
        width_phase = _f(row.get("Wp-pf"))
        if not (tic and period and epoch and period > 0):
            continue
        # The catalogue publishes eclipse width as a phase fraction.
        duration = (width_phase * period * 24.0) if width_phase else None
        usable.append(
            {
                "id": f"eb:{tic}",
                "tic_id": tic,
                "source": "tess_eb",
                "object_class": "eclipsing_binary",
                "identifier": str(tic),
                "snapshot_hash": snapshot_hash,
                "period_days": period,
                "epoch_bjd": epoch,
                "duration_hours": duration,
                "period_uncertainty_days": _f(row.get("e_Per")),
                "epoch_uncertainty_days": _f(row.get("e_BJD0")),
                "disposition": (str(row.get("Morph")).strip() or None),
                "ra_deg": _f(row.get("RAJ2000")),
                "dec_deg": _f(row.get("DEJ2000")),
                "tmag": _f(row.get("Tmag")),
            }
        )
    return _stratify(usable, limit)


def _worst(row: dict[str, str], *keys: str) -> float | None:
    values = [abs(v) for v in (_f(row.get(key)) for key in keys) if v is not None]
    return max(values) if values else None


def _stratify(entries: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Spread the selection across the period range, deterministically.

    Sorting by period and walking with a fixed stride avoids both a random
    seed and the trap of taking the first N rows, which would sample one
    corner of parameter space (the archive's own ordering is not physical).
    """

    ordered = sorted(entries, key=lambda item: (item["period_days"], item["id"]))
    if len(ordered) <= limit:
        return ordered
    stride = len(ordered) / limit
    return [ordered[int(index * stride)] for index in range(limit)]


def as_candidate(entry: dict[str, Any], **overrides) -> dict[str, Any]:
    candidate = {
        "period_days": entry["period_days"],
        "epoch_btjd": adjudicate.to_btjd(entry["epoch_bjd"]),
        "duration_hours": entry["duration_hours"] or 2.0,
    }
    candidate.update(overrides)
    return candidate


def build_cases(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Real objects at their own ephemeris, plus three families of near-miss."""

    cases: list[dict[str, Any]] = []
    for entry in entries:
        cases.append(
            {
                "case_id": f"self:{entry['id']}",
                "kind": "true_match",
                "catalog": entry,
                "candidate": as_candidate(entry),
                "expected": {
                    "relation": adjudicate.RELATION_EXACT,
                    "epoch_verdict": adjudicate.EPOCH_AGREES,
                    "match_level": adjudicate.MATCH_SIGNAL,
                    "kills": True,
                },
                "rationale": (
                    "the candidate is the catalogued object's own ephemeris; "
                    "period and epoch must both agree"
                ),
            }
        )

    impostor_pool = [item for item in entries if item["duration_hours"]]
    phase_shifted = _stratify(impostor_pool, IMPOSTORS_PER_KIND)
    for entry in phase_shifted:
        cases.append(
            {
                "case_id": f"phase:{entry['id']}",
                "kind": "impostor_phase_distinct",
                "catalog": entry,
                "candidate": as_candidate(
                    entry,
                    epoch_btjd=adjudicate.to_btjd(entry["epoch_bjd"])
                    + entry["period_days"] / 2.0,
                ),
                "expected": {
                    "relation": adjudicate.RELATION_EXACT,
                    "epoch_verdict": adjudicate.EPOCH_DISAGREES,
                    "match_level": adjudicate.MATCH_HOST,
                    "kills": False,
                },
                "rationale": (
                    "same period, transits half a period away: a different "
                    "signal on a catalogued star, which the period-only rule "
                    "would have killed"
                ),
            }
        )

    detuned = _stratify(
        [item for item in impostor_pool if item not in phase_shifted],
        IMPOSTORS_PER_KIND,
    )
    for entry in detuned:
        cases.append(
            {
                "case_id": f"detuned:{entry['id']}",
                "kind": "impostor_period_detuned",
                "catalog": entry,
                "candidate": as_candidate(
                    entry, period_days=entry["period_days"] * 1.05
                ),
                "expected": {
                    "relation": adjudicate.RELATION_NONE,
                    "match_level": adjudicate.MATCH_HOST,
                    "kills": False,
                },
                "rationale": (
                    "5% period offset is outside every rung of the alias "
                    "ladder; the star is catalogued, the signal is not"
                ),
            }
        )

    aliased = _stratify(
        [
            item
            for item in impostor_pool
            if item not in phase_shifted and item not in detuned
        ],
        IMPOSTORS_PER_KIND,
    )
    for entry in aliased:
        cases.append(
            {
                "case_id": f"alias:{entry['id']}",
                "kind": "true_match_half_period_alias",
                "catalog": entry,
                "candidate": as_candidate(
                    entry, period_days=entry["period_days"] / 2.0
                ),
                "expected": {
                    "relation": "half_period_alias",
                    "epoch_verdict": adjudicate.EPOCH_AGREES,
                    "match_level": adjudicate.MATCH_SIGNAL,
                    "kills": True,
                },
                "rationale": (
                    "a half-period alias of a catalogued signal is the same "
                    "signal; every event still lands on a catalogued transit"
                ),
            }
        )
    return cases


def check(case: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    entry = case["catalog"]
    relation = adjudicate.relate(
        adjudicate.Candidate(tic_id=entry["tic_id"], **case["candidate"]),
        adjudicate.CatalogEphemeris(
            source=entry["source"],
            identifier=entry["identifier"],
            object_class=entry["object_class"],
            snapshot_hash=entry["snapshot_hash"],
            period_days=entry["period_days"],
            epoch_bjd=entry["epoch_bjd"],
            duration_hours=entry["duration_hours"],
            disposition=entry["disposition"],
            period_uncertainty_days=entry["period_uncertainty_days"],
            epoch_uncertainty_days=entry["epoch_uncertainty_days"],
        ),
    )
    actual = relation.to_dict()
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in case["expected"].items()
        if actual.get(key) != value
    }
    return not mismatches, mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("results/p4/known_objects_v1"))
    parser.add_argument(
        "--keep-disagreements",
        action="store_true",
        help="Freeze cases the current code disagrees with (for triage only).",
    )
    args = parser.parse_args(argv)

    entries = (
        confirmed_planet_entries(CONFIRMED_PLANETS)
        + toi_entries(TOIS)
        + eclipsing_binary_entries(ECLIPSING_BINARIES)
    )
    cases = build_cases(entries)

    agreeing: list[dict[str, Any]] = []
    disagreeing: list[dict[str, Any]] = []
    for case in cases:
        ok, mismatches = check(case)
        (agreeing if ok else disagreeing).append(
            case if ok else {**case, "observed_mismatch": mismatches}
        )

    print(f"cases built:      {len(cases)}")
    print(f"intent reproduced:{len(agreeing):>5}")
    print(f"disagreements:    {len(disagreeing):>5}")
    by_kind: dict[str, int] = {}
    for case in disagreeing:
        by_kind[case["kind"]] = by_kind.get(case["kind"], 0) + 1
    for kind, count in sorted(by_kind.items()):
        print(f"   {kind}: {count}")
    for case in disagreeing[:10]:
        print(f"   e.g. {case['case_id']}: {case['observed_mismatch']}")

    frozen = agreeing + (disagreeing if args.keep_disagreements else [])
    args.out.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SUITE_SCHEMA_VERSION,
        "vetting_signature": vetting_signature(
            code="modules:" + module_digest("adjudicate.py", "identity.py", "snapshots.py"),
            identity=CURRENT_IDENTITY,
            matching=CURRENT_EPHEMERIS_MATCH,
            snapshots=snapshots.snapshot_hashes(),
        ),
        "snapshot_hashes": snapshots.snapshot_hashes(),
        "counts": {
            "cases": len(frozen),
            "built": len(cases),
            "disagreements_excluded": 0 if args.keep_disagreements else len(disagreeing),
            "by_kind": _counts(frozen),
        },
        "cases": frozen,
    }
    suite_path = args.out / "known_objects.json"
    suite_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"[written] {suite_path} ({len(frozen)} cases)")

    positions = args.out / "positions.csv"
    with open(positions, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["tic_id", "ra", "dec", "tmag"])
        seen: set[int] = set()
        for entry in entries:
            if entry["tic_id"] in seen or entry["ra_deg"] is None:
                continue
            seen.add(entry["tic_id"])
            writer.writerow(
                [entry["tic_id"], entry["ra_deg"], entry["dec_deg"], entry["tmag"] or ""]
            )
    print(f"[written] {positions} ({len(seen)} positions)")

    if disagreeing and not args.keep_disagreements:
        report = args.out / "disagreements.json"
        report.write_text(
            json.dumps(disagreeing, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"[written] {report}")
    return 0


def _counts(cases: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["kind"]] = counts.get(case["kind"], 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
