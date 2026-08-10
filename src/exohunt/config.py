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

    # Bump when search semantics change without changing a numeric threshold;
    # checkpoint reuse compares this complete config.
    policy_version: str = "bls-screen-tls-red-noise-decider-v5-alias-ladder"
    # Periodic claims need three transits in multi-sector data; two-transit
    # single-sector signals route to `needs_additional_sector` instead of
    # surviving. Two-transit "periods" are the alias factory (TOI-700 c was
    # recovered at exactly half its true period from one sector).
    min_transits_multisector: int = 3
    min_transits_single_sector: int = 2
    # TLS signal-detection-efficiency floors. The single-sector floor is the
    # locked P3 Sector 100 calibration: 11.5 is above the largest inverted
    # null (11.245) and below the retained baseline signal (12.621), producing
    # 0/500 inverted and 0/500 scrambled survivors with a 1/500 baseline pass
    # rate. The multi-sector value remains provisional until a locked stitched
    # cohort measures it independently.
    sde_min_multisector: float = 8.0
    sde_min_single_sector: float = 11.5
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
    # duration, clamped to physical bounds. The fixed 6-hour pile-up is
    # replaced by star-specific endpoints; measured endpoint fits remain
    # common and are rejected explicitly.
    duration_grid_span: tuple[float, float] = (0.3, 1.5)
    duration_grid_points: int = 8
    duration_min_hours: float = 0.5
    duration_max_hours: float = 12.0
    # BLS pre-threshold above which TLS runs outside the faint-M lane.
    bls_sde_tls_trigger: float = 6.0
    # TLS may choose the same ephemeris or one member of the configured BLS
    # alias ladder. Wider disagreement means the two detectors did not confirm
    # the same signal and cannot promote it automatically.
    tls_bls_period_tolerance_fraction: float = 0.05
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
    # Correction 64: 14 of 53 lost known planets had the true period among the
    # five recorded BLS peaks and simply were not selected -- `pi Men c` at
    # rank 4 and `WASP-169 b` at rank 3, both at relative power 0.9999999.
    # At that separation the BLS statistic has saturated and `argmax` is
    # choosing on grid order and floating-point noise, not on evidence. When
    # independent peaks agree in power to within this fraction, the tie is
    # broken on how consistently the folded per-epoch depths agree instead.
    # Set to 0.0 to restore bare `argmax`.
    near_tie_relative_power: float = 1e-3
    # Only this many independent near-tied peaks are re-examined. The cost is
    # one fold per candidate; the cap keeps a flat periodogram from turning
    # into a linear scan.
    near_tie_max_candidates: int = 5
    # Peaks closer than this in fractional period are the same peak sampled
    # twice, not competing hypotheses.
    near_tie_separation_fraction: float = 0.02
    # Owner decision 2b, REVERTED 2026-08-10 on the measurement it asked for.
    # The veto was switched on so decision 5B's re-calibration could price it,
    # and the price came back bad: in the paired row-level diff of
    # results/p5/calibration_ncvz_1000{,_v2_neartie} it was the sole rejection
    # reason for 4 injected planets and for only 1 inverted and 1 scrambled
    # false alarm -- it cost 4 recoveries to buy 2. Promotion completeness fell
    # 0.08090 -> 0.07865, exactly the 4/1780 those planets represent, and none
    # of the four failing release gates moved. Correction 74.
    #
    # Left in place rather than deleted: the flag, its tests, and correction
    # 72's 1.60x enrichment are the record of why it was tried, and the trade
    # may read differently on a cohort that is not 1,000 NCVZ M dwarfs. Set
    # True to re-enable, and re-calibrate before trusting the result.
    veto_spacecraft_harmonic: bool = False
    # Walk the alias ladder on every reported ephemeris. `adjudicate_alias`
    # was implemented and tested at P2 and never called from any production
    # path, which left 31 of 341 known planets recovered at exactly one third
    # of their true period and none of them scored as recovered -- 45% of all
    # recovery failures, against machinery that already existed.
    adjudicate_alias_ladder: bool = True
    # An adjudicated period is adopted only if an evaluated grid point sits
    # this close to it. Outside that, there is no measured BLS solution to
    # adopt and the disagreement is recorded instead of guessed at.
    alias_snap_tolerance: float = 0.01


