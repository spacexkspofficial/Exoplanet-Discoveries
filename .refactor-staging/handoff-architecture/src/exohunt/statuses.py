"""Canonical EXOHUNT classification vocabulary and evidence ordering.

The JSON registry beside this module is the only hand-maintained definition of
a classification.  Python consumes it directly and the dashboard's TypeScript
tables are generated from it by :mod:`exohunt.status_codegen`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum
from importlib.resources import files
from typing import Final, Iterable


class EvidenceStage(IntEnum):
    """Authority of the evidence that produced a classification."""

    IN_LIGHT_CURVE = 0
    CATALOG_CONTEXT = 1
    MEASURED_SCIENCE = 2
    POPULATION_SCREEN = 3
    HUMAN_OUTCOME = 4


@dataclass(frozen=True, slots=True)
class StatusDefinition:
    slug: str
    label: str
    short_label: str
    help: str
    symbol: str
    color: str
    css_class: str
    evidence_stage: EvidenceStage
    precedence: int

    @property
    def rank(self) -> tuple[int, int]:
        return int(self.evidence_stage), self.precedence


def _load_registry() -> tuple[StatusDefinition, ...]:
    payload = json.loads(
        files("exohunt")
        .joinpath("status_registry.json")
        .read_text(encoding="utf-8")
    )
    definitions = tuple(
        StatusDefinition(
            slug=str(row["slug"]),
            label=str(row["label"]),
            short_label=str(row["short_label"]),
            help=str(row["help"]),
            symbol=str(row["symbol"]),
            color=str(row["color"]),
            css_class=str(row["css_class"]),
            evidence_stage=EvidenceStage[str(row["evidence_stage"]).upper()],
            precedence=int(row["precedence"]),
        )
        for row in payload
    )
    slugs = [definition.slug for definition in definitions]
    if len(slugs) != len(set(slugs)):
        raise ValueError("status_registry.json contains duplicate slugs")
    return definitions


STATUS_DEFINITIONS: Final = _load_registry()
STATUS_REGISTRY: Final = {
    definition.slug: definition for definition in STATUS_DEFINITIONS
}


def export_label(status: str) -> str:
    """Return the legacy sentence-style label written to survey JSON.

    The browser historically used title case and typographic separators while
    the exporter used sentence case and ASCII separators.  This deterministic
    projection preserves both observable formats without defining a second
    label table.
    """

    label = STATUS_REGISTRY[status].label.replace(" — ", " - ")
    label = label.replace(" / ", "/")
    return label[:1] + label[1:].lower()


def labels_for_stage(stage: EvidenceStage) -> dict[str, str]:
    return {
        definition.slug: export_label(definition.slug)
        for definition in STATUS_DEFINITIONS
        if definition.evidence_stage is stage
    }


def resolve_status(statuses: Iterable[str | None]) -> str:
    """Resolve ordered evidence using stage, then within-stage precedence.

    Unknown or absent classifications are ignored.  When rank is equal, later
    evidence wins, preserving the legacy overlay behavior.
    """

    resolved: StatusDefinition | None = None
    for status in statuses:
        if status is None:
            continue
        candidate = STATUS_REGISTRY.get(str(status))
        if candidate is None:
            continue
        if resolved is None or candidate.rank >= resolved.rank:
            resolved = candidate
    if resolved is None:
        raise ValueError("at least one known status is required")
    return resolved.slug


SCREENING_LABELS: Final = labels_for_stage(EvidenceStage.IN_LIGHT_CURVE)
CONTEXT_LABELS: Final = labels_for_stage(EvidenceStage.CATALOG_CONTEXT)
SCIENCE_LABELS: Final = labels_for_stage(EvidenceStage.MEASURED_SCIENCE)
COMMON_MODE_LABELS: Final = labels_for_stage(EvidenceStage.POPULATION_SCREEN)
HUMAN_OUTCOME_LABELS: Final = labels_for_stage(EvidenceStage.HUMAN_OUTCOME)
