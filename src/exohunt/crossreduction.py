"""T7: does the signal survive being reduced by somebody else's pipeline?

MASTER_PLAN.md section 4.5. Promotion to ``science_vetted_lead`` requires four
things on the *fixed* ephemeris, and this module is the gate that checks them:

1. **Depth agreement across independent reductions.** Two pipelines processing
   the same pixels disagree about almost everything except a real astrophysical
   signal. One reduction showing a transit is a statement about that pipeline.
2. **Presence in the undetrended SAP fold.** This is the direct lesson of the
   project's own history: a detrender can manufacture a periodic dip, and every
   *detrended* product inherits it, so agreement among them proves nothing
   about the sky. An undetrended fold cannot be talked into it.
3. **Support in every sector where the signal should have been detectable.**
   The asymmetry matters: a sector where injection says completeness at this
   depth is poor cannot vote *against* the signal. Counting an uninformative
   non-detection as evidence of absence is how real signals get discarded.
4. **Stacked secondary and odd/even re-measured on the all-sector fold.** TIC
   181014443's secondary was 2.3 sigma in one sector and 5.9 sigma stacked;
   per-sector measurement is where that eclipsing binary hid.

And one precedence rule that outranks all four: **a common-mode kill stands.**
Sector coherence cannot clear a shared-ephemeris verdict, because an
observatory systematic repeats identically in every sector -- coherence is
exactly what it looks like.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from .adjudicate import _assert_automated_status
from .config import CrossReductionConfig, CURRENT_CROSS_REDUCTION

PROMOTED_STATUS = "science_vetted_lead"
WITHHELD_STATUS = "single_sector_unconfirmed"
COMMON_MODE_STATUS = "common_mode_systematic"


@dataclass(frozen=True, slots=True)
class ReductionDepth:
    """One pipeline's depth measurement on the fixed ephemeris."""

    product: str
    depth_ppm: float
    depth_error_ppm: float
    detrended: bool = True

    def significance(self) -> float | None:
        if self.depth_error_ppm is None or self.depth_error_ppm <= 0:
            return None
        return self.depth_ppm / self.depth_error_ppm


@dataclass(frozen=True, slots=True)
class SectorSupport:
    """Whether one sector saw the signal, and whether it could have."""

    sector: int
    detected: bool
    completeness: float | None = None


@dataclass(frozen=True, slots=True)
class StackedVetoes:
    """Secondary and odd/even, re-measured on the all-sector fold."""

    secondary_sigma: float | None = None
    odd_even_sigma: float | None = None
    sectors_stacked: int = 0


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promoted: bool
    status: str | None
    blocking: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def depth_agreement(
    measurements: Sequence[ReductionDepth],
    *,
    config: CrossReductionConfig | None = None,
) -> dict[str, Any]:
    """Do the independent reductions agree on how deep the transit is?

    Compared pairwise on the difference of two measurements, whose uncertainty
    is the quadrature sum of theirs. Comparing each against a combined mean
    would let one precise outlier drag the mean onto itself and then pass.
    """

    settings = config or CURRENT_CROSS_REDUCTION
    measured = [
        item
        for item in measurements
        if item.detrended and item.depth_error_ppm and item.depth_error_ppm > 0
    ]
    # Only reductions that actually detected something may vote. One with an
    # uncertainty wide enough to cover any depth agrees with everything, and
    # counting that as confirmation is how a cross-reduction test passes
    # without doing any work.
    usable = [
        item
        for item in measured
        if abs(item.significance() or 0.0) >= settings.minimum_reduction_significance
    ]
    uninformative = [
        {
            "product": item.product,
            "depth_ppm": item.depth_ppm,
            "depth_error_ppm": item.depth_error_ppm,
            "significance": item.significance(),
        }
        for item in measured
        if item not in usable
    ]
    products = sorted({item.product for item in usable})
    if len(products) < settings.minimum_independent_reductions:
        return {
            "products": products,
            "independent_reductions": len(products),
            "agrees": False,
            "uninformative_reductions": uninformative,
            "reason": (
                f"needs {settings.minimum_independent_reductions} independent "
                f"reductions detecting at "
                f"{settings.minimum_reduction_significance} sigma, has "
                f"{len(products)}"
            ),
        }

    worst_tension = 0.0
    disagreeing: list[dict[str, Any]] = []
    for index, first in enumerate(usable):
        for second in usable[index + 1 :]:
            if first.product == second.product:
                continue
            error = (first.depth_error_ppm**2 + second.depth_error_ppm**2) ** 0.5
            tension = abs(first.depth_ppm - second.depth_ppm) / error
            worst_tension = max(worst_tension, tension)
            if tension > settings.depth_agreement_sigma:
                disagreeing.append(
                    {
                        "products": [first.product, second.product],
                        "depths_ppm": [first.depth_ppm, second.depth_ppm],
                        "tension_sigma": tension,
                    }
                )
    return {
        "products": products,
        "independent_reductions": len(products),
        "worst_tension_sigma": worst_tension,
        "disagreeing_pairs": disagreeing,
        "agrees": not disagreeing,
        "tolerance_sigma": settings.depth_agreement_sigma,
        "uninformative_reductions": uninformative,
        "minimum_reduction_significance": settings.minimum_reduction_significance,
    }