@dataclass(frozen=True, slots=True)
class VetoConfig:
    """Cheap physical veto settings (T3)."""

    # Bump when veto semantics change without changing a numeric threshold;
    # checkpoint reuse compares this complete config.
    # Bumped for the absolute-time dip veto: reports now carry a
    # `dip_window` block and per-star `population_bins`, and an event inside
    # a registered window stops counting toward the minimum-transit rule.
    # Reuse must not mix pre-dip and post-dip reports in one campaign.
    policy_version: str = "t3-dip-window-veto-v1"
    # Model-fit odd/even difference; 3 sigma continues the historical gate but
    # the estimator works from a two-depth folded fit rather than medians of
    # per-event medians, so it no longer returns None at 3+1 events.
    odd_even_kill_sigma: float = 3.0
    # Secondary eclipses are scanned over the full out-of-transit phase, not
    # only phase 0.5 (eccentric binaries put secondaries elsewhere). This is
    # the global threshold after a family-wise phase-window correction, not a
    # raw local maximum: the latter killed 30.8% of 500 pure-noise folds at
    # 3 sigma. The kill is applied on the all-sector stacked fold before any
    # promotion: TIC 181014443's secondary was 2.3 sigma in one sector, 5.9
    # stacked.
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
    #
    # NOT YET CALIBRATED, and the obvious calibration was measured and
    # rejected. See PROGRESS.md correction 10 and
    # results/p2_gates/ for the evidence. Summary: at window 1.0 d a clean
    # segment edge has f = 0.5, above this 0.4 floor, so the floor never fires
    # and the guard is effectively off. Raising the floor to 0.8 suppresses the
    # artifact but leaves an edge transit only 2 of the 5 cadences it needs.
    # Raising alpha to 5 keeps every cadence and looked like it worked on a
    # fast probe harness (enrichment 1.12, p = 0.14), but through the real
    # batch-hunt path on 371 targets it did not move enrichment at all
    # (1.137 -> 1.140) and raised artifact-epoch survivors from 1 to 9.
    # The owner's next choice -- a quarter-window hard guard plus a requirement
    # for two events with two-sided local baselines -- was also measured and
    # reverted. It retained 83.584% (below the 85% gate), left enrichment at
    # 1.142 (p=0.048), and produced 3 artifact-epoch survivors versus 1 under
    # the production guard. Local sample support cannot detect trend bias.
    # These values therefore stay at their provisional defaults until a
    # mechanism exists that separates edge sensitivity from edge artifacts.
    edge_support_floor: float = 0.4
    edge_weight_alpha: float = 1.0
    # Outlier clipping mirrors the existing pipeline.
    outlier_sigma_upper: float = 4.0
    outlier_sigma_lower: float = 20.0


