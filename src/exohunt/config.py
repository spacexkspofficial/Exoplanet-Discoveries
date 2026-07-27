"""The single home of every named science threshold, and the signature over it.

Two rules, enforced by tests:

* No science-touching module introduces a bare numeric threshold; it names a
  field here, where the value carries its rationale. (``cli.py``'s historical
  literals migrate here as the kernel rewiring lands; new modules start
  compliant.)
* Every evidence record and summary is stamped with a scientific signature --
  a digest over code version, this configuration, the data-product family,
  and the target list -- and nothing aggregates across signatures. This is
  the rule that would have prevented the sector100_spoc mixed-version
  confusion, adopted from the research review after verification.

All values marked "initial" are provisional design targets from
MASTER_PLAN.md Appendix A; Phase 3 replaces them with values calibrated
against injections and inverted-data nulls. They live here so the calibration
lands as one reviewed, signature-bumping edit.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Detection-stage settings (T2)."""

    # Periodic claims need three transits in multi-sector data; two-transit
    # single-sector signals route to `needs_additional_sector` instead of
    # surviving. Two-transit "periods" are the alias factory (TOI-700 c was
    # recovered at exactly half its true period from one sector).
    min_transits_multisector: int = 3
    min_transits_single_sector: int = 2
    # TLS signal-detection-efficiency floors. White-noise FAP ~1% sits near
    # SDE 7 in the TLS literature; TESS red noise pushes the practical floor
    # higher, and single-sector data is alias-richer than stitched data.
    # Initial values; calibrated to <=0.1% inverted-data survivors in P3.
    sde_min_multisector: float = 8.0
    sde_min_single_sector: float = 9.0
    # Continuity with the historical gate, but applied to the red-noise
    # adjusted statistic that `signal_vetting_diagnostics` already measures,
    # not the optimistic white-noise BLS ratio.
    red_noise_snr_min: float = 7.1
    # Period grid: floor at half a day; ceiling from baseline/min_transits,
    # never a bare number. The grid extends past the *reporting* ceiling so
    # rails cannot accumulate at the reporting boundary; fits in the overscan
    # region are diagnostics, never survivors.
    min_period_days: float = 0.5
    period_overscan_fraction: float = 0.08
    # Duration grid from stellar density: [0.3, 1.5] x the b=0 expected
    # duration, clamped to physical bounds. No star searches durations it
    # cannot produce; the 6-hour grid rail disappears as a class.
    duration_grid_span: tuple[float, float] = (0.3, 1.5)
    duration_grid_points: int = 8
    duration_min_hours: float = 0.5
    duration_max_hours: float = 12.0
    # BLS pre-threshold above which TLS runs outside the faint-M lane.
    bls_sde_tls_trigger: float = 6.0
    # Alias ladder evaluated for every reported ephemeris.
    alias_ratios: tuple[float, ...] = (
        1.0 / 3.0,
        0.5,
        2.0 / 3.0,
        1.0,
        1.5,
        2.0,
        3.0,
    )
    # An alias only replaces the reported period when it beats it by this
    # factor: the half-period fold of a true signal gains a sqrt(2) cadence
    # bonus in stacked S/N, so near-ties must not flip the ephemeris.
    alias_change_margin: float = 1.1
    # A predicted event window counts as showing the signal only when its
    # depth clears this many standard errors; sign alone lets empty windows
    # vote "present" half the time by chance (measured in test_search).
    alias_event_sigma: float = 3.0


@dataclass(frozen=True, slots=True)
class VetoConfig:
    """Cheap physical veto settings (T3)."""

    # Model-fit odd/even difference; 3 sigma continues the historical gate but
    # the estimator works from a two-depth folded fit rather than medians of
    # per-event medians, so it no longer returns None at 3+1 events.
    odd_even_kill_sigma: float = 3.0
    # Secondary eclipses are scanned over the full out-of-transit phase, not
    # only phase 0.5 (eccentric binaries put secondaries elsewhere), and the
    # kill is applied on the all-sector stacked fold before any promotion:
    # TIC 181014443's secondary was 2.3 sigma in one sector, 5.9 stacked.
    secondary_kill_sigma: float = 3.0
    secondary_exclusion_durations: float = 1.5
    # Fitted duration versus the b=0 expectation from stellar density.
    # Inside [0.4, 1.5] passes; outside [0.25, 2.5] kills; between flags.
    # Catches giants, blends, and junk fits with no pixel download.
    duration_density_flag_span: tuple[float, float] = (0.4, 1.5)
    duration_density_kill_span: tuple[float, float] = (0.25, 2.5)
    # Depth implying a companion above 2 R_Jup routes to the EB lane; that is
    # astrophysics, not a tuning choice.
    max_companion_radius_rjup: float = 2.0
    # Continuity with historical gates.
    duty_cycle_max: float = 0.15
    depth_max_fraction: float = 0.05
    # Every counted transit event needs this many in-transit cadences and a
    # two-sided local baseline; events failing support do not count toward
    # min_transits.
    min_cadences_per_event: int = 3
    # An event whose centre falls inside a registered systematic window (see
    # PopulationConfig) is vetoed individually.
    dip_registry_veto: bool = True