def undetrended_support(
    measurements: Sequence[ReductionDepth],
    *,
    config: CrossReductionConfig | None = None,
) -> dict[str, Any]:
    """Is the signal there before anybody detrended anything?"""

    settings = config or CURRENT_CROSS_REDUCTION
    raw = [item for item in measurements if not item.detrended]
    if not raw:
        return {
            "measured": False,
            "present": False,
            "reason": "no undetrended measurement was supplied",
        }
    best = max(raw, key=lambda item: item.significance() or float("-inf"))
    significance = best.significance()
    return {
        "measured": True,
        "product": best.product,
        "depth_ppm": best.depth_ppm,
        "significance": significance,
        "minimum_sigma": settings.undetrended_minimum_sigma,
        "present": bool(
            significance is not None
            and significance >= settings.undetrended_minimum_sigma
        ),
    }


def sector_support(
    sectors: Sequence[SectorSupport],
    *,
    config: CrossReductionConfig | None = None,
) -> dict[str, Any]:
    """Which sectors support the signal, and which are entitled to object?

    A non-detection is only evidence of absence where the signal would have
    been detectable. Sectors below the completeness floor abstain, and are
    reported as abstaining rather than quietly dropped.
    """

    settings = config or CURRENT_CROSS_REDUCTION
    supporting: list[int] = []
    objecting: list[int] = []
    abstaining: list[dict[str, Any]] = []
    for entry in sectors:
        if entry.detected:
            supporting.append(entry.sector)
            continue
        completeness = entry.completeness
        if (
            completeness is None
            or completeness < settings.sector_veto_minimum_completeness
        ):
            abstaining.append(
                {
                    "sector": entry.sector,
                    "completeness": completeness,
                    "reason": (
                        "completeness at this depth is below the floor, so a "
                        "non-detection here carries no information"
                    ),
                }
            )
            continue
        objecting.append(entry.sector)
    return {
        "supporting_sectors": sorted(supporting),
        "objecting_sectors": sorted(objecting),
        "abstaining_sectors": abstaining,
        "completeness_floor": settings.sector_veto_minimum_completeness,
        "supported": bool(supporting) and not objecting,
    }


def stacked_vetoes(
    vetoes: StackedVetoes | None,
    *,
    config: CrossReductionConfig | None = None,
) -> dict[str, Any]:
    """Secondary and odd/even on the all-sector fold, not per sector."""

    settings = config or CURRENT_CROSS_REDUCTION
    if vetoes is None:
        return {
            "measured": False,
            "passes": False,
            "reason": "stacked secondary and odd/even were not re-measured",
        }
    failures: list[str] = []
    if (
        vetoes.secondary_sigma is not None
        and vetoes.secondary_sigma >= settings.stacked_secondary_kill_sigma
    ):
        failures.append("stacked secondary eclipse")
    if (
        vetoes.odd_even_sigma is not None
        and vetoes.odd_even_sigma >= settings.stacked_odd_even_kill_sigma
    ):
        failures.append("stacked odd/even depth difference")
    return {
        "measured": True,
        "sectors_stacked": vetoes.sectors_stacked,
        "secondary_sigma": vetoes.secondary_sigma,
        "odd_even_sigma": vetoes.odd_even_sigma,
        "failures": failures,
        "passes": not failures,
    }


def evaluate(
    *,
    measurements: Iterable[ReductionDepth],
    sectors: Iterable[SectorSupport],
    vetoes: StackedVetoes | None = None,
    common_mode_verdict: str | None = None,
    config: CrossReductionConfig | None = None,
) -> PromotionDecision:
    """Decide whether the fixed ephemeris may be promoted."""

    settings = config or CURRENT_CROSS_REDUCTION
    measurements = list(measurements)
    sectors = list(sectors)

    # Precedence, before anything else is computed. HANDOFF section 7,
    # preserved: an observatory systematic repeats identically in every
    # sector, so multi-sector coherence is exactly what it looks like and
    # cannot be allowed to argue its way out.
    if common_mode_verdict in {COMMON_MODE_STATUS, "localized_coincidence"}:
        return PromotionDecision(
            promoted=False,
            status=common_mode_verdict,
            blocking=[
                "a shared-ephemeris population verdict outranks every "
                "cross-reduction result"
            ],
            checks={"common_mode_verdict": common_mode_verdict},
        )

    depth = depth_agreement(measurements, config=settings)
    raw = undetrended_support(measurements, config=settings)
    support = sector_support(sectors, config=settings)
    stacked = stacked_vetoes(vetoes, config=settings)

    blocking: list[str] = []
    if not depth["agrees"]:
        blocking.append(
            depth.get("reason")
            or (
                "independent reductions disagree on depth beyond "
                f"{settings.depth_agreement_sigma} sigma"
            )
        )
    if settings.require_undetrended_detection and not raw["present"]:
        blocking.append(
            raw.get("reason")
            or "the signal is not present in the undetrended SAP fold"
        )
    if not support["supported"]:
        blocking.append(
            "sectors that could have detected this signal did not: "
            f"{support['objecting_sectors']}"
            if support["objecting_sectors"]
            else "no sector supports the signal"
        )
    if not stacked["passes"]:
        blocking.append(
            stacked.get("reason") or f"stacked vetoes failed: {stacked['failures']}"
        )

    status = PROMOTED_STATUS if not blocking else WITHHELD_STATUS
    _assert_automated_status(status)
    return PromotionDecision(
        promoted=not blocking,
        status=status,
        blocking=blocking,
        checks={
            "depth_agreement": depth,
            "undetrended": raw,
            "sector_support": support,
            "stacked_vetoes": stacked,
            "policy_version": settings.policy_version,
        },
    )
