"""Import the existing file-based history into the evidence ledger.

Seventeen campaigns, their vetting outputs, the common-mode screen, and every
human outcome already exist as files. This importer walks them **in exactly
the order and scope the dashboard exporter reads them**, so the ledger's
projection reproduces the exporter's status counts row for row -- the Phase 1
parity gate from MASTER_PLAN.md. It deliberately reuses the exporter's own
loader helpers rather than re-implementing their semantics.

Two kinds of rows come out:

* **Effective rows** (``affects_state=1``): the evidence the exporter would
  select today -- the newest screening row per star, the newest context /
  science / common-mode verdicts, and every human outcome. These drive the
  status projection.
* **History rows** (``affects_state=0``): everything else worth preserving --
  screening rows superseded by later campaigns, rows from non-active
  checkpoints (including the interrupted ``sector100_spoc`` v3 re-run), and
  per-target reports of campaigns that never wrote a summary (the 1,864
  ``processed-lc-v2`` + 26 ``v3-edge-safe`` mixed-version record). They keep
  the full historical reading without voting on current state.

Every row records the legacy pipeline version as its signature, so summaries
partition by scientific identity from the first day of the ledger.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import ledger
from .dashboard import (
    _common_mode_by_tic,
    _common_mode_notes,
    _science_notes,
    _science_vetting_by_tic,
    _screening_class,
    _sectors,
    _tic_id,
)
from .statuses import (
    COMMON_MODE_LABELS,
    CONTEXT_LABELS,
    SCIENCE_LABELS,
    SCREENING_LABELS,
    STATUS_REGISTRY,
)

ACTIVE_PROGRESS_STATES = {"running", "finalizing", "retry_pending"}


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _row_signature(row: dict[str, Any], fallback: str | None) -> str:
    version = str(
        row.get("data_pipeline_version") or fallback or "legacy_unversioned"
    )
    return f"legacy:{version}"


def _settings_version(container: dict[str, Any]) -> str | None:
    settings = container.get("settings")
    if isinstance(settings, dict):
        value = settings.get("data_pipeline_version")
        return str(value) if value else None
    return None


def _screening_payload(row: dict[str, Any], verdict: str) -> dict[str, Any]:
    return {
        "label": SCREENING_LABELS.get(verdict, verdict),
        "notes": str(row.get("followup_reasons") or ""),
        "result": row,
    }


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def import_workspace(
    conn: sqlite3.Connection,
    workspace: str | Path = ".",
    *,
    include_orphan_reports: bool = True,
    rebuild: bool = True,
) -> dict[str, Any]:
    """Import the workspace's file history; return an import report."""

    root = Path(workspace).resolve()
    results_root = root / "results"
    report: dict[str, Any] = {
        "workspace": str(root),
        "evidence_added": 0,
        "evidence_already_present": 0,
        "stars_seen": 0,
        "campaign_summaries": 0,
        "progress_files": 0,
        "orphan_reports": 0,
        "context_winners": 0,
        "science_winners": 0,
        "common_mode_rows": 0,
        "human_outcomes": 0,
        "history_rows": 0,
    }

    def add(
        *,
        tic_id: int,
        kind: str,
        source: str,
        payload: dict[str, Any],
        verdict: str | None,
        affects_state: bool,
        signature: str | None,
    ) -> int | None:
        row_id = ledger.append_evidence(
            conn,
            tic_id=tic_id,
            kind=kind,
            source=source,
            payload=payload,
            verdict=verdict,
            affects_state=affects_state,
            signature=signature,
        )
        if row_id is None:
            report["evidence_already_present"] += 1
        else:
            report["evidence_added"] += 1
        if not affects_state:
            report["history_rows"] += 1
        return row_id

    # ------------------------------------------------------------------
    # 1. The metrics ledger: event order defines screening precedence.
    # ------------------------------------------------------------------
    events: list[dict[str, Any]] = []
    events_path = root / "metrics" / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    invalidated = {
        str(event.get("invalidates_event_id"))
        for event in events
        if event.get("kind") == "event_invalidated"
        and event.get("invalidates_event_id")
    }
    active_events = [
        event for event in events if event.get("event_id") not in invalidated
    ]

    # Track, per star, the screening row the exporter would select: the last
    # summary row in event order, overridden by rows from active checkpoints.
    screening_winner: dict[int, int] = {}

    def record_screening(
        tic_id: int,
        row: dict[str, Any],
        *,
        source: str,
        signature: str,
        eligible_winner: bool,
    ) -> None:
        verdict = _screening_class(row)
        if verdict not in SCREENING_LABELS:
            verdict = "searched"
        row_id = add(
            tic_id=tic_id,
            kind="screening",
            source=source,
            payload=_screening_payload(row, verdict),
            verdict=verdict,
            affects_state=False,
            signature=signature,
        )
        if eligible_winner:
            if row_id is None:
                row_id = conn.execute(
                    "SELECT evidence_id FROM evidence "
                    "WHERE tic_id = ? AND kind = 'screening' AND source = ?",
                    (tic_id, source),
                ).fetchone()[0]
            screening_winner[tic_id] = int(row_id)

    for event in active_events:
        if event.get("kind") != "campaign_completed":
            continue
        summary_path = root / str(event.get("summary_path") or "")
        summary = _read_json(summary_path)
        if not isinstance(summary, dict):
            continue
        report["campaign_summaries"] += 1
        fallback_version = _settings_version(summary)
        source_stem = (
            f"summary:{_relative(summary_path, root)}"
            f"#event:{event.get('event_id')}"
        )
        for row in summary.get("results", []):
            if not isinstance(row, dict):
                continue
            tic_id = _tic_id(row)
            if tic_id is None:
                continue
            record_screening(
                tic_id,
                row,
                source=source_stem,
                signature=_row_signature(row, fallback_version),
                eligible_winner=True,
            )

    # ------------------------------------------------------------------
    # 2. Worker checkpoints: active ones override; the rest is history.
    # ------------------------------------------------------------------
    summarized_dirs: set[Path] = set()
    if results_root.exists():
        for summary_path in results_root.rglob("batch_summary.json"):
            summarized_dirs.add(summary_path.parent.resolve())
        for progress_path in sorted(results_root.rglob("batch_progress.json")):
            progress = _read_json(progress_path)
            if not isinstance(progress, dict):
                continue
            report["progress_files"] += 1
            state = str(progress.get("state") or "")
            is_active = state in ACTIVE_PROGRESS_STATES
            fallback_version = _settings_version(progress)
            source_stem = f"progress:{_relative(progress_path, root)}"
            for row in progress.get("results", []):
                if not isinstance(row, dict):
                    continue
                tic_id = _tic_id(row)
                if tic_id is None:
                    continue
                record_screening(
                    tic_id,
                    row,
                    source=source_stem,
                    signature=_row_signature(row, fallback_version),
                    eligible_winner=is_active,
                )
            if include_orphan_reports and (
                progress_path.parent.resolve() not in summarized_dirs
            ):
                # Campaigns that never wrote a summary (interrupted runs)
                # leave durable per-target reports the checkpoint does not
                # carry -- the mixed-version record lives here.
                for report_path in sorted(
                    progress_path.parent.glob("*_residual.json")
                ):
                    target_report = _read_json(report_path)
                    if not isinstance(target_report, dict):
                        continue
                    data = target_report.get("data")
                    data = data if isinstance(data, dict) else {}
                    try:
                        tic_id = int(data.get("tic_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if tic_id <= 0:
                        continue
                    configuration = target_report.get("search_configuration")
                    configuration = (
                        configuration if isinstance(configuration, dict) else {}
                    )
                    version = configuration.get("data_pipeline_version")
                    add(
                        tic_id=tic_id,
                        kind="screening_report",
                        source=f"report:{_relative(report_path, root)}",
                        payload={
                            "search_configuration": configuration,
                            "strongest_residual_signal": target_report.get(
                                "strongest_residual_signal"
                            ),
                            "automated_triage": target_report.get(
                                "automated_triage"
                            ),
                        },
                        verdict=None,
                        affects_state=False,
                        signature=f"legacy:{version or 'legacy_unversioned'}",
                    )
                    report["orphan_reports"] += 1

    for tic_id, evidence_id in screening_winner.items():
        ledger.set_affects_state(conn, evidence_id, True)

    # ------------------------------------------------------------------
    # 3. Context vetting: newest report per star, exporter semantics.
    # ------------------------------------------------------------------
    context_winner: dict[int, tuple[str, Path, dict[str, Any]]] = {}
    if results_root.exists():
        for context_path in sorted(
            results_root.rglob("TIC_*_cross_mission_context.json")
        ):
            context = _read_json(context_path)
            if not isinstance(context, dict):
                continue
            tic = context.get("tic", {})
            classification = context.get("context_classification", {})
            try:
                tic_id = int(tic.get("tic_id", 0)) if isinstance(tic, dict) else 0
            except (TypeError, ValueError):
                continue
            if (
                tic_id <= 0
                or not isinstance(classification, dict)
                or classification.get("disposition") not in CONTEXT_LABELS
            ):
                continue
            generated = str(context.get("generated_at_utc") or "")
            previous = context_winner.get(tic_id)
            if previous is None or generated >= previous[0]:
                context_winner[tic_id] = (generated, context_path, classification)
    for tic_id, (generated, context_path, classification) in sorted(
        context_winner.items()
    ):
        disposition = str(classification.get("disposition"))
        add(
            tic_id=tic_id,
            kind="context",
            source=f"context:{_relative(context_path, root)}",
            payload={
                "label": CONTEXT_LABELS[disposition],
                "notes": "; ".join(
                    str(value)
                    for value in classification.get("reasons", [])
                    if str(value).strip()
                ),
                "classification": classification,
                "generated_at_utc": generated,
            },
            verdict=disposition,
            affects_state=True,
            signature=None,
        )
        report["context_winners"] += 1

    # ------------------------------------------------------------------
    # 4. Measured science and the population screen: exporter loaders.
    # ------------------------------------------------------------------
    for tic_id, science in sorted(_science_vetting_by_tic(results_root).items()):
        disposition = science.get("science_disposition")
        in_registry = (
            disposition is not None
            and str(disposition) in SCIENCE_LABELS
        )
        add(
            tic_id=tic_id,
            kind="science",
            source=(
                "science:"
                + str(
                    science.get("science_pixel_report")
                    or science.get("science_sector_report")
                    or f"tic:{tic_id}"
                )
            ),
            payload={
                "label": (
                    SCIENCE_LABELS[str(disposition)]
                    if in_registry
                    else "Measured science evidence"
                ),
                "notes": _science_notes(science),
                "science": science,
            },
            verdict=str(disposition) if in_registry else None,
            affects_state=in_registry,
            signature=None,
        )
        report["science_winners"] += int(in_registry)

    for tic_id, screen in sorted(_common_mode_by_tic(results_root).items()):
        verdict = str(screen.get("verdict") or "")
        in_registry = verdict in COMMON_MODE_LABELS
        add(
            tic_id=tic_id,
            kind="common_mode",
            source=f"common_mode:{screen.get('screen_report')}#tic:{tic_id}",
            payload={
                "label": COMMON_MODE_LABELS.get(verdict, verdict),
                "notes": _common_mode_notes(screen),
                "screen": screen,
            },
            verdict=verdict if in_registry else None,
            affects_state=in_registry,
            signature=None,
        )
        report["common_mode_rows"] += 1

    # ------------------------------------------------------------------
    # 5. Human outcomes: every event votes, in ledger order.
    # ------------------------------------------------------------------
    for event in active_events:
        if event.get("tic_id") is None:
            continue
        try:
            tic_id = int(event["tic_id"])
        except (TypeError, ValueError):
            continue
        kind = str(event.get("kind") or "")
        in_registry = kind in STATUS_REGISTRY
        add(
            tic_id=tic_id,
            kind="human_outcome",
            source=f"events.jsonl:{event.get('event_id')}",
            payload={
                "label": event.get("label"),
                "notes": event.get("notes"),
                "outcome_kind": kind,
                "source_field": event.get("source"),
                "timestamp_utc": event.get("timestamp_utc"),
            },
            verdict=kind if in_registry else None,
            affects_state=in_registry,
            signature=None,
        )
        report["human_outcomes"] += 1

    # ------------------------------------------------------------------
    # 6. Star metadata, then the projection.
    # ------------------------------------------------------------------
    searched = {
        row["tic_id"]
        for row in conn.execute("SELECT DISTINCT tic_id FROM evidence")
    }
    report["stars_seen"] = len(searched)
    for tic_id in searched:
        ledger.upsert_star(conn, tic_id, name=f"TIC {tic_id}")
    _import_star_metadata(conn, root, searched)
    conn.commit()
    if rebuild:
        report["status_counts"] = ledger.rebuild_star_state(conn)
    return report


def _optional_float(value: object) -> float | None:
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _import_star_metadata(
    conn: sqlite3.Connection, root: Path, searched: set[int]
) -> None:
    """Enrich star rows from target lists and the TIC display cache."""

    import csv

    for csv_path in sorted((root / "targets").glob("*.csv")):
        try:
            with csv_path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    tic_id = _tic_id(row)
                    if tic_id is None or tic_id not in searched:
                        continue
                    ledger.upsert_star(
                        conn,
                        tic_id,
                        name=str(row.get("target") or "") or None,
                        ra_deg=_optional_float(row.get("ra_deg") or row.get("ra")),
                        dec_deg=_optional_float(
                            row.get("dec_deg") or row.get("dec")
                        ),
                        distance_pc=_optional_float(
                            row.get("distance_pc") or row.get("d")
                        ),
                        tmag=_optional_float(row.get("tmag") or row.get("Tmag")),
                        teff_k=_optional_float(
                            row.get("teff_k") or row.get("Teff")
                        ),
                        stellar_radius_solar=_optional_float(
                            row.get("stellar_radius_solar") or row.get("rad")
                        ),
                    )
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    cache = _read_json(root / "data" / "dashboard_tic_catalog.json")
    if isinstance(cache, list):
        for row in cache:
            if not isinstance(row, dict):
                continue
            try:
                tic_id = int(row.get("tic_id") or 0)
            except (TypeError, ValueError):
                continue
            if tic_id not in searched:
                continue
            ledger.upsert_star(
                conn,
                tic_id,
                ra_deg=_optional_float(row.get("ra_deg")),
                dec_deg=_optional_float(row.get("dec_deg")),
                distance_pc=_optional_float(row.get("distance_pc")),
                tmag=_optional_float(row.get("tmag")),
                teff_k=_optional_float(row.get("teff_k")),
                stellar_radius_solar=_optional_float(
                    row.get("stellar_radius_solar")
                ),
            )


# Evidence kinds that exist only in the ledger. The dashboard exporter walks
# campaign result files and cannot see these, so a star whose status they
# decide will legitimately differ between the two projections.
#
# Adding a kind here weakens the parity gate, so each one needs a reason:
#   t5_readjudication -- P4 catalog adjudication, promoted to voting by owner
#                        decision 3. Lives in the ledger; no campaign file
#                        carries it.
#   search_artifact   -- owner decision 2a's artifact vetoes, derived from the
#                        common-mode screen's recorded flags.
LEDGER_ONLY_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"t5_readjudication", "search_artifact"}
)