@dataclass(frozen=True, slots=True)
class DetrendConfig:
    """Light-curve preparation settings (T1)."""

    # Biweight windows, in multiples of the longest searched duration: wotan
    # practice keeps depth erosion small at window >= 3x duration. Two
    # prepared fluxes: a short-window flux for the periodic search and a
    # long-window flux for the monotransit detector.
    short_window_days: float = 1.0
    long_window_days: float = 3.0
    # Escalation window for active stars (measured rotation below ~3 d or
    # variability amplitude above ~1%): a wide window cannot follow the
    # variability curvature at extrema and biases depths both ways. Measured
    # on synthetic 2%-amplitude rotators: window 0.4 d with the found signal
    # masked recovers injected depth to ~10%; window 1.0 d misses by ~50%.
    active_window_days: float = 0.4
    # A gap at least this long starts an independently detrended segment.
    # Measured on Sector 100 photometry (see detrending.py history): 0.10 d
    # makes the 3.8 h and 5.2 h interruptions real boundaries.
    segment_gap_days: float = 0.10
    # Support-weighted edges replace the hard half-window guard. f is the
    # populated fraction of a cadence's trend window; below the floor the
    # cadence is dropped, above it the cadence's uncertainty is inflated by
    # (1/f)**alpha so edges are visible to the search but cannot drive it.
    # Initial alpha; calibrated on the artifact regression set (target:
    # >=85% retention with the BTJD 4074.4/4080.8 epochs still dead).
    edge_support_floor: float = 0.4
    edge_weight_alpha: float = 1.0
    # Outlier clipping mirrors the existing pipeline.
    outlier_sigma_upper: float = 4.0
    outlier_sigma_lower: float = 20.0


@dataclass(frozen=True, slots=True)
class PopulationConfig:
    """Population-screen settings (T4)."""

    # Shared-ephemeris screen: measured separation on real campaigns was
    # enrichment 12.3x for the suspect population versus 2.1x for the rest;
    # these floors keep a small campaign from flagging a chance pair.
    common_mode_min_enrichment: float = 10.0
    common_mode_min_shared_targets: int = 10
    common_mode_min_shared_fraction: float = 0.005
    observatory_spread_deg: float = 1.0
    # Absolute-time dip registry: a time bin where this fraction of searched
    # stars dip together (at this per-star significance) is a systematic
    # window, vetoed per event before it can alias into a period. Measured
    # during implementation: at sigma 2 a 30-minute bin holds ~3 cadences of
    # 10-minute data and pure noise trips ~5% of star-bins, so the original
    # 5% cohort floor registered noise. Sigma 3 puts the per-star trip rate
    # near 0.5% and the 10% floor an order of magnitude above it.
    dip_bin_minutes: float = 30.0
    dip_star_sigma: float = 3.0
    dip_min_fraction: float = 0.10
    dip_min_stars: int = 20


@dataclass(frozen=True, slots=True)
class InstrumentConfig:
    """Facts about TESS, named so no module hard-codes them."""

    spacecraft_orbit_days: float = 13.70
    pixel_scale_arcsec: float = 21.0


@dataclass(frozen=True, slots=True)
class ScienceConfig:
    """Everything that defines a result's scientific identity, in one object."""

    search: SearchConfig = field(default_factory=SearchConfig)
    vetoes: VetoConfig = field(default_factory=VetoConfig)
    detrend: DetrendConfig = field(default_factory=DetrendConfig)
    population: PopulationConfig = field(default_factory=PopulationConfig)
    instrument: InstrumentConfig = field(default_factory=InstrumentConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    def config_hash(self) -> str:
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


CURRENT_CONFIG = ScienceConfig()


def code_version(repo_root: str | Path | None = None) -> str:
    """Identify the running code: git commit when available, else package."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root) if repo_root else str(Path(__file__).parent),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return f"git:{result.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    return "package:exohunt-starter-0.1.0"


def scientific_signature(
    *,
    code: str,
    config: ScienceConfig,
    product_family: str,
    target_list_hash: str,
) -> str:
    """Digest everything that defines what an evidence record means.

    Summaries, dashboards, and completeness surfaces group by this value and
    never aggregate across it.
    """

    canonical = json.dumps(
        {
            "code": code,
            "config": config.to_dict(),
            "product_family": product_family,
            "target_list": target_list_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sig1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def hash_target_list(path: str | Path) -> str:
    """Content hash of a target list file, for signature inputs."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