@dataclass(frozen=True, slots=True)
class CatalogMaskConfig:
    """Rules for deciding whether a catalogued ephemeris is safe to mask."""

    # Period and epoch errors accumulate across the cycles separating a
    # catalog measurement from the searched light curve. A mask is permitted
    # only while that propagated phase error is no larger than one transit
    # duration; beyond that point the catalog prediction cannot demonstrate
    # which cadences contain the known transit. The mask half-width is widened
    # by the propagated error when the event remains maskable.
    max_phase_uncertainty_durations: float = 1.0
    uncertainty_propagation: str = "linear_worst_case"


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
class CalibrationConfig:
    """P3 sampling design and release-gate budgets."""

    policy_version: str = "p3-paired-depth-transfer-v2"
    random_sample_fraction: float = 0.05
    archetype_count: int = 50
    random_phase_injections_per_star: int = 20
    edge_injections_per_star: int = 20
    period_grid_points: int = 5
    depth_noise_multipliers: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)
    impact_parameters: tuple[float, ...] = (0.0, 0.5, 0.8)
    photon_noise_hours: float = 3.0
    inverted_survivor_budget: float = 0.001
    scrambled_survivor_budget: float = 0.001
    t3_pass_rate_min: float = 0.002
    t3_pass_rate_max: float = 0.02
    maximum_epoch_enrichment: float = 2.0
    maximum_median_depth_bias_fraction: float = 0.05
    maximum_edge_recovery_gap: float = 0.03
    known_period_tolerance_fraction: float = 0.01
    # Cross-source catalog depths versus this pipeline's detrended SPOC depth
    # differ by up to 48.3% on the frozen 20-planet set while periods remain
    # exact. A 50% scale check catches order-of-magnitude regressions without
    # pretending catalog passbands/apertures are identical reductions.
    known_depth_tolerance_fraction: float = 0.50
    epoch_alignment_minimum_tolerance_days: float = 0.02


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    """Identity-graph and catalog cross-match settings (T5, MASTER_PLAN 4.1).

    Deliberately **not** a member of :class:`ScienceConfig`. The detection
    identity that P3 certified is a digest over ``ScienceConfig``; folding
    vetting parameters into it would silently invalidate the stored trusted
    release every time a catalog rule was tuned, and would equally let a
    detection change masquerade as a vetting change. Vetting evidence carries
    :func:`vetting_signature` instead, which also names the snapshot
    generation it adjudicated against.
    """

    policy_version: str = "identity-graph-pm-aware-v1"
    # Counterpart search radius, expressed in TESS pixels rather than arcsec so
    # the instrument fact lives once (InstrumentConfig.pixel_scale_arcsec).
    # Anything inside one pixel contributes flux to the aperture, so it is kept
    # as a ranked alternative rather than resolved away.
    match_radius_pixels: float = 1.0
    # Gaia DR3 positions are on the J2016.0 frame; every cone match against an
    # epoch-specific catalog propagates proper motion to that catalog's epoch
    # before comparing. At our magnitudes the PM error is negligible; the win
    # is against 2MASS-era mismatches for high-proper-motion M dwarfs, which
    # are precisely the primary lane.
    gaia_reference_epoch_jyear: float = 2016.0
    # 2MASS observations span 1997-2001; its catalogued positions are quoted at
    # the mean epoch of the survey.
    twomass_reference_epoch_jyear: float = 1999.3
    # RUWE above this raises the blend prior. It never kills on its own --
    # planet hosts have binaries (MASTER_PLAN 4.2).
    ruwe_blend_prior: float = 1.4
    # A neighbour this much fainter than the target cannot dilute to the
    # observed depth even if it eclipsed totally, so it is recorded but not
    # ranked as a plausible host.
    max_neighbour_delta_mag: float = 10.0
    # Snapshot generations retained on disk. Manifests (hash, row count,
    # query) are kept forever so an old adjudication stays interpretable; only
    # the bulk rows of older generations are pruned.
    snapshot_generations_kept: int = 3


CURRENT_IDENTITY = IdentityConfig()


@dataclass(frozen=True, slots=True)
class EphemerisMatchConfig:
    """When a catalog ephemeris is the *same signal* (MASTER_PLAN 4.3).

    The rule this replaces accepted a 1% period-ratio match with no epoch test.
    At the 0.5-3 day periods where eclipsing binaries pile up, unrelated
    signals agree on period constantly, so that rule manufactures "known"
    verdicts -- and a false *known* is as damaging as a false *novel*, because
    it discards a genuine new signal silently instead of loudly.
    """

    policy_version: str = "ephemeris-period-and-epoch-v1"
    # Period agreement, as a fraction of the catalogued period. Continuity with
    # the historical gate; the epoch test is what makes it decisive.
    period_tolerance_fraction: float = 0.01
    # Phase agreement is required within max(this fraction of the candidate's
    # own duration, the drift the catalog's period error accumulates over the
    # elapsed cycles). Half a duration is the point at which two events stop
    # overlapping at all.
    phase_tolerance_duration_fraction: float = 0.5
    # Used as the per-cycle drift allowance only when a catalog row quotes no
    # period uncertainty of its own.
    assumed_period_uncertainty_fraction: float = 0.01
    # Once the propagated phase uncertainty reaches this fraction of the
    # period, the catalog prediction no longer says *where* the transit is, so
    # the relation is reported as period-only rather than as agreement. The
    # same reasoning as CatalogMaskConfig.max_phase_uncertainty_durations,
    # applied to matching instead of masking.
    max_phase_uncertainty_periods: float = 0.25