def parity_check(
    conn: sqlite3.Connection, workspace: str | Path = "."
) -> dict[str, Any]:
    """Compare the ledger projection against a fresh exporter run.

    This is the Phase 1 gate: the two independent paths -- files walked by the
    exporter, and the same files imported into the ledger -- must produce
    identical per-status star counts.
    """

    from .dashboard import export_dashboard_data

    root = Path(workspace).resolve()
    output = export_dashboard_data(root)
    if output is None:
        raise RuntimeError(
            f"{root} has no dashboard directory; cannot export survey.json."
        )
    payload = json.loads(Path(output).read_text(encoding="utf-8"))
    exporter_counts: dict[str, int] = {
        str(key): int(value)
        for key, value in payload.get("status_counts", {}).items()
    }
    exporter_star_statuses = {
        int(row["tic_id"]): str(row["status"])
        for row in payload.get("stars", [])
        if isinstance(row, dict)
        and row.get("tic_id") is not None
        and row.get("status") is not None
    }
    ledger_counts = ledger.status_counts(conn)
    from .dashboard_api import all_star_payloads

    api_star_rows = all_star_payloads(conn)
    api_star_statuses = {
        int(row["tic_id"]): str(row["status"])
        for row in api_star_rows
    }
    api_stars = {
        int(row["tic_id"]): row for row in api_star_rows
    }
    exporter_stars = {
        int(row["tic_id"]): row
        for row in payload.get("stars", [])
        if isinstance(row, dict) and row.get("tic_id") is not None
    }
    statuses = sorted(set(exporter_counts) | set(ledger_counts))
    differences = {
        status: {
            "exporter": exporter_counts.get(status, 0),
            "ledger": ledger_counts.get(status, 0),
        }
        for status in statuses
        if exporter_counts.get(status, 0) != ledger_counts.get(status, 0)
    }
    all_status_differences = {
        str(tic_id): {
            "exporter": exporter_star_statuses.get(tic_id),
            "ledger_api": api_star_statuses.get(tic_id),
        }
        for tic_id in sorted(
            set(exporter_star_statuses) | set(api_star_statuses)
        )
        if exporter_star_statuses.get(tic_id)
        != api_star_statuses.get(tic_id)
    }

    # Correction 50 / owner decision 3. The exporter is a *file-derived*
    # projection: it walks campaign reports. The ledger projects those same
    # files plus evidence that exists only in the ledger -- promoted T5
    # adjudications, and the decision-2a search-artifact vetoes. Once the
    # ledger holds a verdict no file contains, the two cannot agree, and that
    # is the system working rather than drifting.
    #
    # So the gate does not compare the two totals and declare a failure. It
    # separates the differences the ledger can *account for* from the ones it
    # cannot, and only the unaccounted ones fail. `star_state.decided_by_
    # evidence_id` names the exact row that decided each status, so this is a
    # lookup rather than an inference.
    #
    # The rule this replaces edited the gate until it passed, and was reverted
    # (correction 50). The difference is that this itemizes the divergence
    # instead of hiding it: `explained_status_differences` stays in the report
    # and is counted.
    decided_by_ledger_only: set[int] = set()
    try:
        for row in conn.execute(
            "SELECT s.tic_id AS tic_id FROM star_state s "
            "JOIN evidence e ON e.evidence_id = s.decided_by_evidence_id "
            f"WHERE e.kind IN ({','.join('?' * len(LEDGER_ONLY_EVIDENCE_KINDS))})",
            tuple(sorted(LEDGER_ONLY_EVIDENCE_KINDS)),
        ):
            decided_by_ledger_only.add(int(row["tic_id"]))
    except sqlite3.Error:
        # An older ledger without these kinds simply has none of them.
        decided_by_ledger_only = set()

    explained_status_differences = {
        tic: diff
        for tic, diff in all_status_differences.items()
        if int(tic) in decided_by_ledger_only
    }
    star_status_differences = {
        tic: diff
        for tic, diff in all_status_differences.items()
        if int(tic) not in decided_by_ledger_only
    }
    # context_report was a raw filesystem path in survey.json and is not
    # consumed by the browser.  The API deliberately exposes evidence source
    # provenance through /api/star/{tic} instead.  Every other legacy display
    # field must remain byte-for-byte equivalent on frozen inputs.
    ignored_star_fields = {"context_report"}
    star_payload_differences: dict[str, dict[str, Any]] = {}
    explained_payload_differences: dict[str, dict[str, Any]] = {}
    for tic_id in sorted(set(exporter_stars) | set(api_stars)):
        exported = exporter_stars.get(tic_id)
        projected = api_stars.get(tic_id)
        if exported is None or projected is None:
            # Presence is never "explained": a star the ledger holds and the
            # exporter does not (or vice versa) is a real import defect
            # regardless of which evidence decided its status.
            star_payload_differences[str(tic_id)] = {
                "exporter_present": exported is not None,
                "ledger_api_present": projected is not None,
            }
            continue
        if tic_id in decided_by_ledger_only:
            # The status field, and anything the exporter derives from it,
            # necessarily differs here. Recorded, not counted against parity.
            field_differences = {
                field: {
                    "exporter": exported.get(field),
                    "ledger_api": projected.get(field),
                }
                for field in sorted(set(exported) - ignored_star_fields)
                if exported.get(field) != projected.get(field)
            }
            if field_differences:
                explained_payload_differences[str(tic_id)] = field_differences
            continue
        field_differences = {
            field: {
                "exporter": exported.get(field),
                "ledger_api": projected.get(field),
            }
            for field in sorted(set(exported) - ignored_star_fields)
            if exported.get(field) != projected.get(field)
        }
        if field_differences:
            star_payload_differences[str(tic_id)] = field_differences
    # `differences` is a per-status *count* rollup of the same stars compared
    # above, so it cannot disagree unless a star does. It stays in the report
    # for readability but is not a second, independent gate -- keying `match`
    # on it would re-fail every star already accounted for above.
    return {
        "match": (
            not star_status_differences and not star_payload_differences
        ),
        "exporter_total": sum(exporter_counts.values()),
        "ledger_total": sum(ledger_counts.values()),
        "exporter_counts": exporter_counts,
        "ledger_counts": ledger_counts,
        "differences": differences,
        "star_status_differences": star_status_differences,
        "star_payload_differences": star_payload_differences,
        # Divergence the ledger accounts for: stars whose current status was
        # decided by evidence that exists only in the ledger and therefore
        # cannot appear in a file-derived export. Itemized so the number is
        # visible rather than absorbed.
        "explained_status_differences": explained_status_differences,
        "explained_payload_differences": explained_payload_differences,
        "explained_by_ledger_only_evidence": len(explained_status_differences),
        "ledger_only_evidence_kinds": sorted(LEDGER_ONLY_EVIDENCE_KINDS),
    }
