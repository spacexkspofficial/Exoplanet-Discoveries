"""Lock the golden search-grid cohort with saved stellar parameters."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(
    source_csv: Path,
    *,
    context_dir: Path,
    output_csv: Path,
    limit: int = 150,
    context_label: str | None = None,
) -> dict[str, object]:
    with source_csv.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)[:limit]
    if not rows:
        raise ValueError("Source target CSV contains no rows.")
    required = {"target", "tic_id", "sectors"}
    if not required.issubset(fieldnames):
        raise ValueError("Source target CSV lacks required identity columns.")
    if "stellar_mass_solar" not in fieldnames:
        radius_index = (
            fieldnames.index("stellar_radius_solar") + 1
            if "stellar_radius_solar" in fieldnames
            else len(fieldnames)
        )
        fieldnames.insert(radius_index, "stellar_mass_solar")

    complete = 0
    missing_context = 0
    incomplete_context = 0
    for row in rows:
        tic_id = int(row["tic_id"])
        context_path = (
            context_dir / f"TIC_{tic_id}_cross_mission_context.json"
        )
        if not context_path.exists():
            row["stellar_mass_solar"] = ""
            missing_context += 1
            continue
        context = json.loads(context_path.read_text(encoding="utf-8"))
        tic = context.get("tic")
        tic = tic if isinstance(tic, dict) else {}
        radius = tic.get("stellar_radius_solar")
        mass = tic.get("stellar_mass_solar")
        if radius is None or mass is None:
            row["stellar_mass_solar"] = ""
            incomplete_context += 1
            continue
        row["stellar_radius_solar"] = str(radius)
        row["stellar_mass_solar"] = str(mass)
        complete += 1

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    # Keep this byte-for-byte compatible with the frozen golden manifest.
    identity = "\n".join(
        f"{row['target']}|{int(row['tic_id'])}|{row['sectors']}"
        for row in rows
    ).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "scope": (
            "same ordered TIC/sector identities as the first 150 frozen "
            "golden targets, enriched only from saved context metadata"
        ),
        "source_csv": str(source_csv),
        "source_csv_sha256": _sha256(source_csv),
        "context_dir": context_label or str(context_dir),
        "output_csv": str(output_csv),
        "output_csv_sha256": _sha256(output_csv),
        "cohort_rows": len(rows),
        "cohort_identity_sha256": hashlib.sha256(identity).hexdigest(),
        "complete_mass_and_radius": complete,
        "solar_density_fallback": len(rows) - complete,
        "missing_context": missing_context,
        "incomplete_context": incomplete_context,
    }
    output_csv.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-csv", required=True, type=Path)
    parser.add_argument("--context-dir", required=True, type=Path)
    parser.add_argument("--output-csv", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument(
        "--context-label",
        help="Portable provenance label to record instead of a local path.",
    )
    args = parser.parse_args()
    result = build(
        args.source_csv,
        context_dir=args.context_dir,
        output_csv=args.output_csv,
        limit=args.limit,
        context_label=args.context_label,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
