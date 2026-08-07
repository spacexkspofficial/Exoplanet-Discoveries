"""Assemble the review packet -- and refuse to, when a section is missing.

MASTER_PLAN.md section 4.7. ``packet_ready_for_review`` is the strongest state
this system may assign on its own, and the plan enumerates exactly what it has
to contain: ephemeris and fit posteriors, every T3 gate value, the population
screen, the catalog adjudication *with the snapshot hashes it was decided
against*, pixel localization, the multi-reduction depth table, completeness at
this star's period and depth, the false-positive probability, and the full
provenance chain. That list is an ExoFOP-grade CTOI submission package.

Two design rules follow from that, and both are about refusal:

* **A missing section is not an empty section.** Assembling a packet with no
  pixel localization and calling it ready would be indistinguishable, to a
  reader, from a packet whose localization passed. Sections are required by
  name, and a packet missing any of them is returned as ``incomplete`` with
  the specific names listed -- never quietly downgraded, never padded.
* **The claim ceilings are structural.** There is no code path here that emits
  "vetted candidate", "validated planet", or "confirmed": those require a
  human decision, ground-based imaging, or external mass evidence that this
  pipeline cannot produce. The registry stages them as ``human_outcome`` and
  :func:`exohunt.adjudicate._assert_automated_status` refuses them at the
  boundary; the test suite asserts no automated writer can reach one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .adjudicate import _assert_automated_status

PACKET_SCHEMA_VERSION = 1
PACKET_STATUS = "packet_ready_for_review"

# Every section section 4.7 names. A packet is not "ready" without all of
# them; the names are the contract, so a renamed section fails loudly instead
# of silently going missing.
REQUIRED_SECTIONS = (
    "ephemeris",
    "fit_posteriors",
    "t3_gates",
    "population_screen",
    "catalog_adjudication",
    "pixel_localization",
    "multi_reduction_depths",
    "completeness",
    "false_positive_probability",
    "provenance",
)

# Statuses this module is structurally incapable of producing. Listed so the
# ceiling is visible in code rather than only in prose.
FORBIDDEN_CLAIMS = ("vetted_candidate", "confirmed_planet", "rediscovery")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True, slots=True)
class Packet:
    tic_id: int
    status: str | None
    ready: bool
    missing_sections: list[str] = field(default_factory=list)
    unmeasured_sections: list[str] = field(default_factory=list)
    sections: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    assembled_at_utc: str = ""
    schema_version: int = PACKET_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_measured(section: Any) -> bool:
    """Distinguish "we looked and here is the answer" from "we did not look".

    A section may legitimately record a negative or an inapplicable result --
    a southern target has no ZTF coverage, an FPP may not have been run -- but
    it must say so explicitly. ``None``, ``{}`` and a bare ``not_run`` are all
    absence wearing the clothes of a result.
    """

    if section is None:
        return False
    if isinstance(section, dict):
        if not section:
            return False
        state = section.get("state") or section.get("status") or section.get("verdict")
        if state in {"not_run", "not_evaluable", "missing", "unmeasured"}:
            return False
        return True
    if isinstance(section, (list, tuple)):
        return len(section) > 0
    return True


def assemble(
    tic_id: int,
    sections: dict[str, Any],
    *,
    required: tuple[str, ...] = REQUIRED_SECTIONS,
) -> Packet:
    """Build a packet, or explain precisely why it is not one.

    ``sections`` is a mapping of the section names above to whatever the
    corresponding stage produced. Nothing here recomputes science; the packet
    is an assembly of evidence other stages already measured and signed.
    """

    missing = [name for name in required if name not in sections]
    unmeasured = [
        name
        for name in required
        if name in sections and not _is_measured(sections[name])
    ]
    ready = not missing and not unmeasured

    body = json.dumps(
        {"tic_id": int(tic_id), "sections": sections},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    status = PACKET_STATUS if ready else None
    if status is not None:
        # Structural, not decorative: the only status this module can emit is
        # checked against the registry's stage rules on the way out.
        _assert_automated_status(status)
    return Packet(
        tic_id=int(tic_id),
        status=status,
        ready=ready,
        missing_sections=missing,
        unmeasured_sections=unmeasured,
        sections=sections,
        content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        assembled_at_utc=_utc_now(),
    )


def provenance_block(
    *,
    scientific_signature: str | None,
    vetting_signature: str | None,
    snapshot_hashes: dict[str, str],
    product_versions: dict[str, str],
    code_version: str,
) -> dict[str, Any]:
    """The chain that makes every number in the packet re-derivable.

    A packet without this is a set of assertions; with it, a reader can say
    which code, which configuration, and which catalog generation produced
    each one -- and re-running against a newer snapshot produces a *different*
    packet rather than silently changing this one's meaning.
    """

    return {
        "scientific_signature": scientific_signature,
        "vetting_signature": vetting_signature,
        "snapshot_hashes": dict(sorted(snapshot_hashes.items())),
        "product_versions": dict(sorted(product_versions.items())),
        "code_version": code_version,
        "state": "measured" if scientific_signature and vetting_signature else "not_run",
    }


def claim_ceiling_note() -> str:
    """The sentence that must travel with every packet."""

    return (
        "This packet is the strongest state the system assigns autonomously. "
        "It is not a vetted candidate, not a validated planet, and not a "
        "confirmed planet. A vetted candidate requires a human decision; a "
        "validated planet additionally requires ground-based imaging that "
        "excludes nearby false-positive scenarios and published-quality "
        "stellar characterization; confirmation requires external mass or "
        "dynamical evidence. This pipeline cannot produce any of those and "
        "has no code path that claims them."
    )


def summarize(packets: list[Packet]) -> dict[str, Any]:
    """Counts for the review queue, with the reasons packets fell short."""

    blocked: dict[str, int] = {}
    for packet in packets:
        for name in packet.missing_sections:
            blocked[f"missing:{name}"] = blocked.get(f"missing:{name}", 0) + 1
        for name in packet.unmeasured_sections:
            blocked[f"unmeasured:{name}"] = blocked.get(f"unmeasured:{name}", 0) + 1
    ready = [packet for packet in packets if packet.ready]
    return {
        "packets": len(packets),
        "ready": len(ready),
        "incomplete": len(packets) - len(ready),
        "blocking_sections": dict(sorted(blocked.items())),
        "required_sections": list(REQUIRED_SECTIONS),
        "claim_ceiling": claim_ceiling_note(),
    }