CURRENT_EPHEMERIS_MATCH = EphemerisMatchConfig()


@dataclass(frozen=True, slots=True)
class PixelVetConfig:
    """Pixel localization settings (T6, MASTER_PLAN 4.4).

    Version 1 asked one question -- is the difference-image centroid within a
    pixel of the target -- and answered it with a bare distance. That is a
    point estimate with no uncertainty, computed once, from one sector, using
    one aperture, and it cannot distinguish "on target" from "the blend is
    close enough that a single centroid lands inside the tolerance".
    """

    policy_version: str = "pixel-vet-v2-aperture-growth"
    # Three apertures suffice (section 4.4). Depth rising with aperture radius
    # means flux from a contaminating neighbour is entering the mask; depth
    # falling is ordinary dilution of an on-target signal.
    aperture_radii_pixels: tuple[float, ...] = (1.0, 2.0, 3.0)
    # Change in depth across the aperture range, normalized by the larger of
    # the two depths so the statistic is bounded in [-1, 1]: +1 means the whole
    # signal lies outside the target aperture, 0 means the same depth either
    # way, negative is ordinary dilution. Normalizing by the *inner* depth
    # instead divides by a near-zero denominator whenever the contaminant is
    # well separated, which is exactly the case the test is for.
    # Kill at 0.5: the wide aperture is at least twice as deep as the target's.
    aperture_growth_kill_fraction: float = 0.50
    aperture_growth_flag_fraction: float = 0.20
    # Localization verdicts carry uncertainties, not just a distance. The
    # bootstrap resamples the in- and out-of-transit cadence selection, which
    # is the choice the centroid is most sensitive to.
    bootstrap_samples: int = 256
    # An offset only counts as off-target when it is significant against its
    # own bootstrap scatter, not merely larger than a fixed pixel distance.
    centroid_offset_sigma: float = 3.0
    # Per-sector consistency: a centroid that wanders between sectors is a
    # blend signature even when every individual sector sits inside tolerance.
    # Reduced chi-square of the per-sector offsets about their weighted mean.
    sector_consistency_max_chi2: float = 3.0
    minimum_sectors_for_consistency: int = 2
    # Neighbour extraction only means anything when the apertures are actually
    # independent. Two 1-pixel apertures whose centres are 1 pixel apart share
    # most of their pixels, so "which is deeper" is a coin flip on noise.
    # Measured on the first real cohort: 22 of 58 stars were reassigned to a
    # neighbour at a median separation of 1.00 px and depth signal-to-noise
    # between 0.002 and 0.57 -- every one of them spurious (correction 46).
    # At 21 arcsec per pixel, most Gaia counterparts are simply not resolvable
    # by TESS, and the honest answer is that this test does not apply.
    neighbour_minimum_separation_apertures: float = 2.0
    # A host reassignment is a strong claim; it needs a depth that is real.
    neighbour_minimum_depth_snr: float = 3.0
    # ...and one that beats the target by more than the noise, rather than by
    # being the least negative of several non-detections.
    neighbour_minimum_depth_margin: float = 0.25
    # Inherited from v1, named rather than inline.
    minimum_in_transit_cadences: int = 3
    minimum_out_of_transit_cadences: int = 10


CURRENT_PIXEL_VET = PixelVetConfig()


