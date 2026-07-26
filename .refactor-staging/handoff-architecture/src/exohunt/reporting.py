"""Human-readable campaign and candidate evidence packets.

These reports deliberately distinguish an automated transit signal from a
planet candidate and a confirmed planet.  They collect evidence for expert
review; they do not perform an external submission.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


EXOFOP_HELP_URL = "https://exofop.ipac.caltech.edu/tess/help.php"
EXOFOP_HOME_URL = "https://exofop.ipac.caltech.edu/tess/"
TFOP_JOIN_URL = "https://tess.mit.edu/followup/apply-join-tfop/"
PLANET_HUNTERS_URL = "https://www.zooniverse.org/projects/nora-dot-eisner/planet-hunters-tess"
EXOFOP_SUPPORT = "exofop-support@ipac.caltech.edu"
TESS_BJD_OFFSET = 2_457_000.0


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "report"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    base.add(
        ParagraphStyle(
            name="PacketTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=25,
            textColor=colors.HexColor("#102A43"),
            alignment=TA_CENTER,
            spaceAfter=10,
        )
    )
    base.add(
        ParagraphStyle(
            name="Status",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#8B1E1E"),
            backColor=colors.HexColor("#FDECEC"),
            borderColor=colors.HexColor("#E9A8A8"),
            borderWidth=0.6,
            borderPadding=7,
            spaceAfter=12,
        )
    )
    base.add(
        ParagraphStyle(
            name="Section",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=17,
            textColor=colors.HexColor("#146C72"),
            spaceBefore=12,
            spaceAfter=7,
        )
    )
    base.add(
        ParagraphStyle(
            name="Small",
            parent=base["BodyText"],
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#486581"),
        )
    )
    return base


def _page(canvas, document) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D9E2EC"))
    canvas.line(0.65 * inch, 0.55 * inch, 7.85 * inch, 0.55 * inch)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#627D98"))
    canvas.drawString(0.65 * inch, 0.34 * inch, "Exohunt evidence packet - not confirmation")
    canvas.drawRightString(7.85 * inch, 0.34 * inch, f"Page {document.page}")
    canvas.restoreState()


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)


def _link(label: str, url: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(f'<link href="{escape(url)}" color="#146C72">{escape(label)}</link>', style)


def _table(rows: list[list[object]], widths: list[float] | None = None) -> Table:
    styles = _styles()
    converted = [
        [cell if isinstance(cell, Paragraph) else _paragraph(cell, styles["Small"]) for cell in row]
        for row in rows
    ]
    table = Table(converted, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9EAF0")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#102A43")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BCCCDC")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFC")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _scaled_image(path: Path, max_width: float = 7.0 * inch, max_height: float = 4.5 * inch):
    image = Image(str(path))
    scale = min(max_width / image.imageWidth, max_height / image.imageHeight)
    image.drawWidth = image.imageWidth * scale
    image.drawHeight = image.imageHeight * scale
    return image


def _write_pdf(path: Path, story: list[object], title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.72 * inch,
        title=title,
        author="Exohunt",
    )
    document.build(story, onFirstPage=_page, onLaterPages=_page)


def _candidate_readiness(
    report: dict[str, Any],
    pixel: dict[str, Any] | None,
    sector_vet: dict[str, Any] | None,
    tce_check: dict[str, Any] | None,
) -> list[dict[str, str]]:
    signal = report["strongest_residual_signal"]
    triage_passes = bool(report.get("automated_triage", {}).get("passes"))
    required = all(
        (_float(signal.get(key)) or 0) > 0
        for key in ("period_days", "transit_time", "depth_ppm", "duration_hours")
    )
    return [
        {
            "check": "Automated residual triage",
            "status": "PASS" if triage_passes else "FAIL",
            "detail": "No automated rejection" if triage_passes else "; ".join(
                report.get("automated_triage", {}).get("rejection_reasons", [])
            ),
        },
        {
            "check": "Four required transit parameters",
            "status": "PASS" if required else "FAIL",
            "detail": "Period, BJD midpoint, depth, and duration are populated.",
        },
        {
            "check": "Pixel-level source localization",
            "status": (
                "PASS" if pixel and pixel.get("on_target_within_one_pixel") else "NEEDED"
            ),
            "detail": (
                f"Offset {pixel.get('centroid_offset_pixels'):.3f} pixels."
                if pixel and pixel.get("centroid_offset_pixels") is not None
                else "Run pixel-vet on at least one sector."
            ),
        },
        {
            "check": "Independent support in multiple TESS sectors",
            "status": (
                "PASS"
                if sector_vet and sector_vet.get("passes_distinct_sector_gate")
                else "NEEDED"
            ),
            "detail": (
                f"Supported in {sector_vet.get('supported_sector_count')} sectors."
                if sector_vet
                else "Run sector-vet; a joint long-baseline peak is not enough."
            ),
        },
        {
            "check": "Current ExoFOP community-candidate duplicate search",
            "status": "MANUAL",
            "detail": "Repeat immediately before sharing; the local catalog snapshot is insufficient.",
        },
        {
            "check": "Public MAST TCE duplicate search",
            "status": (
                "FAIL"
                if tce_check and tce_check.get("matching_tces")
                else "PASS"
                if tce_check and tce_check.get("catalogs_checked", 0) > 0
                else "NEEDED"
            ),
            "detail": (
                f"Found {len(tce_check.get('matching_tces', []))} matching TCE(s)."
                if tce_check
                else "Run tce-check against current single- and multi-sector tables."
            ),
        },
        {
            "check": "TESS TCE/DV and neighboring-source review",
            "status": "MANUAL",
            "detail": "Inspect SPOC DV products, ExoFOP files, Gaia neighbors, and SIMBAD.",
        },
        {
            "check": "Independent light-curve product",
            "status": "MANUAL",
            "detail": "Reproduce with QLP, TESS-SPOC, TGLC, or an independent extraction.",
        },
        {
            "check": "Parameter uncertainties / refined transit fit",
            "status": "NEEDED",
            "detail": "BLS screening values are not publication-quality parameter estimates.",
        },
        {
            "check": "Experienced exoplanet reviewer",
            "status": "NEEDED",
            "detail": "Obtain review before contacting follow-up programs or claiming a candidate.",
        },
    ]


def create_candidate_packet(
    report_path: str | Path,
    *,
    output_dir: str | Path = "output/candidate_packets",
    pdf_output_dir: str | Path = "output/pdf",
    pixel_report_path: str | Path | None = None,
    sector_vet_report_path: str | Path | None = None,
    tce_check_report_path: str | Path | None = None,
    submitter: str = "[fill before sharing]",
    contact_email: str = "[fill before sharing]",
    allow_rejected: bool = False,
) -> dict[str, str]:
    """Create a review packet from one residual-search JSON report."""

    source_path = Path(report_path)
    report = json.loads(source_path.read_text(encoding="utf-8"))
    if "strongest_residual_signal" not in report:
        raise ValueError("Source JSON is not a residual-search report.")
    passes = bool(report.get("automated_triage", {}).get("passes"))
    if not passes and not allow_rejected:
        reasons = report.get("automated_triage", {}).get("rejection_reasons", [])
        raise ValueError(
            "The signal failed automated triage; no candidate packet was created. "
            + "; ".join(reasons)
        )
    pixel = None
    pixel_path = Path(pixel_report_path) if pixel_report_path else None
    if pixel_path:
        pixel = json.loads(pixel_path.read_text(encoding="utf-8"))
    sector_vet = None
    sector_vet_path = Path(sector_vet_report_path) if sector_vet_report_path else None
    if sector_vet_path:
        sector_vet = json.loads(sector_vet_path.read_text(encoding="utf-8"))
    tce_check = None
    tce_check_path = Path(tce_check_report_path) if tce_check_report_path else None
    if tce_check_path:
        tce_check = json.loads(tce_check_path.read_text(encoding="utf-8"))
        if tce_check.get("matching_tces") and not allow_rejected:
            raise ValueError("The signal matches an existing public TCE; packet creation was blocked.")

    metadata = report.get("data", {})
    signal = report["strongest_residual_signal"]
    tic_id = metadata.get("tic_id") or "unknown"
    target = str(metadata.get("target", f"TIC {tic_id}"))
    stem = _safe_name(f"TIC_{tic_id}_candidate")
    bundle_dir = Path(output_dir) / stem
    bundle_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = bundle_dir / "candidate_packet.md"
    worksheet_path = bundle_dir / "exofop_parameter_worksheet.csv"
    checklist_path = bundle_dir / "submission_checklist.json"
    manifest_path = bundle_dir / "bundle_manifest.json"
    pdf_path = Path(pdf_output_dir) / f"{stem}_packet.pdf"

    period = float(signal["period_days"])
    epoch_btjd = float(signal["transit_time"])
    epoch_bjd = epoch_btjd + TESS_BJD_OFFSET
    depth = float(signal["depth_ppm"])
    snr = float(signal["depth_snr"])
    duration = float(signal["duration_hours"])
    depth_error = depth / snr if snr > 0 else None
    readiness = _candidate_readiness(report, pixel, sector_vet, tce_check)
    current_pause = (
        "As of 2026-07-22, ExoFOP has temporarily paused creation of new community "
        "planet candidates while it reviews its guidelines. Do not treat this packet "
        "as an accepted submission."
    )

    worksheet = {
        "tic_id": tic_id,
        "discovery_source": "TESS",
        "candidate_name": "leave blank / use next ExoFOP TIC sequence",
        "initial_disposition": "PC",
        "orbital_period_days": period,
        "orbital_period_error_days": "NEEDS_REFINED_FIT",
        "transit_midpoint_bjd_tdb": epoch_bjd,
        "transit_midpoint_error_days": "NEEDS_REFINED_FIT",
        "transit_depth_ppm": depth,
        "transit_depth_error_ppm_screening_only": depth_error,
        "transit_duration_hours": duration,
        "transit_duration_error_hours": "NEEDS_REFINED_FIT",
        "data_tag": "YYYYMMDD_username_description_number",
        "submitter": submitter,
        "contact_email": contact_email,
        "status": "DRAFT_NOT_READY_FOR_UPLOAD",
    }
    with worksheet_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(worksheet))
        writer.writeheader()
        writer.writerow(worksheet)
    checklist_path.write_text(
        json.dumps(
            {
                "generated_utc": _utc_now(),
                "exofop_new_candidate_upload_status": "temporarily paused as of 2026-07-22",
                "ready_for_upload": False,
                "checks": readiness,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    sectors = metadata.get("downloaded_sectors") or metadata.get("requested_sectors") or []
    known = report.get("known_signal_masks", [])
    lines = [
        f"# Candidate evidence packet: {target}",
        "",
        "> PRELIMINARY TRANSIT SIGNAL - NOT A CONFIRMED PLANET.",
        "",
        current_pause,
        "",
        "## Identity and provenance",
        "",
        f"- TIC ID: {tic_id}",
        f"- TESS sectors: {', '.join(str(value) for value in sectors)}",
        f"- Light-curve author: {metadata.get('author', 'unknown')}",
        f"- Cadence: {metadata.get('requested_cadence_seconds', 'unknown')} seconds",
        f"- Submitter: {submitter}",
        f"- Contact: {contact_email}",
        "",
        "## Screening parameters",
        "",
        f"- Period: {period:.9f} days",
        f"- Transit midpoint: {epoch_btjd:.8f} BTJD = {epoch_bjd:.8f} BJD_TDB (offset conversion)",
        f"- Duration: {duration:.4f} hours",
        f"- Depth: {depth:.2f} ppm",
        f"- White-noise BLS depth S/N: {snr:.2f}",
        f"- Approximate formal depth error: {depth_error:.2f} ppm (screening only)",
        f"- Observed transit events: {signal.get('observed_transits')}",
        f"- Radius ratio: {signal.get('radius_ratio')}",
        "",
        "The period, epoch, duration, and their uncertainties require a refined transit fit before submission.",
        "",
        "## Catalogued signals masked before the residual search",
        "",
    ]
    if known:
        for event in known:
            lines.append(
                f"- {event.get('label')}: {event.get('period_days')} d, "
                f"duration {event.get('duration_hours')} h ({event.get('source')})"
            )
    else:
        lines.append("- None recorded; investigate before sharing.")
    lines.extend(["", "## Readiness checklist", ""])
    for item in readiness:
        lines.append(f"- **{item['status']} - {item['check']}:** {item['detail']}")
    lines.extend(
        [
            "",
            "## Current reporting route",
            "",
            f"1. Recheck the live [ExoFOP community-candidate and TOI lists]({EXOFOP_HOME_URL}).",
            "2. Complete pixel, TCE/DV, Gaia-neighbor, independent-extraction, and uncertainty checks.",
            f"3. Ask an experienced reviewer through [Planet Hunters TESS]({PLANET_HUNTERS_URL}) or [TFOP]({TFOP_JOIN_URL}).",
            f"4. Because candidate creation is paused, verify current instructions at [ExoFOP Help]({EXOFOP_HELP_URL}) or contact {EXOFOP_SUPPORT}.",
            "5. If uploads reopen, use the live form/template rather than uploading this worksheet directly.",
            "",
            "## Evidence inventory",
            "",
            f"- Residual report: `{source_path}`",
            f"- Diagnostic plot: `{source_path.with_suffix('.png')}`",
            f"- Pixel report: `{pixel_path if pixel_path else 'not yet supplied'}`",
            f"- Sector-coherence report: `{sector_vet_path if sector_vet_path else 'not yet supplied'}`",
            f"- TCE duplicate report: `{tce_check_path if tce_check_path else 'not yet supplied'}`",
            f"- Parameter worksheet: `{worksheet_path.name}`",
            f"- Machine checklist: `{checklist_path.name}`",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    styles = _styles()
    story: list[object] = [
        Paragraph("Candidate Evidence Packet", styles["PacketTitle"]),
        Paragraph(escape(target), styles["Heading1"]),
        Paragraph(
            "PRELIMINARY TRANSIT SIGNAL - NOT A CONFIRMED PLANET AND NOT READY FOR UPLOAD",
            styles["Status"],
        ),
        _paragraph(current_pause, styles["BodyText"]),
        Paragraph("Signal summary", styles["Section"]),
        _table(
            [
                ["Field", "Value"],
                ["TIC / sectors", f"{tic_id} / {', '.join(str(v) for v in sectors)}"],
                ["Period", f"{period:.9f} days"],
                ["Midpoint", f"{epoch_bjd:.8f} BJD_TDB ({epoch_btjd:.8f} BTJD)"],
                ["Duration", f"{duration:.4f} hours"],
                ["Depth", f"{depth:.2f} ppm"],
                ["BLS depth S/N", f"{snr:.2f} (white-noise screening statistic)"],
                ["Observed events", signal.get("observed_transits")],
            ],
            [1.7 * inch, 5.2 * inch],
        ),
        Spacer(1, 0.12 * inch),
    ]
    plot_path = source_path.with_suffix(".png")
    if plot_path.exists():
        story.extend(
            [
                Paragraph("Residual-search diagnostic", styles["Section"]),
                _scaled_image(plot_path),
                _paragraph(
                    "The plotted signal remains an automated screening result. Red noise, "
                    "stellar activity, dilution, and eclipsing binaries can mimic transits.",
                    styles["Small"],
                ),
            ]
        )
    story.extend(
        [
            PageBreak(),
            Paragraph("Automated and manual readiness", styles["Section"]),
            _table(
                [["Check", "Status", "Detail"]]
                + [[item["check"], item["status"], item["detail"]] for item in readiness],
                [2.0 * inch, 0.75 * inch, 4.15 * inch],
            ),
            Paragraph("Known signals removed", styles["Section"]),
        ]
    )
    if known:
        story.append(
            _table(
                [["Label", "Period (d)", "Duration (h)", "Source"]]
                + [
                    [
                        event.get("label"),
                        event.get("period_days"),
                        event.get("duration_hours"),
                        event.get("source"),
                    ]
                    for event in known
                ],
                [1.25 * inch, 1.0 * inch, 1.0 * inch, 3.65 * inch],
            )
        )
    else:
        story.append(_paragraph("No masks were recorded; this blocks review.", styles["BodyText"]))
    if pixel:
        pixel_plot = Path(str(pixel.get("plot", "")))
        if pixel_plot.exists():
            story.extend(
                [
                    Paragraph("Pixel-level diagnostic", styles["Section"]),
                    _scaled_image(pixel_plot),
                ]
            )
    if sector_vet:
        sector_plot = Path(str(sector_vet.get("plot", "")))
        if sector_plot.exists():
            story.extend(
                [
                    Paragraph("Independent sector support", styles["Section"]),
                    _scaled_image(sector_plot),
                ]
            )
    story.extend(
        [
            PageBreak(),
            Paragraph("Reporting and collaboration route", styles["Section"]),
            _paragraph(
                "ExoFOP requires useful candidates to include measured parameters and supporting "
                "material, and it requires period, epoch, depth, and duration for cTOI consideration. "
                "Candidate creation is currently paused, so confirm live instructions before acting.",
                styles["BodyText"],
            ),
            Spacer(1, 0.12 * inch),
            _link("ExoFOP Help and community-candidate guidelines", EXOFOP_HELP_URL, styles["BodyText"]),
            _link("ExoFOP target and candidate search", EXOFOP_HOME_URL, styles["BodyText"]),
            _link("Join the TESS Follow-up Observing Program", TFOP_JOIN_URL, styles["BodyText"]),
            _link("Planet Hunters TESS", PLANET_HUNTERS_URL, styles["BodyText"]),
            Spacer(1, 0.16 * inch),
            _paragraph(f"ExoFOP support: {EXOFOP_SUPPORT}", styles["BodyText"]),
            Paragraph("Packet provenance", styles["Section"]),
            _table(
                [
                    ["Item", "Value"],
                    ["Generated UTC", _utc_now()],
                    ["Submitter", submitter],
                    ["Contact", contact_email],
                    ["Source report", str(source_path)],
                    ["Pixel report", str(pixel_path) if pixel_path else "not supplied"],
                    [
                        "Sector-vet report",
                        str(sector_vet_path) if sector_vet_path else "not supplied",
                    ],
                    ["TCE check", str(tce_check_path) if tce_check_path else "not supplied"],
                ],
                [1.4 * inch, 5.5 * inch],
            ),
        ]
    )
    _write_pdf(pdf_path, story, f"Candidate evidence packet - {target}")

    outputs = {
        "markdown": str(markdown_path),
        "pdf": str(pdf_path),
        "worksheet": str(worksheet_path),
        "checklist": str(checklist_path),
    }
    manifest_path.write_text(
        json.dumps(
            {
                "generated_utc": _utc_now(),
                "source_report": str(source_path),
                "pixel_report": str(pixel_path) if pixel_path else None,
                "sector_vet_report": str(sector_vet_path) if sector_vet_path else None,
                "tce_check_report": str(tce_check_path) if tce_check_path else None,
                "automated_triage_passed": passes,
                "generated_from_rejected_signal": not passes,
                "outputs": outputs,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    outputs["manifest"] = str(manifest_path)
    return outputs


def create_campaign_report(
    summary_path: str | Path,
    *,
    target_manifest_path: str | Path | None = None,
    output_dir: str | Path = "output/reports",
    pdf_output_dir: str | Path = "output/pdf",
) -> dict[str, str]:
    """Create a compact campaign report from ``batch_summary.json``."""

    source_path = Path(summary_path)
    summary = json.loads(source_path.read_text(encoding="utf-8"))
    results = list(summary.get("results", []))
    if not results:
        raise ValueError("Campaign summary contains no results.")
    target_manifest = None
    if target_manifest_path:
        target_manifest = json.loads(Path(target_manifest_path).read_text(encoding="utf-8"))
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(source_path.parent.name + "_campaign_report")
    markdown_path = output / f"{stem}.md"
    pdf_path = Path(pdf_output_dir) / f"{stem}.pdf"
    counts = summary.get("counts", {})

    lines = [
        f"# Exohunt campaign report: {source_path.parent.name}",
        "",
        "> Automated residual-transit survey. No row is a confirmed planet.",
        "",
        f"Generated UTC: {_utc_now()}",
        "",
        f"- Targets: {len(results)}",
        f"- Automated survivors: {counts.get('survivor', 0)}",
        f"- Rejected: {counts.get('rejected', 0)}",
        f"- Errors: {counts.get('error', 0)}",
        f"- Search period range: {summary.get('settings', {}).get('period_range_days')}",
        "",
        "## Results",
        "",
        "| TIC | Sectors | Status | Period (d) | Depth (ppm) | S/N | Reason |",
        "|---:|---|---|---:|---:|---:|---|",
    ]
    for row in results:
        lines.append(
            f"| {row.get('tic_id', '')} | {row.get('sectors', '')} | {row.get('status', '')} | "
            f"{row.get('period_days', '')} | {row.get('depth_ppm', '')} | "
            f"{row.get('depth_snr', '')} | {str(row.get('rejection_reasons', row.get('error', ''))).replace('|', '/')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "An automated survivor is only a request for deeper vetting. It must pass current "
            "catalog, TCE/DV, pixel, neighbor, independent-extraction, and expert-review checks.",
            "",
            f"Current community-candidate guidance: [ExoFOP Help]({EXOFOP_HELP_URL}).",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    styles = _styles()
    story: list[object] = [
        Paragraph("Exohunt Campaign Report", styles["PacketTitle"]),
        Paragraph(escape(source_path.parent.name), styles["Heading1"]),
        Paragraph(
            "AUTOMATED RESIDUAL-TRANSIT SURVEY - RESULTS ARE NOT PLANET CONFIRMATIONS",
            styles["Status"],
        ),
        _table(
            [
                ["Metric", "Value"],
                ["Targets", len(results)],
                ["Automated survivors", counts.get("survivor", 0)],
                ["Rejected", counts.get("rejected", 0)],
                ["Errors", counts.get("error", 0)],
                ["Period range", summary.get("settings", {}).get("period_range_days")],
                ["Cadence", f"{summary.get('settings', {}).get('cadence_seconds')} seconds"],
            ],
            [2.1 * inch, 4.8 * inch],
        ),
    ]
    if target_manifest:
        criteria = target_manifest.get("criteria", {})
        story.extend(
            [
                Paragraph("Prespecified target rule", styles["Section"]),
                _paragraph(
                    "Cool, bright, nearby hosts with one unique transiting ephemeris across "
                    "the NASA TOI and confirmed-planet tables and at least two public SPOC sectors.",
                    styles["BodyText"],
                ),
                _paragraph(f"Saved criteria: {json.dumps(criteria, sort_keys=True)}", styles["Small"]),
            ]
        )
    story.extend([Paragraph("Target results", styles["Section"])])
    result_rows: list[list[object]] = [["TIC / sectors", "Status", "P (d)", "Depth", "S/N", "Decision"]]
    for row in results:
        result_rows.append(
            [
                f"{row.get('tic_id')} / {row.get('sectors')}",
                row.get("status"),
                f"{float(row['period_days']):.5f}" if row.get("period_days") is not None else "-",
                f"{float(row['depth_ppm']):.1f}" if row.get("depth_ppm") is not None else "-",
                f"{float(row['depth_snr']):.2f}" if row.get("depth_snr") is not None else "-",
                row.get("rejection_reasons", row.get("error", "manual review")),
            ]
        )
    story.append(
        _table(
            result_rows,
            [1.25 * inch, 0.65 * inch, 0.62 * inch, 0.62 * inch, 0.48 * inch, 3.28 * inch],
        )
    )
    story.extend(
        [
            Paragraph("Interpretation and next gate", styles["Section"]),
            _paragraph(
                "Automated survivors, if any, require live duplicate checks against ExoFOP, "
                "TOIs and confirmed planets; SPOC TCE/DV review; pixel-level localization; "
                "neighbor and eclipsing-binary checks; an independent extraction; a refined "
                "transit fit with uncertainties; and experienced review.",
                styles["BodyText"],
            ),
            _paragraph(
                "As of 2026-07-22, ExoFOP has temporarily paused creation of new community "
                "planet candidates. Confirm current instructions before sharing a packet.",
                styles["BodyText"],
            ),
            _link("Live ExoFOP guidance", EXOFOP_HELP_URL, styles["BodyText"]),
        ]
    )
    _write_pdf(pdf_path, story, f"Exohunt campaign report - {source_path.parent.name}")
    return {"markdown": str(markdown_path), "pdf": str(pdf_path)}
