"""Combine P3 gate evidence and optionally authorize its exact signature."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exohunt import ledger  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finalize(
    calibration_path: Path,
    known_path: Path,
    *,
    output_path: Path,
    store: bool = False,
    db_path: Path | None = None,
) -> dict[str, object]:
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    known = json.loads(known_path.read_text(encoding="utf-8"))
    signature = str(calibration["scientific_signature"])
    calibration_passes = bool(calibration.get("release_gate_passes"))
    known_passes = bool(known.get("passes"))
    errors = [
        *list(calibration.get("errors") or []),
        *list(known.get("errors") or []),
    ]
    report: dict[str, object] = {
        "schema_version": 1,
        "scientific_signature": signature,
        "code_version": calibration.get("code_version"),
        "status": (
            "trusted_release" if calibration_passes and known_passes and not errors
            else "diagnostic_blocked"
        ),
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "execution_complete": bool(
            calibration.get("execution_complete")
            and int(known.get("counts", {}).get("total", 0)) == 20
        ),
        "errors": errors,
        "calibration_gate": {
            "passes": calibration_passes,
            "signature": signature,
            "counts": calibration.get("counts"),
            "gates": calibration.get("gates"),
            "source": str(calibration_path),
            "sha256": _sha256(calibration_path),
        },
        "known_planet_gate": {
            "passes": known_passes,
            "signature": known.get("scientific_signature"),
            "counts": known.get("counts"),
            "failed_planets": known.get("failed_planets"),
            "source": str(known_path),
            "sha256": _sha256(known_path),
        },
        "artifact_epoch_diagnostic": calibration.get("gates", {}).get(
            "epoch_enrichment"
        ),
    }
    report["release_gate_passes"] = bool(
        report["execution_complete"]
        and calibration_passes
        and known_passes
        and not errors
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    markdown = output_path.with_suffix(".md")
    gate_lines = []
    for name, gate in (calibration.get("gates") or {}).items():
        gate_lines.append(
            f"| {name} | {gate.get('value')} | "
            f"{'PASS' if gate.get('passes') else 'FAIL'} |"
        )
    markdown.write_text(
        "\n".join(
            [
                "# P3 release report",
                "",
                f"Status: **{report['status']}**",
                "",
                f"Scientific signature: `{signature}`",
                "",
                "| Calibration gate | Value | Verdict |",
                "|---|---:|---|",
                *gate_lines,
                "",
                f"Known planets: {known.get('counts', {}).get('passed', 0)}/20 passed.",
                "",
                "This report authorizes only the exact signature above. A failed "
                "or incomplete report remains diagnostic and cannot be stored as trusted.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if store:
        conn = ledger.connect(db_path)
        try:
            ledger.store_release_report(
                conn,
                signature=signature,
                report_path=output_path,
                payload=report,
            )
            conn.commit()
        finally:
            conn.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--known-planets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--store", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    report = finalize(
        args.calibration,
        args.known_planets,
        output_path=args.output,
        store=args.store,
        db_path=args.db,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["release_gate_passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