@dataclass(frozen=True, slots=True)
class CrossReductionConfig:
    """Independent-reduction promotion gate (T7, MASTER_PLAN 4.5)."""

    policy_version: str = "t7-independent-reduction-v1"
    # Two independent reductions of the same pixels are the minimum that can
    # distinguish a signal from one pipeline's processing of it.
    minimum_independent_reductions: int = 2
    depth_agreement_sigma: float = 3.0
    # A reduction only counts toward agreement if it measured something. Two
    # depths "agree" trivially when one carries an uncertainty large enough to
    # cover any value -- measured on the first real cohort, where QLP at FFI
    # cadence returned +/-20,000 ppm errors and duly agreed with everything,
    # including a SPOC depth seven times its own. Agreement must mean the
    # reductions confirm each other, not that one of them is uninformative.
    minimum_reduction_significance: float = 3.0
    # The undetrended SAP requirement is the direct lesson of this project's
    # own history: a detrender can manufacture a periodic dip, and every
    # detrended product inherits it. An undetrended fold cannot.
    require_undetrended_detection: bool = True
    undetrended_minimum_sigma: float = 3.0
    # A sector only gets to vote *against* a signal if injection says the
    # signal would have been recoverable there. Below this completeness a
    # non-detection carries no information and must abstain rather than count
    # as evidence of absence.
    sector_veto_minimum_completeness: float = 0.50
    # Re-measured on the all-sector stacked fold, not per sector: TIC
    # 181014443's secondary was 2.3 sigma in one sector and 5.9 stacked.
    stacked_secondary_kill_sigma: float = 3.0
    stacked_odd_even_kill_sigma: float = 3.0


CURRENT_CROSS_REDUCTION = CrossReductionConfig()


@dataclass(frozen=True, slots=True)
class TransitFitConfig:
    """Transit fit and physical sanity checks (T8, MASTER_PLAN 4.6)."""

    policy_version: str = "t8-transit-fit-density-sanity-v1"
    walkers: int = 32
    burn_in_steps: int = 500
    production_steps: int = 2000
    # Posteriors are only reported when the chain has actually mixed. A
    # fit that has not converged is a number with an error bar attached to
    # nothing, and it looks identical to one that has.
    max_autocorrelation_ratio: float = 50.0
    # Stellar density from the fitted a/R* and period, against the density
    # implied by the catalogued mass and radius. A transit fit that requires a
    # star of the wrong density is the classic giant-impostor and blend
    # signature: the same light curve fits a small planet on a dwarf or a
    # grazing binary on a giant, and only the density separates them.
    density_agreement_sigma: float = 3.0
    # Below this the fitted density is unphysical for any main-sequence star,
    # regardless of what the catalogue says.
    minimum_physical_density_solar: float = 0.01
    maximum_physical_density_solar: float = 100.0
    # Quadratic limb darkening, interpolated from stellar temperature. These
    # bracket the TESS band for FGKM dwarfs; they are priors, not fixed
    # values, and the fit is allowed to move them.
    limb_darkening_u1_range: tuple[float, float] = (0.1, 0.6)
    limb_darkening_u2_range: tuple[float, float] = (0.0, 0.4)


CURRENT_TRANSIT_FIT = TransitFitConfig()


@dataclass(frozen=True, slots=True)
class ScienceConfig:
    """Everything that defines a result's scientific identity, in one object."""

    search: SearchConfig = field(default_factory=SearchConfig)
    vetoes: VetoConfig = field(default_factory=VetoConfig)
    detrend: DetrendConfig = field(default_factory=DetrendConfig)
    catalog_masking: CatalogMaskConfig = field(default_factory=CatalogMaskConfig)
    population: PopulationConfig = field(default_factory=PopulationConfig)
    instrument: InstrumentConfig = field(default_factory=InstrumentConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)

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


