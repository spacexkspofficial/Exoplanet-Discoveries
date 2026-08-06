"""Freeze the P3 release-gate cohort without looking at search outcomes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity_hash(rows: list[dict[str, str]]) -> str:
    identity = "\n".join(
        f"{row['target']}|{int(row['tic_id'])}|{row['sectors']}" for row in rows
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build(
    source_csv: Path,
    *,
    output_csv: Path,
    limit: int = 500,
    enriched_prefix_csv: Path | None = None,
) -> dict[str, object]:
    """Select the first ``limit`` ranked rows and publish their provenance.

    The selection is intentionally positional.  It extends the previously
    frozen first-150 P2 cohort and cannot be influenced by any search result.
    If that P2 file is supplied, its stellar-mass enrichment is copied only
    for identities that match at the same position.
    """

    with source_csv.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)[:limit]
    if len(rows) != limit:
        raise ValueError(
            f"Source contains {len(rows)} rows; {limit} are required for the lock."
        )
    required = {"target", "tic_id", "sectors"}
    if not required.issubset(fieldnames):
        raise ValueError("Source target CSV lacks required identity columns.")
    if len({int(row["tic_id"]) for row in rows}) != len(rows):
        raise ValueError("Locked cohort contains duplicate TIC IDs.")

    if "stellar_mass_solar" not in fieldnames:
        radius_index = (
            fieldnames.index("stellar_radius_solar") + 1
            if "stellar_radius_solar" in fieldnames
            else len(fieldnames)
        )
        fieldnames.insert(radius_index, "stellar_mass_solar")
    for row in rows:
        row.setdefault("stellar_mass_solar", "")

    copied_mass_rows = 0
    prefix_hash = None
    if enriched_prefix_csv is not None:
        with enriched_prefix_csv.open(newline="", encoding="utf-8-sig") as handle:
            prefix_rows = list(csv.DictReader(handle))
        if len(prefix_rows) > len(rows):
            raise ValueError("Enriched prefix is longer than the locked cohort.")
        for index, prefix in enumerate(prefix_rows):
            identity = (prefix["target"], prefix["tic_id"], prefix["sectors"])
            locked_identity = (
                rows[index]["target"],
                rows[index]["tic_id"],
                rows[index]["sectors"],
            )
            if identity != locked_identity:
                raise ValueError(
                    f"Enriched prefix identity mismatch at row {index + 1}."
                )
            mass = str(prefix.get("stellar_mass_solar") or "").strip()
            if mass:
                rows[index]["stellar_mass_solar"] = mass
                copied_mass_rows += 1
        prefix_hash = _sha256(enriched_prefix_csv)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    manifest = {
        "schema_version": 1,
        "status": "locked_before_p3_outcome_measurement",
        "selection_rule": (
            f"first {limit} rows in the existing small-planet-merit ranking; "
            "no search outcomes consulted"
        ),
        "source_csv": str(source_csv),
        "source_csv_sha256": _sha256(source_csv),
        "output_csv": str(output_csv),
        "output_csv_sha256": _sha256(output_csv),
        "cohort_rows": len(rows),
        "unique_tic_ids": len({int(row["tic_id"]) for row in rows}),
        "cohort_identity_sha256": _identity_hash(rows),
        "extends_p2_first_150": bool(enriched_prefix_csv),
        "enriched_prefix_csv": (
            str(enriched_prefix_csv) if enriched_prefix_csv else None
        ),
        "enriched_prefix_sha256": prefix_hash,
        "copied_stellar_mass_rows": copied_mass_rows,
        "stellar_radius_rows": sum(
            bool(str(row.get("stellar_radius_solar") or "").strip()) for row in rows
        ),
        "stellar_mass_rows": sum(
            bool(str(row.get("stellar_mass_solar") or "").strip()) for row in rows
        ),
    }
    manifest_path = output_csv.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-csv", required=True, type=Path)
    parser.add_argument("--output-csv", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--enriched-prefix-csv", type=Path)
    args = parser.parse_args()
    manifest = build(
        args.source_csv,
        output_csv=args.output_csv,
        limit=args.limit,
        enriched_prefix_csv=args.enriched_prefix_csv,
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