def require_clean_repository(repo_root: str | Path | None = None) -> None:
    """Refuse release evidence whose git commit does not describe its code."""

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(repo_root) if repo_root else str(Path(__file__).parent),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Could not verify a clean repository for release evidence.") from exc
    if result.returncode != 0:
        raise RuntimeError("Could not verify a clean repository for release evidence.")
    if result.stdout.strip():
        raise RuntimeError(
            "Release calibration requires a clean git worktree so code_version "
            "identifies the code that actually ran. Commit or stash changes, or "
            "use --allow-dirty only for a diagnostic smoke run."
        )


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


def settings_signature(
    *,
    code: str,
    settings: dict[str, object],
    product_family: str,
    target_list_hash: str,
) -> str:
    """Sign the settings the shipping path actually consumed.

    ``scientific_signature`` remains the identity for the complete frozen
    ``ScienceConfig``.  Campaigns also have command-level scientific inputs
    (author, cadence, period bounds, and the shipped detrending constants), so
    their release evidence must sign that exact serialized mapping rather than
    a nearby configuration object.
    """

    canonical = json.dumps(
        {
            "code": code,
            "settings": settings,
            "product_family": product_family,
            "target_list": target_list_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sig1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Every module whose text can change what a search returns. `cli.py` is on the
# list because P2's decomposition is unfinished: the single-target
# `_hunt_from_light_curve` analysis path still lives there, so a change to it
# is a change to the science whatever the file is nominally about.
DETECTION_KERNEL_MODULES = (
    "calibration.py",
    "campaign.py",
    "cli.py",
    "commonmode.py",
    "config.py",
    "detection.py",
    "detrend.py",
    "detrending.py",
    "photometry.py",
    "population.py",
    "screening.py",
    "search.py",
    "vetoes.py",
)


def kernel_version() -> str:
    """Identify the code that actually produces a detection.

    `code_version` answers "which commit is checked out", which retires a
    trusted release whenever *anything* in the repository moves -- a README
    edit, a dashboard rebuild, a new vetting module. That is not a
    conservative identity, it is a wrong one: it claims a calibration stopped
    describing code that did not change, and it did exactly that after P3
    (correction 39).

    This digests the modules that can alter a search result, so re-calibration
    is forced precisely when the search changes and never merely because the
    repository did.
    """

    return "kernel1:" + module_digest(*DETECTION_KERNEL_MODULES)


def module_digest(*module_names: str) -> str:
    """Digest the source text of specific modules.

    ``code_version`` answers "which commit is checked out", which is the right
    identity for a survey run but the wrong one for a layer that must stay
    stable across unrelated edits: a README fix moves ``git rev-parse HEAD``
    and would retire an otherwise-valid vetting generation. This digests only
    the modules that actually compute the verdict, so re-adjudication is forced
    exactly when the adjudicating code changes.
    """

    if not module_names:
        raise ValueError("module_digest needs at least one module name.")
    here = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in sorted(module_names):
        path = here / name
        if not path.is_file():
            raise FileNotFoundError(f"Cannot digest missing module: {path}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def vetting_signature(
    *,
    code: str,
    identity: IdentityConfig,
    snapshots: dict[str, str],
    matching: EphemerisMatchConfig | None = None,
) -> str:
    """Digest what a catalog adjudication means.

    ``snapshots`` maps each consulted source name to the content hash of the
    generation it was adjudicated against, so "re-vet the world against new
    catalogs" produces provably different evidence instead of quietly
    overwriting the old verdict's meaning (MASTER_PLAN section 4).
    """

    canonical = json.dumps(
        {
            "code": code,
            "identity": asdict(identity),
            "matching": asdict(matching or CURRENT_EPHEMERIS_MATCH),
            "snapshots": dict(sorted(snapshots.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "vet1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def match_radius_arcsec(
    identity: IdentityConfig | None = None,
    instrument: InstrumentConfig | None = None,
) -> float:
    """Counterpart search radius in arcseconds, derived from the pixel scale."""

    identity = identity or CURRENT_IDENTITY
    instrument = instrument or CURRENT_CONFIG.instrument
    return identity.match_radius_pixels * instrument.pixel_scale_arcsec


def hash_target_list(path: str | Path) -> str:
    """Content hash of a target list file, for signature inputs."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
