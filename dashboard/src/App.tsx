import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { createStarfield, StarfieldMode, type Starfield } from "./starfield";
import {
  ALL_STATUSES,
  STATUS_HELP,
  STATUS_META,
  STATUS_SYMBOL,
  type Status,
} from "./generated/statusRegistry";
import tessSectorGeometry from "./tess-sector-footprints.json";

type Star = {
  tic_id: number;
  name: string;
  status: Status;
  status_label: string;
  notes: string;
  ra_deg: number;
  dec_deg: number;
  distance_pc: number;
  distance_is_estimated: boolean;
  direction_is_estimated: boolean;
  coordinate_source: string;
  tmag: number | null;
  teff_k: number | null;
  stellar_radius_solar: number | null;
  sectors: number[];
  period_days: number | null;
  depth_ppm: number | null;
  snr: number | null;
  duration_hours: number | null;
  observed_transits: number | null;
  screening_status: string | null;
  screening_class: string | null;
  rejection_reasons: string;
  followup_priority: number;
  followup_reasons: string;
  vetting_tier: string;
  deeper_vetting_flags: string;
  recommended_data_sources: string;
  planet_free: false;
  sensitivity_3d_ppm: number | null;
  sensitivity_12d_ppm: number | null;
  red_noise_adjusted_snr: number | null;
  event_coverage_fraction: number | null;
  positive_depth_event_fraction: number | null;
  phase_curve_available: boolean;
  context_disposition: string | null;
  context_followup_lane: string | null;
  context_source_states: Record<string, string> | null;
  context_report: string | null;
  science_vetted: boolean;
  science_disposition: string | null;
  science_on_target: boolean | null;
  science_centroid_offset_arcsec: number | null;
  science_sector_gate_passed: boolean | null;
  science_supported_sector_count: number | null;
  science_sectors_tested: number | null;
  science_supporting_sectors: number[];
  common_mode_verdict: string | null;
  common_mode_shared_targets: number | null;
  common_mode_expected_targets: number | null;
  common_mode_enrichment: number | null;
  common_mode_cameras_spanned: number | null;
  common_mode_sky_spread_deg: number | null;
  spacecraft_harmonic: string | null;
  spacecraft_harmonic_period_days: number | null;
  duration_at_grid_rail: boolean;
  period_at_search_ceiling: boolean;
  x: number;
  y: number;
  z: number;
};

type PhaseCurve = {
  schema_version: number;
  source: string;
  phase_min: number;
  phase_max: number;
  bin_count: number;
  phase: number[];
  median_residual_flux_ppm: number[];
  scatter_ppm: number[];
  count: number[];
  measurements_total: number;
  measurements_in_range: number;
};

type InFlightTarget = {
  tic_id: number;
  target: string;
  stage: string;
  module: string;
  stage_index: number;
  stage_count: number;
  elapsed_seconds: number;
  stage_elapsed_seconds: number;
  /** Present only while downloading; archive clients report no byte total. */
  downloaded_bytes?: number;
  download_bytes_per_second?: number;
};

type ActiveCampaign = {
  name: string;
  workflow?: "batch_hunt" | "context_vet" | "science_vet";
  state: "running" | "finalizing" | "retry_pending";
  target_list: string | null;
  sectors: number[];
  total_targets: number;
  completed_targets: number;
  counts: Record<string, number>;
  /** Absent for campaigns started before stage tracking existed. */
  in_flight?: InFlightTarget[];
  stages?: string[];
  runtime: {
    analysis_workers?: number;
    download_workers?: number;
    prefetch_targets?: number;
    downloads_in_flight?: number;
    analyses_in_flight?: number;
    downloaded_waiting?: number;
    targets_remaining?: number;
    science_products_downloaded?: number;
    performance?: {
      average_stars_per_hour?: number | null;
      rolling_stars_per_hour?: number | null;
      rolling_window_minutes?: number;
      rolling_samples?: number;
      elapsed_hours?: number;
      eta_hours?: number | null;
      estimated_completion_utc?: string | null;
    };
    vetting_coverage?: {
      eligible_targets?: number;
      measured_targets?: number;
      legacy_unmeasured_targets?: number;
      coverage_fraction?: number | null;
      warning?: string | null;
    };
  };
  started_at_utc: string;
  updated_at_utc: string;
};

type SurveyData = {
  schema_version: number;
  generated_at_utc: string;
  data_revision: string;
  stars_total: number;
  stats: Record<string, number | string | Record<string, number>>;
  status_counts: Record<string, number>;
  common_mode_screen?: {
    screened_targets: number;
    flagged_targets: number;
    observatory_systematic: number;
    localized_coincidence: number;
    flagged_fraction: number | null;
    scope: string;
  };
  science_vetting?: {
    vetted_targets: number;
    on_target: number;
    off_target: number;
    sector_gate_passed: number;
    passed_both_gates: number;
    scope: string;
  };
  observed_sectors: number[];
  sector_coverage: SectorCoverage[];
  active_campaigns: ActiveCampaign[];
  stars: Star[];
};

type StarPage = {
  page: number;
  page_size: number;
  pages: number;
  total: number;
  items: Star[];
};

type OpsData = {
  generated_at_utc: string;
  liveness: "live" | "stale" | "absent";
  live: boolean;
  heartbeat_age_seconds: number | null;
  heartbeat_at_utc: string | null;
  holder: string | null;
  live_threshold_seconds: number;
  queue_depths: Record<string, Record<string, number>>;
  alarms: Array<Record<string, unknown>>;
  liveness_basis: string;
};

type SectorCoverage = {
  sector: number;
  state: "completed" | "active" | "partial" | "unsearched";
  targeted_stars: number;
  analyzed_stars: number;
  progress_fraction: number;
  active_campaign: string | null;
  updated_at_utc: string | null;
  scope: string;
};

type ViewMode = "3d" | "sky" | "earth";

type FootprintPoint = {
  ra_deg: number;
  dec_deg: number;
  x: number;
  y: number;
  z: number;
};

type TessSectorFootprint = {
  sector: number;
  frame: "ICRS/J2000";
  spacecraft_boresight: {
    ra_deg: number;
    dec_deg: number;
    roll_deg: number;
  };
  cameras: Array<{
    camera: number;
    boresight_ra_deg: number;
    boresight_dec_deg: number;
    outline: FootprintPoint[];
    ccds: Array<{
      ccd: number;
      corners: FootprintPoint[];
    }>;
  }>;
};

const TESS_SECTOR_GEOMETRY = tessSectorGeometry as unknown as {
  model: string;
  precision_note: string;
  sectors: Record<string, TessSectorFootprint>;
};

const UNKNOWN_STATUS_HELP =
  "This classification was produced by a newer exporter than the loaded dashboard bundle. Rebuild the dashboard to see its full description.";

const UNKNOWN_STATUS_META = {
  label: "Unrecognized Classification",
  short: "Unknown class",
  color: "#94a3b8",
  className: "muted",
};

/** Tolerate a status written by a newer exporter than this bundle. */
function statusMeta(status: Status) {
  return STATUS_META[status] ?? UNKNOWN_STATUS_META;
}

const HELP = {
  filters: "Controls that decide which analyzed stars are visible on the map.",
  statusFilters:
    "Show or hide mapped stars based on their current-best ledger classification. Counts refresh from the read-only state projection; they do not include aggregate validation benchmarks.",
  distanceRange:
    "Only show stars closer than this distance. One parsec is about 3.26 light-years.",
  stellarTemperature:
    "Only show stars cooler than this surface temperature. Lower values generally mean redder stars.",
  stellarRadius:
    "Only show stars smaller than this size, measured relative to the Sun.",
  minimumSnr:
    "Only show signals at or above this signal-to-noise ratio. Higher values stand out more clearly from random noise.",
  tessSector:
    "A TESS sector is one patch of sky observed continuously for roughly 27 days.",
  threeD:
    "Places stars in a rotatable Galactic coordinate frame. TIC sky directions are used when available; display-only estimates are clearly marked where catalog coordinates or distances are missing.",
  skyProjection:
    "Flattens the celestial sphere into right ascension and declination, like a sky atlas.",
  earthView:
    "Shows distance and sky direction in a view centered on Earth.",
  coordinateFrame:
    "The coordinate system used to turn astronomical positions into locations on this map.",
  galacticXyz:
    "Sun-centered Galactic axes: X points toward the Milky Way center, Y follows Galactic longitude 90°, and Z points toward the north Galactic pole.",
  galacticPlane:
    "The local Milky Way mid-plane, Galactic latitude b = 0°. Its disk, distance rings, and vertical curves rotate rigidly with the stars.",
  raDec:
    "Right ascension and declination are the sky equivalents of longitude and latitude.",
  distanceRa:
    "A view combining how far away a star is with its right-ascension direction.",
  zoom:
    "The current map magnification. The distance or angle scale changes automatically as you zoom.",
  scale:
    "The length represented by this bar at the current zoom level.",
  tic:
    "TIC means TESS Input Catalog. Its number is the star's identifier in the TESS target catalog.",
  ra: "Right ascension gives east-west position on the sky, similar to longitude.",
  dec: "Declination gives north-south position on the sky, similar to latitude.",
  distance:
    "Distance from Earth in parsecs. A leading approximation sign means the dashboard is using a display-only estimate rather than a catalog measurement.",
  tessMagnitude:
    "Brightness measured in the TESS camera's wavelength range. Smaller numbers mean brighter stars.",
  stellarRadiusValue: "The star's estimated radius compared with the Sun's radius.",
  stellarTemperatureValue: "The star's estimated surface temperature in kelvin.",
  observedSectors: "The TESS observing sectors whose data were searched for this star.",
  recoveredPeriod: "The repeating time between the strongest detected dimming events.",
  transitDepth:
    "How much the star dims during the event, measured in parts per million. A deeper dip can mean a larger object or an eclipsing binary.",
  signalToNoise:
    "Signal strength divided by the estimated random noise. Larger values are easier to distinguish, but can still be false positives.",
  catalogueStatus: "The best current classification recorded by this local survey and public checks.",
  coordinateSource: "Where the star's sky position and distance information came from.",
  phaseFolded:
    "Actual normalized residual TESS measurements, folded at the detected period and summarized into 160 compact bins. The line shows median residual brightness and the bars show robust scatter. Older searches do not have this stored curve.",
  phase:
    "Position within one repeating cycle. Phase zero is centered on the detected event.",
  orbitalDiagram:
    "A simplified sketch of the repeating event. Sizes and distances are illustrative, not literal.",
  radiusRatio:
    "Estimated object radius divided by star radius, approximated from the dip depth.",
  eventsSeen: "The number of separate dimming events represented in the searched data.",
  duration: "How long one detected dimming event lasts.",
  targetsMapped: "Unique stars currently represented in the dashboard, including live campaign results.",
  noVettedSignal:
    "A broad legacy bucket for stars searched before the newer triage labels were recorded. It does not mean planet-free.",
  noTransitDetected:
    "No repeating transit crossed the detection threshold in the searched TESS window. A planet can still be non-transiting, too small, outside the searched period range, hidden in a data gap, or missed by the pipeline.",
  screenedRejected:
    "The strongest repeating feature failed an automated plausibility check. This screens one signal; it does not rule out every planet around the star.",
  singleEventLeads:
    "Promising one-off dips that need a longer observing baseline before an orbital period can be established.",
  automatedSurvivors:
    "Signals that passed the automated gates and were placed in the deeper follow-up queue. These are leads, not vetted candidates or discoveries.",
  followupPriority:
    "A local triage score used to order deeper checks. Higher values mean more urgent review; it is not a probability that the signal is a planet.",
  sensitivityProbe:
    "The shallowest synthetic transit recovered at a fixed known period in this star's cleaned light curve. This compact probe describes local signal sensitivity, not blind-search completeness and not proof that the star is planet-free.",
  deeperVetting:
    "A second automated pass using the already-downloaded light curve. It checks red-noise-adjusted significance, event-to-event depths, event coverage, and whether a single event sits near a gap or boundary. It ranks follow-up; it does not confirm a planet.",
  redNoiseSnr:
    "Signal-to-noise after inflating the noise estimate for variability correlated across roughly one transit duration. This is more conservative than the original white-noise score.",
  eventCoverage:
    "The fraction of predicted transit windows that contain enough measurements to test the event. Low coverage means gaps may dominate the fitted period.",
  positiveEventFraction:
    "The fraction of individually sampled events that dim rather than brighten. Inconsistent event depths make a repeating signal less convincing.",
  followupSources:
    "Independent data suggested for deeper review. Alternate TESS reductions are broadly useful; Kepler/K2 and ground surveys are used only when their sky coverage and cadence fit the target.",
  averageThroughput:
    "Completed targets divided by total elapsed campaign time, including slow periods and retries.",
  rollingThroughput:
    "The recent completion rate measured from targets finished during the latest 15-minute window.",
  estimatedTime:
    "Remaining targets divided by the recent completion rate. It will move as archive speed, retries, and target complexity change.",
  vettingCoverage:
    "For a live context/science workflow, completed leads divided by its queue total. During a batch search, this instead reports how many eligible targets received the newer in-light-curve diagnostics.",
  parallelWorkers:
    "The live coordinator runs several analysis workers while a bounded download queue stages upcoming stars. One coordinator remains responsible for the checkpoint and dashboard.",
  activeWorkers:
    "Live tasks currently occupying worker slots. Batch searches separate analysis and download pools; context/science workflows report their vetting workers and science-product count. Configured capacity is not the same thing as active work.",
  searchErrors:
    "Targets whose data retrieval or analysis did not finish. They need a retry and are kept separate from completed no-signal searches.",
  planetRecoveries:
    "Mapped survey stars whose search recovered an already-known planet. This uses the same per-star classification and live count as the status filter.",
  validationRecoveries:
    "Known planets recovered by the separate validation benchmark suite. This measures pipeline performance and is deliberately kept separate from mapped-star classifications.",
  tceRecoveries: "Signals that match existing TESS threshold-crossing events.",
  falsePositives: "Signals rejected after additional vetting because they are probably not planets.",
  newCandidates: "Signals that passed the defined vetting steps but are not confirmed planets.",
  coverage:
    "The map's display-distance scale. Catalog distances are used when available; display-only estimates are marked and are not excluded by the distance filter.",
  sectorsRepresented:
    "How many distinct TESS sectors contain at least one successfully analyzed local target. This is not whole-sector sky completeness.",
  campaignRuns: "Completed batches of stars recorded in the permanent survey ledger.",
  polling: "How often the browser asks the local server for new campaign data.",
  timeline:
    "Completion of this project's local target plans by TESS sector. It does not claim that every star or pixel in a sector was searched.",
  completedSector:
    "Every unique star in the local sector-specific campaign plan finished successfully. Blue means the local plan is complete, not that every star in the TESS sector was analyzed.",
  activeSector:
    "This sector is being processed now. Its orange fill grows as more targets finish.",
  partialSector:
    "Some locally targeted stars were analyzed, but no complete sector-specific plan exists or unresolved targets remain. Gray fill shows the local analyzed/targeted fraction.",
  noLocalTarget:
    "No active or previously analyzed local campaign target is recorded for this sector.",
  sectorFootprint:
    "The modeled TESS observing footprint for the highlighted sector: four cameras with four CCDs each. In 3D the lines are angular sight lines extending from the observer, not a finite box in space. Boundaries come from the TESS focal-plane pointing model; calibrated-image WCS is the final pixel-level authority.",
};

function InfoTerm({
  children,
  description,
  className = "",
  focusable = true,
}: {
  children: React.ReactNode;
  description: string;
  className?: string;
  focusable?: boolean;
}) {
  const termRef = useRef<HTMLSpanElement>(null);
  const tooltipId = useId();
  const [tooltip, setTooltip] = useState<{
    left: number;
    top: number;
    placement: "above" | "below";
  } | null>(null);

  const showTooltip = () => {
    const element = termRef.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    const width = 270;
    const left = Math.max(
      width / 2 + 10,
      Math.min(window.innerWidth - width / 2 - 10, rect.left + rect.width / 2),
    );
    const above = rect.bottom + 120 > window.innerHeight && rect.top > 120;
    setTooltip({
      left,
      top: above ? rect.top - 8 : rect.bottom + 8,
      placement: above ? "above" : "below",
    });
  };

  useEffect(() => {
    if (!tooltip) return;
    const close = () => setTooltip(null);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [tooltip]);

  return (
    <>
      <span
        ref={termRef}
        className={`info-term ${className}`.trim()}
        tabIndex={focusable ? 0 : undefined}
        aria-describedby={tooltip ? tooltipId : undefined}
        onMouseEnter={showTooltip}
        onMouseLeave={() => setTooltip(null)}
        onFocus={showTooltip}
        onBlur={() => setTooltip(null)}
      >
        {children}
      </span>
      {tooltip
        ? createPortal(
            <span
              id={tooltipId}
              className={`term-tooltip ${tooltip.placement}`}
              role="tooltip"
              style={{ left: tooltip.left, top: tooltip.top }}
            >
              {description}
            </span>,
            document.body,
          )
        : null}
    </>
  );
}

function fmt(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function fmtInteger(value: number | string | undefined) {
  const number = Number(value ?? 0);
  return number.toLocaleString();
}

function fmtDuration(hours: number | null | undefined) {
  if (hours === null || hours === undefined || !Number.isFinite(hours)) return "—";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m`;
  const wholeHours = Math.floor(hours);
  const minutes = Math.round((hours - wholeHours) * 60);
  if (wholeHours < 24) return `${wholeHours}h ${minutes}m`;
  const days = Math.floor(wholeHours / 24);
  return `${days}d ${wholeHours % 24}h`;
}

function relativeUpdate(iso: string) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 15) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

function selectActiveCampaign(campaigns: ActiveCampaign[]) {
  return campaigns.reduce<ActiveCampaign | undefined>((selected, campaign) => {
    if (!selected) return campaign;
    return new Date(campaign.updated_at_utc).getTime() >
      new Date(selected.updated_at_utc).getTime()
      ? campaign
      : selected;
  }, undefined);
}

function niceScale(value: number) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const fraction = value / 10 ** exponent;
  const niceFraction = fraction < 1.5 ? 1 : fraction < 3.5 ? 2 : fraction < 7.5 ? 5 : 10;
  return niceFraction * 10 ** exponent;
}

function distanceScaleLabel(parsecs: number) {
  if (parsecs >= 1000) return `${fmt(parsecs / 1000, parsecs >= 10_000 ? 0 : 1)} kpc`;
  if (parsecs >= 0.1) return `${fmt(parsecs, parsecs >= 10 ? 0 : 2)} pc`;
  const au = parsecs * 206_264.806;
  return au >= 1000 ? `${fmt(au / 1000, 1)}k AU` : `${fmt(au, au >= 10 ? 0 : 1)} AU`;
}

function angleScaleLabel(degrees: number) {
  if (degrees >= 1) return `${fmt(degrees, degrees >= 10 ? 0 : 2)}°`;
  const arcminutes = degrees * 60;
  if (arcminutes >= 1) return `${fmt(arcminutes, arcminutes >= 10 ? 0 : 1)}′`;
  return `${fmt(arcminutes * 60, 1)}″`;
}

function Marker({ status, small = false }: { status: Status; small?: boolean }) {
  return (
    <span
      className={`marker marker-${status} ${small ? "marker-small" : ""}`}
      style={{ "--marker-color": statusMeta(status).color } as React.CSSProperties}
      aria-hidden="true"
    >
      {STATUS_SYMBOL[status] ?? "?"}
    </span>
  );
}

function drawCanvasStatusMarker(
  ctx: CanvasRenderingContext2D,
  star: Star,
  x: number,
  y: number,
  selected: boolean,
) {
  const sprite = statusSprite(star.status, selected);
  // One blit per star. Every marker of a given status is pixel-identical, so
  // rasterising the shape and its glyph once and reusing the bitmap replaces
  // a save/restore, a font assignment, a path and a `fillText` per star --
  // the per-star text rasterisation was the dominant cost of drawing a
  // twelve-thousand-star field at 60fps.
  ctx.drawImage(sprite.bitmap, x - sprite.centre, y - sprite.centre,
    sprite.cssSize, sprite.cssSize);
  return sprite.hitRadius;
}

/**
 * Pre-rendered marker bitmaps, keyed by status and selection.
 *
 * There are on the order of thirty distinct markers and twelve thousand
 * stars, so every marker is drawn tens or hundreds of times per frame with
 * identical parameters. Rasterising each one once and blitting it is the
 * same picture for a fraction of the work.
 *
 * Sprites are rendered at device-pixel resolution so they stay crisp on
 * high-DPI displays, and the cache is keyed on that ratio so moving the
 * window between monitors re-renders rather than upscaling.
 */
const SPRITE_CACHE = new Map<
  string,
  { bitmap: HTMLCanvasElement; centre: number; cssSize: number; hitRadius: number }
>();

/**
 * Pack every status marker into one texture atlas for the GPU layer.
 *
 * The tiles are produced by `statusSprite`, the same function the 2D
 * renderer uses, so a marker on the map is pixel-identical to the one in the
 * status key. Generating simplified shapes for the GPU instead would let the
 * map and its legend drift apart, which is worse than being slow: the reader
 * would be looking at symbols that no longer mean what the key says.
 */
/**
 * The handful of stars being worked on right now.
 *
 * Aggregate counters say four analyses are running; this says *which* stars
 * and what each is doing, which is the difference between knowing the run is
 * alive and being able to see it work. Rows appear when a target starts and
 * disappear when it finishes, so the panel is always the present tense.
 *
 * It renders nothing at all when the campaign predates stage tracking, so an
 * older run shows the metrics it always did rather than an empty frame.
 */
function InFlightPanel({ campaign }: { campaign?: ActiveCampaign }) {
  const rows = campaign?.in_flight || [];
  // The checkpoint is throttled to ~5s and the browser polls on the same
  // cadence, so raw values step in five-second jumps. Record when this
  // payload arrived and extrapolate from it at 10Hz: the numbers advance
  // smoothly, and every poll re-anchors them to the truth so the display
  // can drift by at most one frame rather than accumulating error.
  const arrivedAt = useMemo(() => performance.now(), [rows]);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (rows.length === 0) return;
    const id = window.setInterval(() => setTick((n) => n + 1), 100);
    return () => window.clearInterval(id);
  }, [rows.length]);
  void tick;
  const drift = Math.max(0, (performance.now() - arrivedAt) / 1000);

  if (!campaign || rows.length === 0) return null;
  const stages = campaign.stages && campaign.stages.length
    ? campaign.stages
    : ["queued", "downloading", "preparing", "masking", "searching", "vetting", "writing"];
  return (
    <div className="inflight">
      <div className="inflight-head">
        <InfoTerm description="The targets this campaign is processing right now, and which module is handling each one. Progress detail only; it is not a scientific result.">
          NOW PROCESSING
        </InfoTerm>
        <span className="inflight-count">
          {rows.length} in flight · {stages.length} stages
        </span>
      </div>
      <div className="inflight-rows">
        {rows.map((row) => {
          const index = Math.max(0, Math.min(row.stage_index, stages.length - 1));
          const stageElapsed = row.stage_elapsed_seconds + drift;
          const totalElapsed = row.elapsed_seconds + drift;
          const downloading = row.stage === "downloading";
          const bytes = row.downloaded_bytes;
          const rate = row.download_bytes_per_second;
          return (
            <div className="inflight-row" key={row.tic_id}>
              <span className="inflight-tic" title={row.target || `TIC ${row.tic_id}`}>
                TIC {row.tic_id}
              </span>
              <span className="inflight-stage" data-stage={row.stage}>
                {row.stage}
              </span>
              <span className="inflight-module">
                {downloading && bytes !== undefined
                  ? `${fmtBytes(bytes)}${rate ? ` · ${fmtBytes(rate)}/s` : ""}`
                  : row.module}
              </span>
              <span className="inflight-bar" aria-hidden="true">
                {stages.map((stage, position) => (
                  <b
                    key={stage}
                    className={
                      position < index
                        ? "done"
                        : position === index
                          ? "current"
                          : ""
                    }
                    title={stage}
                  />
                ))}
              </span>
              <span className="inflight-elapsed">
                {fmtElapsed(stageElapsed)}
                <em> / {fmtElapsed(totalElapsed)}</em>
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function fmtElapsed(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  // One decimal under a minute so the extrapolated counter visibly moves
  // rather than sitting on an integer for a second at a time.
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function fmtBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const ATLAS_TILE_CSS = 26;

function buildStatusAtlas(dpr: number) {
  const statuses = ALL_STATUSES as ReadonlyArray<Star["status"]>;
  const columns = Math.ceil(Math.sqrt(statuses.length));
  const rows = Math.ceil(statuses.length / columns);
  const image = document.createElement("canvas");
  image.width = Math.round(columns * ATLAS_TILE_CSS * dpr);
  image.height = Math.round(rows * ATLAS_TILE_CSS * dpr);
  const ctx = image.getContext("2d");
  if (!ctx) return null;
  ctx.scale(dpr, dpr);
  const index = new Map<string, number>();
  statuses.forEach((status, i) => {
    index.set(status, i);
    const sprite = statusSprite(status, false);
    const centreX = (i % columns) * ATLAS_TILE_CSS + ATLAS_TILE_CSS / 2;
    const centreY =
      Math.floor(i / columns) * ATLAS_TILE_CSS + ATLAS_TILE_CSS / 2;
    ctx.drawImage(
      sprite.bitmap,
      centreX - sprite.centre,
      centreY - sprite.centre,
      sprite.cssSize,
      sprite.cssSize,
    );
  });
  return { image, columns, rows, tileCssSize: ATLAS_TILE_CSS, index };
}

function statusSprite(status: Star["status"], selected: boolean) {
  const dpr = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1;
  const key = `${status}|${selected ? 1 : 0}|${dpr}`;
  const cached = SPRITE_CACHE.get(key);
  if (cached) return cached;

  const meta = statusMeta(status);
  const symbol = STATUS_SYMBOL[status] ?? "?";
  const size = selected ? 13 : status === "searched" ? 5 : 10;
  const half = size / 2;
  const hitRadius = Math.max(8, half + 5);
  // Enough room for the widest marker plus the dashed selection ring.
  const cssSize = Math.ceil((half + 8) * 2);
  const centre = cssSize / 2;

  const bitmap = document.createElement("canvas");
  bitmap.width = Math.round(cssSize * dpr);
  bitmap.height = Math.round(cssSize * dpr);
  const ctx = bitmap.getContext("2d");
  if (!ctx) {
    const fallback = { bitmap, centre, cssSize, hitRadius };
    SPRITE_CACHE.set(key, fallback);
    return fallback;
  }
  ctx.scale(dpr, dpr);

  const x = centre;
  const y = centre;
  ctx.globalAlpha = status === "searched" ? 0.72 : 0.96;
  ctx.strokeStyle = meta.color;
  ctx.fillStyle = meta.color;
  ctx.lineWidth = selected ? 2 : 1.4;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.font = `800 ${selected ? 10 : 8}px ui-monospace, monospace`;

  if (status === "searched") {
    ctx.fillRect(x - half, y - half, size, size);
  } else if (status === "no_transit_detected") {
    ctx.beginPath();
    ctx.arc(x, y, half, 0, Math.PI * 2);
    ctx.stroke();
  } else if (status === "screened_rejected" || status === "false_positive") {
    ctx.font = `800 ${selected ? 15 : 12}px ui-monospace, monospace`;
    ctx.fillText(symbol, x, y + 0.5);
  } else {
    const filled =
      status === "automated_survivor" ||
      status === "vetted_candidate" ||
      status === "confirmed_planet";
    if (status === "known_variable_star_review") {
      ctx.beginPath();
      ctx.arc(x, y, half + 0.5, 0, Math.PI * 2);
      if (filled) ctx.fill();
      else ctx.stroke();
    } else {
      if (filled) ctx.fillRect(x - half, y - half, size, size);
      else ctx.strokeRect(x - half, y - half, size, size);
    }
    ctx.fillStyle = filled ? "#06111a" : meta.color;
    ctx.fillText(symbol, x, y + 0.5);
  }

  if (selected) {
    ctx.strokeStyle = meta.color;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.arc(x, y, half + 5, 0, Math.PI * 2);
    ctx.stroke();
  }

  const entry = { bitmap, centre, cssSize, hitRadius };
  SPRITE_CACHE.set(key, entry);
  return entry;
}

function ActualPhaseCurve({ curve, color }: { curve: PhaseCurve; color: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);
    const points = curve.phase
      .map((phase, index) => ({
        phase,
        flux: curve.median_residual_flux_ppm[index],
        scatter: curve.scatter_ppm[index] || 0,
      }))
      .filter(
        (point) =>
          Number.isFinite(point.phase) &&
          Number.isFinite(point.flux) &&
          Number.isFinite(point.scatter),
      );
    if (!points.length) return;

    const sortedFlux = points.map((point) => point.flux).sort((a, b) => a - b);
    const quantile = (fraction: number) =>
      sortedFlux[Math.min(sortedFlux.length - 1, Math.floor((sortedFlux.length - 1) * fraction))];
    let yMin = Math.min(0, quantile(0.02));
    let yMax = Math.max(0, quantile(0.98));
    const initialSpan = Math.max(10, yMax - yMin);
    yMin -= initialSpan * 0.14;
    yMax += initialSpan * 0.14;
    const plotLeft = 34;
    const plotRight = Math.max(plotLeft + 1, w - 4);
    const plotTop = 7;
    const plotBottom = h - 7;
    const toX = (phase: number) =>
      plotLeft +
      ((phase - curve.phase_min) / (curve.phase_max - curve.phase_min)) *
        (plotRight - plotLeft);
    const toY = (flux: number) =>
      plotBottom - ((flux - yMin) / (yMax - yMin)) * (plotBottom - plotTop);

    ctx.strokeStyle = "rgba(88, 129, 151, .22)";
    ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) {
      const y = plotTop + ((plotBottom - plotTop) * i) / 4;
      ctx.beginPath();
      ctx.moveTo(plotLeft, y);
      ctx.lineTo(plotRight, y);
      ctx.stroke();
    }
    ctx.strokeStyle = "rgba(173, 208, 220, .28)";
    ctx.beginPath();
    ctx.moveTo(toX(0), plotTop);
    ctx.lineTo(toX(0), plotBottom);
    ctx.stroke();

    ctx.font = "8px monospace";
    ctx.fillStyle = "#718a95";
    ctx.textAlign = "right";
    ctx.fillText(`${Math.round(yMax)}`, plotLeft - 4, plotTop + 5);
    ctx.fillText("0", plotLeft - 4, toY(0) + 3);
    ctx.fillText(`${Math.round(yMin)}`, plotLeft - 4, plotBottom);

    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.25;
    for (const point of points) {
      const x = toX(point.phase);
      ctx.beginPath();
      ctx.moveTo(x, toY(Math.min(yMax, point.flux + point.scatter)));
      ctx.lineTo(x, toY(Math.max(yMin, point.flux - point.scatter)));
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = toX(point.phase);
      const y = toY(Math.max(yMin, Math.min(yMax, point.flux)));
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    for (const point of points) {
      ctx.beginPath();
      ctx.arc(
        toX(point.phase),
        toY(Math.max(yMin, Math.min(yMax, point.flux))),
        1.1,
        0,
        Math.PI * 2,
      );
      ctx.fill();
    }
  }, [color, curve]);
  return (
    <canvas
      ref={canvasRef}
      className="phase-canvas"
      aria-label="Actual binned phase-folded residual TESS photometry in parts per million"
    />
  );
}

function StarMap({
  stars,
  sectorFootprint,
  highlightedSector,
  selected,
  onSelect,
  mode,
}: {
  stars: Star[];
  sectorFootprint: TessSectorFootprint | null;
  highlightedSector: number | null;
  selected: Star | null;
  onSelect: (star: Star) => void;
  mode: ViewMode;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const pointsRef = useRef<Array<{ star: Star; x: number; y: number; r: number }>>([]);
  const dragRef = useRef({ active: false, moved: false, panning: false, x: 0, y: 0 });
  const [rotation, setRotation] = useState({ x: -0.36, y: -0.52 });
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [hovered, setHovered] = useState<Star | null>(null);
  const [scaleInfo, setScaleInfo] = useState({
    label: "50 pc",
    width: 90,
    location: "Centered on Sun / Earth",
  });

  // GPU star layer. Created once; null whenever WebGL2 is unavailable or the
  // context is lost, in which case the 2D renderer below keeps working
  // unchanged. It only replaces the star field itself -- the grid, rings,
  // labels, sector footprints and selection markers stay on the 2D canvas.
  const glCanvasRef = useRef<HTMLCanvasElement>(null);
  const starfieldRef = useRef<Starfield | null>(null);
  const [gpuReady, setGpuReady] = useState(false);

  useEffect(() => {
    const canvas = glCanvasRef.current;
    if (!canvas) return;
    const field = createStarfield(canvas);
    starfieldRef.current = field;
    setGpuReady(Boolean(field));
    return () => {
      field?.dispose();
      starfieldRef.current = null;
    };
  }, []);

  // Upload only when the star set changes, not per frame: this is the whole
  // point of the GPU path. The atlas is uploaded in the same effect so a
  // star can never reference a tile that has not been sent yet.
  useEffect(() => {
    const field = starfieldRef.current;
    if (!field) return;
    const atlas = buildStatusAtlas(window.devicePixelRatio || 1);
    if (!atlas) return;
    field.setAtlas(atlas);
    field.setStars(
      stars.map((star) => ({
        x: star.x,
        y: star.y,
        z: star.z,
        tile: atlas.index.get(star.status) ?? 0,
        raDeg: star.ra_deg,
        decDeg: star.dec_deg,
        distancePc: star.distance_pc,
      })),
    );
  }, [stars, gpuReady]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    // Assigning width/height reallocates and clears the backing store, so do
    // it only when the size actually changes rather than on every frame.
    const nextWidth = Math.round(rect.width * dpr);
    const nextHeight = Math.round(rect.height * dpr);
    if (canvas.width !== nextWidth || canvas.height !== nextHeight) {
      canvas.width = nextWidth;
      canvas.height = nextHeight;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);

    const baseCx = mode === "sky" ? w * 0.5 : w * 0.51;
    const baseCy = mode === "sky" ? h * 0.5 : h * 0.49;
    const cx = baseCx + pan.x;
    const cy = baseCy + pan.y;
    const mapRadius = Math.min(w * 0.47, h * 0.46) * zoom;
    const maxDistance = 155;
    const pixelsPerParsec = mapRadius / maxDistance;
    const skyPixelsPerRaDegree = ((w - 48) / 360) * zoom;
    const skyPixelsPerDecDegree = ((h - 48) / 180) * zoom;
    const targetScale =
      mode === "sky" ? 90 / skyPixelsPerRaDegree : 90 / pixelsPerParsec;
    const scaleValue = niceScale(targetScale);
    const scaleWidth = Math.max(
      36,
      Math.min(
        150,
        scaleValue * (mode === "sky" ? skyPixelsPerRaDegree : pixelsPerParsec),
      ),
    );
    const location =
      mode === "sky"
        ? `Center RA ${fmt(((180 - pan.x / skyPixelsPerRaDegree) % 360 + 360) % 360, 2)}° · Dec ${fmt(pan.y / skyPixelsPerDecDegree, 2)}°`
        : `Center offset ${fmt(-pan.x / pixelsPerParsec, 2)} / ${fmt(-pan.y / pixelsPerParsec, 2)} pc`;
    const nextScale = {
      label: mode === "sky" ? angleScaleLabel(scaleValue) : distanceScaleLabel(scaleValue),
      width: Math.round(scaleWidth),
      location,
    };
    setScaleInfo((current) =>
      current.label === nextScale.label &&
      current.width === nextScale.width &&
      current.location === nextScale.location
        ? current
        : nextScale,
    );

    const gradientRadius = Math.min(mapRadius * 1.1, Math.max(w, h) * 2);
    const gradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, gradientRadius);
    gradient.addColorStop(0, "rgba(15, 53, 66, .2)");
    gradient.addColorStop(0.65, "rgba(3, 15, 25, .08)");
    gradient.addColorStop(1, "rgba(0, 5, 10, 0)");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, w, h);

    const projectGalacticPoint = (pointX: number, pointY: number, pointZ: number) => {
      const cosY = Math.cos(rotation.y);
      const sinY = Math.sin(rotation.y);
      const cosX = Math.cos(rotation.x);
      const sinX = Math.sin(rotation.x);
      const x1 = pointX * cosY - pointZ * sinY;
      const z1 = pointX * sinY + pointZ * cosY;
      const y1 = pointY * cosX - z1 * sinX;
      const z2 = pointY * sinX + z1 * cosX;
      const perspective = 1 / (1 + (z2 / maxDistance) * 0.22);
      return {
        x: cx + (x1 / maxDistance) * mapRadius * perspective,
        y: cy + (y1 / maxDistance) * mapRadius * perspective * 0.74,
        depth: z2,
      };
    };

    const traceProjectedCurve = (
      points: Array<[number, number, number]>,
      close = false,
    ) => {
      ctx.beginPath();
      points.forEach(([pointX, pointY, pointZ], index) => {
        const point = projectGalacticPoint(pointX, pointY, pointZ);
        if (index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      });
      if (close) ctx.closePath();
    };

    ctx.save();
    ctx.strokeStyle = "rgba(91, 138, 158, .22)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 5]);
    if (mode === "3d") {
      const farthestCorner = Math.max(
        Math.hypot(cx, cy),
        Math.hypot(w - cx, cy),
        Math.hypot(cx, h - cy),
        Math.hypot(w - cx, h - cy),
      );
      const planeStep = niceScale(72 / pixelsPerParsec);
      const visibleLimit = Math.min(
        maxDistance,
        Math.max(planeStep, farthestCorner / pixelsPerParsec),
      );
      const rings = Array.from(
        { length: Math.min(12, Math.floor(visibleLimit / planeStep)) },
        (_, index) => (index + 1) * planeStep,
      );
      const diskRadius = rings[rings.length - 1] || visibleLimit;
      const circlePoints = (radius: number, samples = 96) =>
        Array.from({ length: samples + 1 }, (_, index) => {
          const angle = (index / samples) * Math.PI * 2;
          return [
            radius * Math.cos(angle),
            radius * Math.sin(angle),
            0,
          ] as [number, number, number];
        });

      ctx.setLineDash([]);
      traceProjectedCurve(circlePoints(diskRadius), true);
      ctx.fillStyle = "rgba(25, 117, 148, .055)";
      ctx.fill();
      ctx.strokeStyle = "rgba(74, 213, 235, .34)";
      ctx.stroke();

      ctx.strokeStyle = "rgba(64, 151, 180, .18)";
      ctx.setLineDash([2, 6]);
      for (let index = 0; index < 6; index += 1) {
        const angle = (index / 6) * Math.PI;
        const dx = diskRadius * Math.cos(angle);
        const dy = diskRadius * Math.sin(angle);
        traceProjectedCurve([
          [-dx, -dy, 0],
          [dx, dy, 0],
        ]);
        ctx.stroke();
      }

      ctx.strokeStyle = "rgba(91, 182, 208, .3)";
      ctx.setLineDash([3, 5]);
      rings.forEach((ring) => {
        traceProjectedCurve(circlePoints(ring));
        ctx.stroke();
        const labelPoint = projectGalacticPoint(
          ring * Math.cos(0.62),
          ring * Math.sin(0.62),
          0,
        );
        ctx.fillStyle = "rgba(206, 230, 239, .76)";
        ctx.font = "600 11px var(--font-geist-mono)";
        ctx.fillText(distanceScaleLabel(ring), labelPoint.x + 4, labelPoint.y - 4);
      });

      ctx.strokeStyle = "rgba(126, 167, 184, .2)";
      ctx.setLineDash([3, 7]);
      [0, Math.PI / 3, (Math.PI * 2) / 3].forEach((longitude) => {
        const hoop = Array.from({ length: 73 }, (_, index) => {
          const angle = (index / 72) * Math.PI * 2;
          const radial = diskRadius * Math.cos(angle);
          return [
            radial * Math.cos(longitude),
            radial * Math.sin(longitude),
            diskRadius * Math.sin(angle),
          ] as [number, number, number];
        });
        traceProjectedCurve(hoop);
        ctx.stroke();
      });
      traceProjectedCurve([
        [0, 0, -diskRadius],
        [0, 0, diskRadius],
      ]);
      ctx.stroke();

      ctx.setLineDash([]);
      const planeLabel = projectGalacticPoint(
        diskRadius * Math.cos(-0.52),
        diskRadius * Math.sin(-0.52),
        0,
      );
      const northPoleLabel = projectGalacticPoint(0, 0, diskRadius);
      ctx.fillStyle = "rgba(94, 221, 239, .82)";
      ctx.font = "600 10px var(--font-geist-mono)";
      ctx.fillText("GALACTIC PLANE · b=0°", planeLabel.x + 7, planeLabel.y + 12);
      ctx.fillStyle = "rgba(178, 204, 215, .66)";
      ctx.fillText("+Z · NGP", northPoleLabel.x + 6, northPoleLabel.y - 5);
    } else if (mode === "sky") {
      const minRa = 180 + (0 - cx) / skyPixelsPerRaDegree;
      const maxRa = 180 + (w - cx) / skyPixelsPerRaDegree;
      const raStep = niceScale((maxRa - minRa) / 8);
      for (
        let ra = Math.ceil(minRa / raStep) * raStep;
        ra <= maxRa + raStep * 0.01;
        ra += raStep
      ) {
        const x = cx + (ra - 180) * skyPixelsPerRaDegree;
        ctx.beginPath();
        ctx.moveTo(x, 16);
        ctx.lineTo(x, h - 16);
        ctx.stroke();
        ctx.fillStyle = "rgba(206, 230, 239, .7)";
        ctx.font = "600 10px var(--font-geist-mono)";
        ctx.fillText(`${fmt(((ra % 360) + 360) % 360, 1)}°`, x + 4, 29);
      }
      const maxDec = (cy - 0) / skyPixelsPerDecDegree;
      const minDec = (cy - h) / skyPixelsPerDecDegree;
      const decStep = niceScale((maxDec - minDec) / 6);
      for (
        let dec = Math.ceil(minDec / decStep) * decStep;
        dec <= maxDec + decStep * 0.01;
        dec += decStep
      ) {
        const y = cy - dec * skyPixelsPerDecDegree;
        ctx.beginPath();
        ctx.moveTo(20, y);
        ctx.lineTo(w - 20, y);
        ctx.stroke();
        ctx.fillStyle = "rgba(206, 230, 239, .7)";
        ctx.font = "600 10px var(--font-geist-mono)";
        ctx.fillText(`${fmt(dec, 1)}°`, 24, y - 4);
      }
    } else {
      const farthestCorner = Math.max(
        Math.hypot(cx, cy),
        Math.hypot(w - cx, cy),
        Math.hypot(cx, h - cy),
        Math.hypot(w - cx, h - cy),
      );
      const ringStep = niceScale(72 / pixelsPerParsec);
      const visibleLimit = Math.min(maxDistance, farthestCorner / pixelsPerParsec);
      const rings = Array.from(
        { length: Math.min(16, Math.floor(visibleLimit / ringStep)) },
        (_, index) => (index + 1) * ringStep,
      );
      rings.forEach((ring) => {
        const radius = ring * pixelsPerParsec;
        ctx.beginPath();
        ctx.ellipse(cx, cy, radius, radius * 0.74, 0, 0, Math.PI * 2);
        ctx.stroke();
        ctx.fillStyle = "rgba(206, 230, 239, .7)";
        ctx.font = "600 10px var(--font-geist-mono)";
        ctx.fillText(distanceScaleLabel(ring), cx + radius * 0.7, cy - radius * 0.5);
      });
    }
    ctx.restore();

    const project = (star: Star) => {
      if (mode === "sky") {
        return {
          x: cx + (star.ra_deg - 180) * skyPixelsPerRaDegree,
          y: cy - star.dec_deg * skyPixelsPerDecDegree,
          depth: 0,
        };
      }
      if (mode === "earth") {
        const radius = (star.distance_pc / maxDistance) * mapRadius;
        const angle = (star.ra_deg / 180) * Math.PI;
        return {
          x: cx + Math.cos(angle) * radius,
          y: cy + Math.sin(angle) * radius * 0.74,
          depth: star.dec_deg / 90,
        };
      }
      return projectGalacticPoint(star.x, star.y, star.z);
    };

    if (highlightedSector !== null && sectorFootprint) {
      ctx.save();
      const cameraColors = [
        [255, 173, 32],
        [255, 204, 92],
        [255, 137, 72],
        [255, 224, 138],
      ] as const;
      const sightLineDistance = maxDistance * 0.96;
      const viewportCenterRa = 180 - pan.x / skyPixelsPerRaDegree;

      const projectAngularPoint = (point: FootprintPoint) => {
        if (mode === "earth") {
          const angle = (point.ra_deg / 180) * Math.PI;
          return {
            x: cx + Math.cos(angle) * sightLineDistance * pixelsPerParsec,
            y: cy + Math.sin(angle) * sightLineDistance * pixelsPerParsec * 0.74,
          };
        }
        return projectGalacticPoint(
          point.x * sightLineDistance,
          point.y * sightLineDistance,
          point.z * sightLineDistance,
        );
      };

      const unwrapSkyPolygon = (points: FootprintPoint[]) => {
        const unwrapped: Array<{ x: number; y: number }> = [];
        let previousRa = points[0]?.ra_deg ?? viewportCenterRa;
        while (previousRa - viewportCenterRa > 180) previousRa -= 360;
        while (previousRa - viewportCenterRa < -180) previousRa += 360;
        for (const point of points) {
          let ra = point.ra_deg;
          while (ra - previousRa > 180) ra -= 360;
          while (ra - previousRa < -180) ra += 360;
          unwrapped.push({
            x: cx + (ra - 180) * skyPixelsPerRaDegree,
            y: cy - point.dec_deg * skyPixelsPerDecDegree,
          });
          previousRa = ra;
        }
        return unwrapped;
      };

      const traceScreenPolygon = (
        points: Array<{ x: number; y: number }>,
      ) => {
        ctx.beginPath();
        points.forEach((point, index) => {
          if (index === 0) ctx.moveTo(point.x, point.y);
          else ctx.lineTo(point.x, point.y);
        });
        ctx.closePath();
      };

      sectorFootprint.cameras.forEach((camera, cameraIndex) => {
        const [red, green, blue] = cameraColors[cameraIndex];
        ctx.strokeStyle = `rgba(${red}, ${green}, ${blue}, .86)`;
        ctx.fillStyle = `rgba(${red}, ${green}, ${blue}, .055)`;
        ctx.lineWidth = 1.15;
        ctx.setLineDash([]);

        if (mode === "sky") {
          camera.ccds.forEach((ccd) => {
            const basePolygon = unwrapSkyPolygon(ccd.corners);
            for (const wrapOffset of [-360, 0, 360]) {
              const shifted = basePolygon.map((point) => ({
                x: point.x + wrapOffset * skyPixelsPerRaDegree,
                y: point.y,
              }));
              const minX = Math.min(...shifted.map((point) => point.x));
              const maxX = Math.max(...shifted.map((point) => point.x));
              if (maxX < -24 || minX > w + 24) continue;
              traceScreenPolygon(shifted);
              ctx.fill();
              ctx.stroke();
            }
          });
        } else {
          const outline = camera.outline.map(projectAngularPoint);
          outline.forEach((point, index) => {
            const next = outline[(index + 1) % outline.length];
            traceScreenPolygon([
              { x: cx, y: cy },
              point,
              next,
            ]);
            ctx.fill();
          });
          ctx.setLineDash([6, 5]);
          outline.forEach((point) => {
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(point.x, point.y);
            ctx.stroke();
          });
          ctx.setLineDash([]);
          camera.ccds.forEach((ccd) => {
            traceScreenPolygon(ccd.corners.map(projectAngularPoint));
            ctx.stroke();
          });
        }
      });

      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(255, 202, 92, .96)";
      ctx.font = "700 10px var(--font-geist-mono)";
      ctx.fillText(
        `SECTOR ${highlightedSector} · TESS CAMERA / CCD FOOTPRINT`,
        18,
        24,
      );
      ctx.restore();
    }

    // The vertex shader reproduces all three projections exactly, so the GPU
    // draws the field in every view. A machine without WebGL2 still falls
    // back to the 2D renderer below.
    const field = starfieldRef.current;
    const gpuStars = Boolean(field);
    if (field) {
      field.render({
        mode:
          mode === "sky"
            ? StarfieldMode.Sky
            : mode === "earth"
              ? StarfieldMode.Earth
              : StarfieldMode.Galactic,
        centreX: cx,
        centreY: cy,
        mapRadius,
        maxDistance,
        rotationX: rotation.x,
        rotationY: rotation.y,
        pixelRatio: dpr,
        width: w,
        height: h,
        skyPixelsPerRaDegree,
        skyPixelsPerDecDegree,
      });
    }

    const projectedStars = stars.map((star) => ({ star, ...project(star) }));
    // Depth order only matters when the CPU paints overlapping markers; the
    // GPU layer blends them, so the per-frame sort is skipped there.
    const projected = gpuStars
      ? projectedStars
      : projectedStars.sort((a, b) => a.depth - b.depth);
    const hitPoints: Array<{ star: Star; x: number; y: number; r: number }> = [];
    for (const point of projected) {
      const { star, x, y } = point;
      const selectedPoint = selected?.tic_id === star.tic_id;
      if (x < -20 || x > w + 20 || y < -20 || y > h + 20) continue;
      // With the GPU drawing the field, the 2D pass only needs the hit
      // radius -- except for the selected star, whose distinct marker and
      // ring must stay visible above the points.
      const hitRadius = gpuStars
        ? selectedPoint
          ? drawCanvasStatusMarker(ctx, star, x, y, true)
          : statusSprite(star.status, false).hitRadius
        : drawCanvasStatusMarker(ctx, star, x, y, selectedPoint);
      if (selectedPoint) {
        ctx.save();
        ctx.setLineDash([3, 4]);
        ctx.globalAlpha = 0.58;
        ctx.strokeStyle = statusMeta(star.status).color;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(x, y);
        ctx.stroke();
        ctx.restore();
      }
      hitPoints.push({ star, x, y, r: hitRadius });
    }
    pointsRef.current = hitPoints;

    ctx.save();
    const starGlow = ctx.createRadialGradient(cx, cy, 0, cx, cy, 28);
    starGlow.addColorStop(0, "#fffce5");
    starGlow.addColorStop(0.12, "#ffe07b");
    starGlow.addColorStop(0.3, "rgba(255, 193, 57, .34)");
    starGlow.addColorStop(1, "rgba(255, 193, 57, 0)");
    ctx.fillStyle = starGlow;
    ctx.fillRect(cx - 30, cy - 30, 60, 60);
    ctx.fillStyle = "#fff8c7";
    ctx.beginPath();
    ctx.arc(cx, cy, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(235, 244, 247, .85)";
    ctx.font = "600 10px var(--font-geist-mono)";
    ctx.fillText("SUN / EARTH", cx + 12, cy + 16);
    ctx.restore();

    if (hovered) {
      const hit = hitPoints.find((item) => item.star.tic_id === hovered.tic_id);
      if (hit) {
        const boxW = 190;
        const boxH = 67;
        const bx = Math.min(w - boxW - 12, hit.x + 16);
        const by = Math.max(12, Math.min(h - boxH - 12, hit.y - boxH / 2));
        ctx.fillStyle = "rgba(4, 15, 24, .94)";
        ctx.strokeStyle = statusMeta(hovered.status).color;
        ctx.lineWidth = 1;
        ctx.fillRect(bx, by, boxW, boxH);
        ctx.strokeRect(bx, by, boxW, boxH);
        ctx.fillStyle = statusMeta(hovered.status).color;
        ctx.font = "700 11px var(--font-geist-mono)";
        ctx.fillText(`TIC ${hovered.tic_id}`, bx + 10, by + 18);
        ctx.fillStyle = "#c8d9e0";
        ctx.font = "10px var(--font-geist-mono)";
        ctx.fillText(`${fmt(hovered.distance_pc, 1)} pc`, bx + 10, by + 36);
        ctx.fillText(statusMeta(hovered.status).short, bx + 10, by + 52);
      }
    }
  }, [
    highlightedSector,
    hovered,
    mode,
    pan,
    rotation,
    sectorFootprint,
    selected,
    stars,
    zoom,
  ]);

  useEffect(() => {
    draw();
    const observer = new ResizeObserver(draw);
    if (canvasRef.current) observer.observe(canvasRef.current);
    return () => observer.disconnect();
  }, [draw]);

  const locate = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    return [...pointsRef.current]
      .reverse()
      .find((point) => Math.hypot(point.x - x, point.y - y) <= point.r);
  };

  const changeZoom = (factor: number, anchor?: { x: number; y: number }) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const baseCx = mode === "sky" ? rect.width * 0.5 : rect.width * 0.51;
    const baseCy = mode === "sky" ? rect.height * 0.5 : rect.height * 0.49;
    const zoomAnchor = anchor || { x: baseCx, y: baseCy };
    setZoom((current) => {
      const next = Math.max(0.0001, Math.min(100_000, current * factor));
      const applied = next / current;
      setPan((currentPan) => ({
        x:
          zoomAnchor.x -
          baseCx -
          (zoomAnchor.x - baseCx - currentPan.x) * applied,
        y:
          zoomAnchor.y -
          baseCy -
          (zoomAnchor.y - baseCy - currentPan.y) * applied,
      }));
      return next;
    });
  };

  const resetView = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
    setRotation({ x: -0.36, y: -0.52 });
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const handleWheel = (event: WheelEvent) => {
      event.preventDefault();
      event.stopPropagation();
      const rect = canvas.getBoundingClientRect();
      changeZoom(Math.exp(-event.deltaY * 0.0015), {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      });
    };

    canvas.addEventListener("wheel", handleWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", handleWheel);
  }, [mode]);

  return (
    <div className="map-stage">
      <canvas ref={glCanvasRef} className="star-canvas-gl" aria-hidden="true" />
      <canvas
        ref={canvasRef}
        className="star-canvas"
        role="application"
        aria-label="Interactive spatial map of analyzed stars"
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          dragRef.current = {
            active: true,
            moved: false,
            panning: mode !== "3d" || event.shiftKey,
            x: event.clientX,
            y: event.clientY,
          };
        }}
        onPointerMove={(event) => {
          if (dragRef.current.active) {
            const dx = event.clientX - dragRef.current.x;
            const dy = event.clientY - dragRef.current.y;
            if (Math.abs(dx) + Math.abs(dy) > 2) dragRef.current.moved = true;
            dragRef.current.x = event.clientX;
            dragRef.current.y = event.clientY;
            if (dragRef.current.panning) {
              setPan((current) => ({ x: current.x + dx, y: current.y + dy }));
            } else {
              setRotation((current) => ({
                x: Math.max(-1.3, Math.min(1.3, current.x + dy * 0.005)),
                y: current.y + dx * 0.005,
              }));
            }
          } else {
            setHovered(locate(event)?.star || null);
          }
        }}
        onPointerUp={(event) => {
          if (!dragRef.current.moved) {
            const hit = locate(event);
            if (hit) onSelect(hit.star);
          }
          dragRef.current.active = false;
        }}
        onPointerLeave={() => {
          dragRef.current.active = false;
          setHovered(null);
        }}
        onDoubleClick={resetView}
      />
      <div className="axis-card">
        <span>
          <InfoTerm description={HELP.coordinateFrame}>COORDINATE FRAME</InfoTerm>
        </span>
        <strong>
          <InfoTerm
            description={
              mode === "3d" ? HELP.galacticXyz : mode === "sky" ? HELP.raDec : HELP.distanceRa
            }
          >
            {mode === "3d" ? "GALACTIC XYZ" : mode === "sky" ? "RA / DEC" : "DISTANCE / RA"}
          </InfoTerm>
        </strong>
        {mode === "3d" ? (
          <em>
            <InfoTerm description={HELP.galacticPlane}>DISK: GALACTIC PLANE · b=0°</InfoTerm>
          </em>
        ) : null}
        {highlightedSector !== null ? (
          <em>
            <InfoTerm description={HELP.sectorFootprint}>
              FOOTPRINT: SECTOR {highlightedSector} · {TESS_SECTOR_GEOMETRY.model}
            </InfoTerm>
          </em>
        ) : null}
        <em>{scaleInfo.location}</em>
        <em>
          <InfoTerm description={HELP.zoom}>
            {fmt(zoom, zoom < 0.01 ? 4 : zoom < 1 ? 2 : zoom < 100 ? 1 : 0)}× zoom
          </InfoTerm>
        </em>
      </div>
      <div className="map-nav" aria-label="Map zoom controls">
        <button type="button" onClick={() => changeZoom(2)} title="Zoom in">
          +
        </button>
        <button type="button" onClick={() => changeZoom(0.5)} title="Zoom out">
          −
        </button>
        <button type="button" onClick={resetView} title="Reset map view">
          FIT
        </button>
      </div>
      <div className="map-instructions">
        <span>{mode === "3d" ? "↻ Drag to orbit · Shift+drag to pan" : "↔ Drag to pan"}</span>
        <span>⊕ Scroll to zoom at cursor</span>
        <span>
          {highlightedSector !== null
            ? `□ Sector ${highlightedSector} camera sight lines`
            : mode === "3d"
              ? "◎ Rigid Galactic-plane grid"
              : "◎ Hover for info"}
        </span>
        <span>⌖ Click to select · Double-click to fit</span>
      </div>
      <div className="scale-bar">
        <span style={{ width: `${scaleInfo.width}px` }} />
        <InfoTerm description={HELP.scale}>{scaleInfo.label}</InfoTerm>
      </div>
    </div>
  );
}

export default function App() {
  const [survey, setSurvey] = useState<SurveyData | null>(null);
  const [ops, setOps] = useState<OpsData | null>(null);
  const [loadError, setLoadError] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(260708537);
  const [mode, setMode] = useState<ViewMode>("3d");
  const [search, setSearch] = useState("");
  const [statuses, setStatuses] = useState<Set<Status>>(new Set(ALL_STATUSES));
  const [distance, setDistance] = useState(150);
  const [maxTemp, setMaxTemp] = useState(10000);
  const [maxRadius, setMaxRadius] = useState(5);
  const [minSnr, setMinSnr] = useState(0);
  const [sector, setSector] = useState("all");
  const [now, setNow] = useState(Date.now());
  const [phaseCurve, setPhaseCurve] = useState<PhaseCurve | null>(null);
  const [phaseCurveState, setPhaseCurveState] = useState<
    "legacy" | "loading" | "ready" | "error"
  >("legacy");
  const loadSequence = useRef(0);
  const phaseCurveCache = useRef(new Map<number, PhaseCurve>());
  const starCache = useRef<Star[]>([]);
  const starRevision = useRef<string | null>(null);

  const loadSurvey = useCallback(async () => {
    const sequence = ++loadSequence.current;
    try {
      const [summaryResponse, opsResponse] = await Promise.all([
        fetch(`/api/summary?t=${Date.now()}`, { cache: "no-store" }),
        fetch(`/api/ops?t=${Date.now()}`, { cache: "no-store" }),
      ]);
      if (!summaryResponse.ok) {
        throw new Error(`Survey summary returned ${summaryResponse.status}`);
      }
      if (!opsResponse.ok) {
        throw new Error(`Operations status returned ${opsResponse.status}`);
      }
      const summary = (await summaryResponse.json()) as Omit<SurveyData, "stars">;
      const nextOps = (await opsResponse.json()) as OpsData;
      let stars = starCache.current;
      if (starRevision.current !== summary.data_revision || stars.length === 0) {
        const loadPage = async (page: number) => {
          const response = await fetch(`/api/stars?page=${page}&page_size=1000`, {
            cache: "no-store",
          });
          if (!response.ok) throw new Error(`Star page ${page} returned ${response.status}`);
          return (await response.json()) as StarPage;
        };
        const first = await loadPage(1);
        const remaining =
          first.pages > 1
            ? await Promise.all(
                Array.from({ length: first.pages - 1 }, (_, index) => loadPage(index + 2)),
              )
            : [];
        stars = [first, ...remaining]
          .sort((left, right) => left.page - right.page)
          .flatMap((page) => page.items);
        if (stars.length !== first.total) {
          throw new Error(`Star projection returned ${stars.length} of ${first.total} rows`);
        }
      }
      if (sequence !== loadSequence.current) return;
      starCache.current = stars;
      starRevision.current = summary.data_revision;
      const derivedSectors = Array.from(
        new Set(stars.flatMap((star) => star.sectors)),
      ).sort((left, right) => left - right);
      const observedSectors =
        summary.observed_sectors.length > 0 ? summary.observed_sectors : derivedSectors;
      const sectorCoverage =
        summary.sector_coverage.length > 0
          ? summary.sector_coverage
          : observedSectors.map((observedSector) => {
              const analyzed = stars.filter((star) =>
                star.sectors.includes(observedSector),
              ).length;
              return {
                sector: observedSector,
                state: "partial" as const,
                targeted_stars: analyzed,
                analyzed_stars: analyzed,
                progress_fraction: 1,
                active_campaign: null,
                updated_at_utc: null,
                scope: "Derived from current ledger star projections; target-plan completeness is unavailable.",
              };
            });
      const next: SurveyData = {
        ...summary,
        observed_sectors: observedSectors,
        sector_coverage: sectorCoverage,
        active_campaigns: summary.active_campaigns || [],
        stars,
      };
      setSurvey(next);
      setOps(nextOps);
      setLoadError("");
      setSelectedId((current) => {
        if (current && next.stars.some((star) => star.tic_id === current)) return current;
        return (
          next.stars.find((star) => star.status === "known_tce_rediscovery")?.tic_id ||
          next.stars[0]?.tic_id ||
          null
        );
      });
    } catch (error) {
      if (sequence !== loadSequence.current) return;
      setLoadError(error instanceof Error ? error.message : "Unable to load survey data");
    }
  }, []);

  useEffect(() => {
    loadSurvey();
    const poll = window.setInterval(loadSurvey, 5_000);
    const clock = window.setInterval(() => setNow(Date.now()), 10_000);
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") loadSurvey();
    };
    window.addEventListener("focus", loadSurvey);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(clock);
      window.removeEventListener("focus", loadSurvey);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [loadSurvey]);

  const filteredStars = useMemo(() => {
    if (!survey) return [];
    const query = search.trim().toLowerCase();
    return survey.stars.filter((star) => {
      if (!statuses.has(star.status)) return false;
      if (!star.distance_is_estimated && star.distance_pc > distance) return false;
      if (star.teff_k && star.teff_k > maxTemp) return false;
      if (star.stellar_radius_solar && star.stellar_radius_solar > maxRadius) return false;
      if ((star.snr || 0) < minSnr) return false;
      if (sector !== "all" && !star.sectors.includes(Number(sector))) return false;
      if (
        query &&
        !star.name.toLowerCase().includes(query) &&
        !String(star.tic_id).includes(query) &&
        !star.status_label.toLowerCase().includes(query)
      )
        return false;
      return true;
    });
  }, [distance, maxRadius, maxTemp, minSnr, search, sector, statuses, survey]);

  const selected = useMemo(
    () => survey?.stars.find((star) => star.tic_id === selectedId) || filteredStars[0] || null,
    [filteredStars, selectedId, survey],
  );

  useEffect(() => {
    if (!selected || !selected.phase_curve_available) {
      setPhaseCurve(null);
      setPhaseCurveState("legacy");
      return;
    }
    const cached = phaseCurveCache.current.get(selected.tic_id);
    if (cached) {
      setPhaseCurve(cached);
      setPhaseCurveState("ready");
      return;
    }

    const controller = new AbortController();
    setPhaseCurve(null);
    setPhaseCurveState("loading");
    const loadPhaseCurve = async () => {
      try {
        const response = await fetch(`/api/targets/${selected.tic_id}/phase-curve`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (response.status === 404) {
          setPhaseCurveState("legacy");
          return;
        }
        if (!response.ok) throw new Error(`Curve data returned ${response.status}`);
        const payload = (await response.json()) as { phase_curve: PhaseCurve };
        const curve = payload.phase_curve;
        if (
          !curve ||
          !Array.isArray(curve.phase) ||
          !Array.isArray(curve.median_residual_flux_ppm) ||
          !Array.isArray(curve.scatter_ppm) ||
          curve.phase.length === 0 ||
          curve.phase.length !== curve.median_residual_flux_ppm.length ||
          curve.phase.length !== curve.scatter_ppm.length
        ) {
          throw new Error("Curve data is incomplete");
        }
        phaseCurveCache.current.set(selected.tic_id, curve);
        setPhaseCurve(curve);
        setPhaseCurveState("ready");
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setPhaseCurve(null);
        setPhaseCurveState("error");
      }
    };
    void loadPhaseCurve();
    return () => controller.abort();
  }, [selected?.phase_curve_available, selected?.tic_id]);

  const submitSearch = (event: React.FormEvent) => {
    event.preventDefault();
    if (!survey) return;
    const query = search.trim().toLowerCase();
    const match = survey.stars.find(
      (star) =>
        star.name.toLowerCase().includes(query) ||
        String(star.tic_id).includes(query) ||
        star.status_label.toLowerCase().includes(query),
    );
    if (match) {
      setSelectedId(match.tic_id);
      setStatuses((current) => new Set([...current, match.status]));
    }
  };

  const toggleStatus = (status: Status) => {
    setStatuses((current) => {
      const next = new Set(current);
      if (next.has(status)) next.delete(status);
      else next.add(status);
      return next;
    });
  };

  const stats = survey?.stats || {};
  const coordinatorLive = ops?.live === true;
  const activeCampaigns = survey?.active_campaigns || [];
  const activeCampaign = coordinatorLive
    ? selectActiveCampaign(activeCampaigns)
    : undefined;
  const highlightedSector =
    sector !== "all"
      ? Number(sector)
      : activeCampaign?.sectors?.length === 1
        ? activeCampaign.sectors[0]
        : null;
  const highlightedSectorFootprint =
    highlightedSector === null
      ? null
      : TESS_SECTOR_GEOMETRY.sectors[String(highlightedSector)] || null;
  const campaignPerformance = activeCampaign?.runtime?.performance;
  const activeWorkflow = activeCampaign?.workflow || "batch_hunt";
  const activeWorkflowLabel =
    activeWorkflow === "context_vet"
      ? "context vetting"
      : activeWorkflow === "science_vet"
        ? "science vetting"
        : "overnight run";
  const analysisWorkerCapacity = activeCampaign?.runtime?.analysis_workers || 1;
  const downloadWorkerCapacity = activeCampaign?.runtime?.download_workers || 0;
  const activeAnalysisWorkers = activeCampaign?.runtime?.analyses_in_flight || 0;
  const activeDownloadWorkers = activeCampaign?.runtime?.downloads_in_flight || 0;
  const activeWorkerCount = activeAnalysisWorkers + activeDownloadWorkers;
  const workerSlotCount = analysisWorkerCapacity + downloadWorkerCapacity;
  const idleWorkerCount = Math.max(0, workerSlotCount - activeWorkerCount);
  const activeProgress = activeCampaign?.total_targets
    ? Math.min(100, (activeCampaign.completed_targets / activeCampaign.total_targets) * 100)
    : 0;
  const activePercent = activeCampaign?.total_targets
    ? Math.round(activeProgress)
    : 0;
  const sectorCoverage = survey?.sector_coverage || [];
  const sectorCoverageByNumber = new Map(
    sectorCoverage.map((coverage) => [coverage.sector, coverage]),
  );
  const maxSector = Math.max(
    105,
    ...(survey?.observed_sectors || [105]),
    ...sectorCoverage.map((coverage) => coverage.sector),
  );
  const representedSectorCount = sectorCoverage.filter(
    (coverage) => coverage.analyzed_stars > 0,
  ).length;
  const liveStatusCounts = survey?.status_counts || {};

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand">
          <strong>EXOHUNT</strong>
          <span>// LOCAL STELLAR SURVEY</span>
        </div>
        <form className="search" onSubmit={submitSearch}>
          <span aria-hidden="true">⌕</span>
          <input
            aria-label="Search TIC or target name"
            placeholder="Search TIC, label, or name…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </form>
        <nav className="view-modes" aria-label="View mode">
          {(
            [
              ["3d", "3D Space"],
              ["sky", "Sky Projection"],
              ["earth", "Earth View"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              className={mode === value ? "active" : ""}
              onClick={() => setMode(value)}
              type="button"
            >
              <InfoTerm
                description={
                  value === "3d"
                    ? HELP.threeD
                    : value === "sky"
                      ? HELP.skyProjection
                      : HELP.earthView
                }
                focusable={false}
              >
                {label}
              </InfoTerm>
            </button>
          ))}
        </nav>
        <div className={`freshness freshness-${ops?.liveness || "connecting"}`}>
          <i />
          {survey
            ? activeCampaign
              ? `${activeCampaign.name}: ${activeCampaign.completed_targets}/${activeCampaign.total_targets} · ${activeWorkerCount}/${workerSlotCount} active · updated ${relativeUpdate(activeCampaign.updated_at_utc)}`
              : coordinatorLive
                ? `Coordinator live · heartbeat ${Math.round(ops?.heartbeat_age_seconds || 0)}s ago`
                : ops?.liveness === "stale"
                  ? `Coordinator stale · heartbeat ${Math.round(ops.heartbeat_age_seconds || 0)}s ago`
                  : `Data updated ${relativeUpdate(survey.generated_at_utc)} · coordinator idle`
            : "Connecting…"}
        </div>
      </header>

      <section className="workspace">
        <aside className="filters panel">
          <div className="panel-title">
            <InfoTerm description={HELP.filters}>FILTERS</InfoTerm>
            <button
              type="button"
              onClick={() => {
                setStatuses(new Set(ALL_STATUSES));
                setDistance(150);
                setMaxTemp(10000);
                setMaxRadius(5);
                setMinSnr(0);
                setSector("all");
                setSearch("");
              }}
            >
              Reset all ↻
            </button>
          </div>
          <div className="filter-body">
            <h2>
              <InfoTerm description={HELP.statusFilters}>STATUS FILTERS</InfoTerm>
            </h2>
            <div className="status-list">
              {ALL_STATUSES.map((status) => (
                <label key={status}>
                  <input
                    type="checkbox"
                    checked={statuses.has(status)}
                    onChange={() => toggleStatus(status)}
                  />
                  <Marker status={status} small />
                  <InfoTerm description={STATUS_HELP[status]} focusable={false}>
                    {statusMeta(status).label}
                  </InfoTerm>
                  <b
                    key={`${status}-${liveStatusCounts[status] || 0}`}
                    className="live-count"
                  >
                    {fmtInteger(liveStatusCounts[status])}
                  </b>
                </label>
              ))}
            </div>
            <p className="status-scope-note">
              <InfoTerm description={HELP.noTransitDetected}>
                No category means planet-free. Every label describes only this search window and pipeline.
              </InfoTerm>
            </p>
            <RangeControl
              label="DISTANCE RANGE"
              description={HELP.distanceRange}
              value={distance}
              min={5}
              max={150}
              step={1}
              unit="pc"
              onChange={setDistance}
            />
            <RangeControl
              label="STELLAR TEMPERATURE"
              description={HELP.stellarTemperature}
              value={maxTemp}
              min={2500}
              max={10000}
              step={100}
              unit="K max"
              onChange={setMaxTemp}
            />
            <RangeControl
              label="STELLAR RADIUS"
              description={HELP.stellarRadius}
              value={maxRadius}
              min={0.1}
              max={5}
              step={0.1}
              unit="R☉ max"
              onChange={setMaxRadius}
            />
            <RangeControl
              label="MINIMUM S/N"
              description={HELP.minimumSnr}
              value={minSnr}
              min={0}
              max={30}
              step={0.5}
              unit=""
              onChange={setMinSnr}
            />
            <label className="select-field">
              <span>
                <InfoTerm description={HELP.tessSector} focusable={false}>
                  TESS SECTOR
                </InfoTerm>
              </span>
              <select value={sector} onChange={(event) => setSector(event.target.value)}>
                <option value="all">All observed sectors</option>
                {[...(survey?.observed_sectors || [])].reverse().map((value) => (
                  <option key={value} value={value}>
                    Sector {value}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </aside>

        <section className="map-column">
          {loadError ? (
            <div className="map-error">
              <strong>Survey data unavailable</strong>
              <span>{loadError}</span>
              <button type="button" onClick={loadSurvey}>
                Retry
              </button>
            </div>
          ) : (
            <StarMap
              stars={filteredStars}
              sectorFootprint={highlightedSectorFootprint}
              highlightedSector={highlightedSector}
              selected={selected}
              onSelect={(star) => setSelectedId(star.tic_id)}
              mode={mode}
            />
          )}
          <div className="map-foot">
            <InfoTerm description={HELP.targetsMapped}>
              <i className="dot cyan-dot" />
              {filteredStars.length} visible targets
            </InfoTerm>
            <InfoTerm description={HELP.coordinateFrame}>
              {mode === "3d"
                ? "TIC sky direction + measured/estimated distance"
                : "TIC celestial coordinates"}
            </InfoTerm>
            <InfoTerm description={HELP.earthView}>Earth-centered frame</InfoTerm>
          </div>
        </section>

        <aside className="target-panel panel">
          <div className="panel-title">
            <InfoTerm description="The star whose local search record is shown below.">
              SELECTED TARGET
            </InfoTerm>
            <span className="pin">⌖</span>
          </div>
          {selected ? (
            <div className="target-body">
              <div className="target-heading">
                <Marker status={selected.status} />
                <div>
                  <h1>
                    <InfoTerm description={HELP.tic}>TIC {selected.tic_id}</InfoTerm>
                  </h1>
                  <p>
                    <InfoTerm description={HELP.ra}>RA {fmt(selected.ra_deg, 2)}°</InfoTerm>{" "}
                    <b>•</b>{" "}
                    <InfoTerm description={HELP.dec}>Dec {fmt(selected.dec_deg, 2)}°</InfoTerm>
                  </p>
                </div>
              </div>
              <div className={`status-banner ${statusMeta(selected.status).className}`}>
                <InfoTerm description={STATUS_HELP[selected.status] ?? UNKNOWN_STATUS_HELP}>
                  {selected.status === "known_tce_rediscovery"
                    ? "REDISCOVERED / NOT A NEW PLANET"
                    : statusMeta(selected.status).label.toUpperCase()}
                </InfoTerm>
              </div>
              <dl className="target-data">
                <div>
                  <dt><InfoTerm description={HELP.distance}>Distance</InfoTerm></dt>
                  <dd>
                    {selected.distance_is_estimated ? "≈ " : ""}
                    {fmt(selected.distance_pc, 2)} pc
                  </dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.tessMagnitude}>TESS Magnitude</InfoTerm></dt>
                  <dd>{fmt(selected.tmag, 2)}</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.stellarRadiusValue}>Stellar Radius</InfoTerm></dt>
                  <dd>{fmt(selected.stellar_radius_solar, 3)} R☉</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.stellarTemperatureValue}>Stellar Temperature</InfoTerm></dt>
                  <dd>{fmt(selected.teff_k, 0)} K</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.observedSectors}>Observed Sectors</InfoTerm></dt>
                  <dd>{selected.sectors.join(", ") || "—"}</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.recoveredPeriod}>Recovered Period</InfoTerm></dt>
                  <dd>{fmt(selected.period_days, 6)} d</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.transitDepth}>Transit Depth</InfoTerm></dt>
                  <dd>{fmt(selected.depth_ppm, 0)} ppm</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.signalToNoise}>Signal-to-Noise</InfoTerm></dt>
                  <dd>{fmt(selected.snr, 2)}</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.followupPriority}>Follow-up Priority</InfoTerm></dt>
                  <dd>{selected.followup_priority ?? 0}/100</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.deeperVetting}>Deeper Vetting Tier</InfoTerm></dt>
                  <dd>{(selected.vetting_tier || "legacy_unmeasured").replaceAll("_", " ")}</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.redNoiseSnr}>Red-noise S/N</InfoTerm></dt>
                  <dd>{fmt(selected.red_noise_adjusted_snr, 2)}</dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.eventCoverage}>Event Coverage</InfoTerm></dt>
                  <dd>
                    {selected.event_coverage_fraction === null ||
                    selected.event_coverage_fraction === undefined
                      ? "Legacy / not measured"
                      : `${fmt(selected.event_coverage_fraction * 100, 0)}%`}
                  </dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.positiveEventFraction}>Positive-depth Events</InfoTerm></dt>
                  <dd>
                    {selected.positive_depth_event_fraction === null ||
                    selected.positive_depth_event_fraction === undefined
                      ? "Legacy / not measured"
                      : `${fmt(selected.positive_depth_event_fraction * 100, 0)}%`}
                  </dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.sensitivityProbe}>3-day Sensitivity Probe</InfoTerm></dt>
                  <dd>
                    {selected.sensitivity_3d_ppm
                      ? `${fmt(selected.sensitivity_3d_ppm, 0)} ppm`
                      : "Legacy / not measured"}
                  </dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.sensitivityProbe}>12-day Sensitivity Probe</InfoTerm></dt>
                  <dd>
                    {selected.sensitivity_12d_ppm
                      ? `${fmt(selected.sensitivity_12d_ppm, 0)} ppm`
                      : "Legacy / not measured"}
                  </dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.catalogueStatus}>Catalogue Status</InfoTerm></dt>
                  <dd className={statusMeta(selected.status).className}>
                    {selected.status_label}
                  </dd>
                </div>
                {selected.context_followup_lane && (
                  <div>
                    <dt>
                      <InfoTerm description="The workflow lane assigned after independent public catalog and metadata checks.">
                        Context Follow-up Lane
                      </InfoTerm>
                    </dt>
                    <dd>{selected.context_followup_lane.replaceAll("_", " ")}</dd>
                  </div>
                )}
                <div>
                  <dt><InfoTerm description={HELP.coordinateSource}>Coordinate Source</InfoTerm></dt>
                  <dd className="cyan">{selected.coordinate_source}</dd>
                </div>
              </dl>
              {(selected.deeper_vetting_flags || selected.recommended_data_sources) && (
                <section className="mini-section followup-section">
                  <h2>
                    <InfoTerm description={HELP.deeperVetting}>DEEPER FOLLOW-UP PLAN</InfoTerm>
                  </h2>
                  <p>
                    <strong>Automated flags:</strong>{" "}
                    {selected.deeper_vetting_flags || "No additional in-light-curve flags."}
                  </p>
                  <p>
                    <InfoTerm description={HELP.followupSources}>
                      <strong>Independent data:</strong>{" "}
                      {selected.recommended_data_sources || "Legacy result; availability not planned."}
                    </InfoTerm>
                  </p>
                </section>
              )}
              {selected.context_disposition && (
                <section className="mini-section followup-section">
                  <h2>
                    <InfoTerm description="Independent metadata checks against NASA planet records, official TESS TCEs, the TESS eclipsing-binary catalog, SIMBAD, Gaia DR3, MAST holdings, and nearby TIC/Gaia sources.">
                      AUTHORITATIVE CONTEXT CHECKS
                    </InfoTerm>
                  </h2>
                  <p>
                    <strong>Disposition:</strong>{" "}
                    {selected.context_disposition.replaceAll("_", " ")}
                  </p>
                  <p>
                    <strong>Source coverage:</strong>{" "}
                    {Object.entries(selected.context_source_states || {})
                      .map(([source, state]) => `${source.replaceAll("_", " ")}: ${state}`)
                      .join("; ") || "No saved context pass yet."}
                  </p>
                  <p>
                    <strong>Rule:</strong> Catalog absence never promotes a signal by itself.
                  </p>
                </section>
              )}
              {selected.common_mode_verdict && (
                <section className="mini-section followup-section">
                  <h2>
                    <InfoTerm description="Asks how many unrelated targets observed in the same campaign carry this same fitted ephemeris. Planets around unrelated stars do not share transit times; an observatory event makes hundreds of them share one.">
                      SHARED-EPHEMERIS SCREEN
                    </InfoTerm>
                  </h2>
                  <p>
                    <strong>Verdict:</strong>{" "}
                    {selected.common_mode_verdict.replaceAll("_", " ")}
                  </p>
                  <p>
                    <strong>Targets sharing this ephemeris:</strong>{" "}
                    {fmtInteger(selected.common_mode_shared_targets ?? undefined)}
                    {selected.common_mode_expected_targets !== null
                      ? ` (${fmt(selected.common_mode_expected_targets, 1)} expected by chance`
                      : ""}
                    {selected.common_mode_enrichment !== null
                      ? `, ${fmt(selected.common_mode_enrichment, 1)}× enrichment)`
                      : selected.common_mode_expected_targets !== null
                        ? ")"
                        : ""}
                  </p>
                  <p>
                    <strong>Spread:</strong>{" "}
                    {selected.common_mode_cameras_spanned !== null
                      ? `${selected.common_mode_cameras_spanned} camera(s)`
                      : "cameras not recorded"}
                    {selected.common_mode_sky_spread_deg !== null
                      ? ` over ${fmt(selected.common_mode_sky_spread_deg, 0)}° of sky`
                      : ""}
                  </p>
                  {(selected.spacecraft_harmonic ||
                    selected.duration_at_grid_rail ||
                    selected.period_at_search_ceiling) && (
                    <p>
                      <strong>Grid and orbit cautions:</strong>{" "}
                      {[
                        selected.spacecraft_harmonic
                          ? `period sits on the ${selected.spacecraft_harmonic} TESS-orbit ratio (${fmt(selected.spacecraft_harmonic_period_days, 2)} d)`
                          : null,
                        selected.period_at_search_ceiling
                          ? "period is pinned to the search ceiling"
                          : null,
                        selected.duration_at_grid_rail
                          ? "duration is pinned to the edge of the duration grid"
                          : null,
                      ]
                        .filter(Boolean)
                        .join("; ")}
                      . These are cautions, not disproof: a fit that reaches the
                      edge of its grid is a fit that wanted to leave it.
                    </p>
                  )}
                  <p>
                    <strong>Rule:</strong> A transit belongs to one star. Sector
                    coherence cannot clear a shared ephemeris, because an
                    observatory event repeats in every sector.
                  </p>
                </section>
              )}
              {selected.science_vetted && (
                <section className="mini-section followup-section">
                  <h2>
                    <InfoTerm description="Measured checks that use actual pixel and multi-sector photometry: where the lost light came from, and whether independently searched sectors reproduce the same fixed ephemeris.">
                      MEASURED SCIENCE CHECKS
                    </InfoTerm>
                  </h2>
                  <p>
                    <strong>Pixel localization:</strong>{" "}
                    {selected.science_on_target === null
                      ? "Not measured."
                      : `${
                          selected.science_on_target ? "On target" : "Off target"
                        }${
                          selected.science_centroid_offset_arcsec !== null
                            ? ` — centroid ${fmt(
                                selected.science_centroid_offset_arcsec,
                                0,
                              )}″ from the star`
                            : ""
                        }`}
                  </p>
                  <p>
                    <strong>Sector coherence:</strong>{" "}
                    {selected.science_sector_gate_passed === null
                      ? "Not measured."
                      : `${selected.science_supported_sector_count ?? 0} of ${
                          selected.science_sectors_tested ?? 0
                        } tested sectors support the ephemeris${
                          selected.science_supporting_sectors.length > 0
                            ? ` (${selected.science_supporting_sectors.join(", ")})`
                            : ""
                        }`}
                  </p>
                  <p>
                    <strong>Rule:</strong> One TESS pixel spans roughly 21″. Passing both
                    checks is a screening result, not a planet candidate.
                  </p>
                </section>
              )}
              <section className="mini-section">
                <h2>
                  <InfoTerm description={HELP.phaseFolded}>
                    PHASE-FOLDED SIGNAL <small>(ACTUAL BINNED TESS DATA)</small>
                  </InfoTerm>
                </h2>
                {phaseCurveState === "ready" && phaseCurve ? (
                  <>
                    <ActualPhaseCurve
                      curve={phaseCurve}
                      color={statusMeta(selected.status).color}
                    />
                    <div className="phase-labels">
                      <span>{fmt(phaseCurve.phase_min, 2)}</span>
                      <InfoTerm description={HELP.phase}>Phase</InfoTerm>
                      <span>+{fmt(phaseCurve.phase_max, 2)}</span>
                    </div>
                    <p className="phase-meta">
                      Residual flux (ppm) ·{" "}
                      {phaseCurve.measurements_in_range.toLocaleString()} measurements
                    </p>
                  </>
                ) : (
                  <div className="phase-unavailable" role="status">
                    {phaseCurveState === "loading"
                      ? "Loading actual curve…"
                      : phaseCurveState === "error"
                        ? "The actual curve could not be loaded."
                        : "No actual curve for this star — it was searched before the feature was added."}
                  </div>
                )}
              </section>
              <section className="mini-section orbit-section">
                <h2>
                  <InfoTerm description={HELP.orbitalDiagram}>
                    ORBITAL DIAGRAM <small>(NOT TO SCALE)</small>
                  </InfoTerm>
                </h2>
                <div className="orbit-wrap">
                  <div className="orbit">
                    <i className="orbit-star" />
                    <i className="orbit-planet" />
                  </div>
                  <dl>
                    <div>
                      <dt><InfoTerm description={HELP.radiusRatio}>Radius ratio</InfoTerm></dt>
                      <dd>
                        {selected.depth_ppm
                          ? fmt(Math.sqrt(selected.depth_ppm / 1_000_000), 3)
                          : "—"}
                      </dd>
                    </div>
                    <div>
                      <dt><InfoTerm description={HELP.eventsSeen}>Events seen</InfoTerm></dt>
                      <dd>{selected.observed_transits ?? "—"}</dd>
                    </div>
                    <div>
                      <dt><InfoTerm description={HELP.duration}>Duration</InfoTerm></dt>
                      <dd>{selected.duration_hours ? `${fmt(selected.duration_hours, 2)} h` : "—"}</dd>
                    </div>
                  </dl>
                </div>
              </section>
              {selected.notes && <p className="target-note">{selected.notes}</p>}
              <button
                className="target-action"
                type="button"
                onClick={() => navigator.clipboard?.writeText(`TIC ${selected.tic_id}`)}
              >
                Copy target identifier ↗
              </button>
            </div>
          ) : (
            <div className="target-empty">Select a star to inspect its search record.</div>
          )}
        </aside>
      </section>

      <footer className="bottom-grid">
        <section className="metrics-panel panel">
          <div className="panel-title">
            <InfoTerm description="Mapped-star outcomes plus cumulative validation and vetting performance. Hover each metric to see its scope.">
              SURVEY METRICS
            </InfoTerm>
            {activeCampaign ? (
              <div className="performance-head">
                <InfoTerm description={HELP.rollingThroughput}>
                  NOW {fmt(campaignPerformance?.rolling_stars_per_hour, 0)}/h
                </InfoTerm>
                <InfoTerm description={HELP.averageThroughput}>
                  AVG {fmt(campaignPerformance?.average_stars_per_hour, 0)}/h
                </InfoTerm>
                <InfoTerm description={HELP.estimatedTime}>
                  ETA {fmtDuration(campaignPerformance?.eta_hours)}
                </InfoTerm>
                <InfoTerm description={HELP.activeWorkers}>
                  WORK {activeWorkerCount}/{workerSlotCount}
                </InfoTerm>
              </div>
            ) : (
              <b>
                {coordinatorLive
                  ? "LIVE"
                  : ops?.liveness === "stale"
                    ? "STALE"
                    : "IDLE"}
              </b>
            )}
          </div>
          <div className="metric-row">
            <Metric
              label="Targets mapped"
              description={HELP.targetsMapped}
              value={fmtInteger(survey?.stars.length)}
            />
            <Metric
              label="No transit in window"
              description={HELP.noTransitDetected}
              value={fmtInteger(survey?.status_counts.no_transit_detected)}
              color="#55c6d8"
            />
            <Metric
              label="Signals screened out"
              description={HELP.screenedRejected}
              value={fmtInteger(survey?.status_counts.screened_rejected)}
              color="#8098a5"
            />
            <Metric
              label="Single-event leads"
              description={HELP.singleEventLeads}
              value={fmtInteger(survey?.status_counts.single_event_lead)}
              color="#ffd166"
            />
            <Metric
              label="Automated survivors"
              description={HELP.automatedSurvivors}
              value={fmtInteger(survey?.status_counts.automated_survivor)}
              color="#62e6a7"
            />
            <Metric
              label="Known EB recoveries"
              description="Signals whose TIC host and recovered period match an authoritative eclipsing-binary record. These are validation results, not planets."
              value={fmtInteger(survey?.status_counts.known_eb_rediscovery)}
              color="#ff9f43"
            />
            <Metric
              label="Unresolved after context"
              description="Signals not explained by the checked catalogs. They still require pixel localization, an independent light-curve reduction, repeat-epoch support, and human review."
              value={fmtInteger(
                survey?.status_counts.unresolved_transit_like_signal,
              )}
              color="#77ff9f"
            />
            <Metric
              label="Observatory systematics"
              description="Signals whose fitted ephemeris is shared by many unrelated stars observed at the same time, across multiple cameras. These were produced by the spacecraft, not by any star."
              value={fmtInteger(survey?.status_counts.common_mode_systematic)}
              color="#9ca3af"
            />
            <Metric
              label="Off-target light"
              description="Signals whose difference-image centroid falls more than one TESS pixel from the star. The dimming most likely belongs to a neighbouring source."
              value={fmtInteger(
                survey?.status_counts.pixel_offset_contamination,
              )}
              color="#38bdf8"
            />
            <Metric
              label="Single supporting sector"
              description="On-target signals that only the discovery sector supports. Independently searched sectors did not reproduce the fixed ephemeris."
              value={fmtInteger(survey?.status_counts.single_sector_unconfirmed)}
              color="#f5c542"
            />
            <Metric
              label="Science-vetted leads"
              description="On target and supported in more than one independently searched sector. This is the strongest screening state the pipeline produces and is still not a planet candidate."
              value={fmtInteger(survey?.status_counts.science_vetted_lead)}
              color="#34d399"
            />
            <Metric
              label="Retry needed"
              description={HELP.searchErrors}
              value={fmtInteger(survey?.status_counts.search_error)}
              color="#ff7b54"
            />
            <Metric
              label="New candidates"
              description={HELP.newCandidates}
              value={fmtInteger(survey?.status_counts.vetted_candidate)}
              color="#77ff9f"
            />
          </div>
          <InFlightPanel campaign={activeCampaign} />
          <div className="metric-meta">
            <InfoTerm description={HELP.coverage}>Display scale 0–150 pc</InfoTerm>
            {activeCampaign ? (
              <InfoTerm
                className="active-campaign"
                description={`${activeCampaign.completed_targets} of ${activeCampaign.total_targets} ${activeWorkflowLabel} targets have finished processing.`}
              >
                <i style={{ "--campaign-progress": `${activePercent}%` } as React.CSSProperties} />
                {activePercent}% {activeWorkflowLabel}
              </InfoTerm>
            ) : null}
            <InfoTerm description={HELP.sectorsRepresented}>
              {representedSectorCount} sectors represented
            </InfoTerm>
            <InfoTerm description={HELP.campaignRuns}>
              {Number(stats.campaign_runs_logged || 0)} campaign runs
            </InfoTerm>
            {activeCampaign ? (
              <InfoTerm description={HELP.activeWorkers}>
                {activeWorkflow === "batch_hunt" ? (
                  <>
                    {activeWorkerCount}/{workerSlotCount} active ·{" "}
                    {activeAnalysisWorkers}/{analysisWorkerCapacity} analyzing ·{" "}
                    {activeDownloadWorkers}/{downloadWorkerCapacity} downloading ·{" "}
                    {idleWorkerCount} idle ·{" "}
                    {activeCampaign.runtime?.downloaded_waiting || 0} staged
                  </>
                ) : (
                  <>
                    {activeAnalysisWorkers}/{analysisWorkerCapacity} vetting ·{" "}
                    {idleWorkerCount} idle ·{" "}
                    {fmtInteger(activeCampaign.runtime?.science_products_downloaded)} science products
                  </>
                )}
              </InfoTerm>
            ) : null}
            {activeCampaign ? (
              <InfoTerm description={HELP.vettingCoverage}>
                {activeWorkflow === "context_vet"
                  ? "Context vetted"
                  : activeWorkflow === "science_vet"
                    ? "Science vetted"
                    : "Deep vetting"}{" "}
                {fmtInteger(activeCampaign.runtime?.vetting_coverage?.measured_targets)}/
                {fmtInteger(activeCampaign.runtime?.vetting_coverage?.eligible_targets)}
                {activeCampaign.runtime?.vetting_coverage?.legacy_unmeasured_targets
                  ? ` · ${fmtInteger(
                      activeCampaign.runtime.vetting_coverage.legacy_unmeasured_targets,
                    )} legacy`
                  : ""}
              </InfoTerm>
            ) : null}
            {activeCampaign ? (
              <InfoTerm description={HELP.estimatedTime}>
                Estimated finish{" "}
                {campaignPerformance?.estimated_completion_utc
                  ? new Date(
                      campaignPerformance.estimated_completion_utc,
                    ).toLocaleTimeString([], {
                      hour: "numeric",
                      minute: "2-digit",
                    })
                  : "calculating"}
              </InfoTerm>
            ) : null}
            <InfoTerm description={HELP.validationRecoveries}>
              {fmtInteger(stats.known_planet_rediscoveries as number)} separate validation
              recoveries
            </InfoTerm>
            <InfoTerm description={HELP.polling}>
              {now ? "Polling every 5 seconds" : ""}
            </InfoTerm>
          </div>
        </section>

        <section className="timeline-panel panel">
          <div className="timeline-head">
            <div>
              <InfoTerm description={HELP.timeline}>
                TESS SECTOR TIMELINE / SURVEY COVERAGE
              </InfoTerm>
              <p>
                {activeCampaign
                  ? activeWorkflow === "batch_hunt"
                    ? `Sector ${[...(activeCampaign.sectors || [])].join(", ") || "—"} active • ${activeCampaign.completed_targets}/${activeCampaign.total_targets} targets • ${activePercent}% • updated ${relativeUpdate(activeCampaign.updated_at_utc)}`
                    : `${activeWorkflow === "context_vet" ? "Context metadata vetting" : "Focused science vetting"} active • ${activeCampaign.completed_targets}/${activeCampaign.total_targets} leads • ${activePercent}% • updated ${relativeUpdate(activeCampaign.updated_at_utc)}`
                  : "Hover for local analyzed/targeted coverage; this is not all stars in each TESS sector"}
              </p>
            </div>
            <div className="timeline-legend">
              <InfoTerm description={HELP.completedSector}>
                <i className="completed" /> Local plan complete
              </InfoTerm>
              <InfoTerm description={HELP.activeSector}>
                <i className="active" /> Active campaign
              </InfoTerm>
              <InfoTerm description={HELP.partialSector}>
                <i className="partial" /> Partial / inactive
              </InfoTerm>
              <InfoTerm description={HELP.noLocalTarget}>
                <i /> No local coverage
              </InfoTerm>
            </div>
          </div>
          {activeCampaign ? (
            <div
              className="timeline-progress"
              aria-label={`${activeCampaign.completed_targets} of ${activeCampaign.total_targets} ${activeWorkflowLabel} targets complete`}
              title={`${activeCampaign.completed_targets} of ${activeCampaign.total_targets} ${activeWorkflowLabel} targets complete`}
            >
              <i style={{ "--timeline-progress": `${activeProgress}%` } as React.CSSProperties} />
            </div>
          ) : null}
          <div
            className="sector-strip"
            style={{ gridTemplateColumns: `repeat(${maxSector}, minmax(2px, 1fr))` }}
          >
            {Array.from({ length: maxSector }, (_, index) => index + 1).map((value) => {
              const coverage = sectorCoverageByNumber.get(value);
              const state = coverage?.state || "unsearched";
              const progress = Math.max(
                0,
                Math.min(100, (coverage?.progress_fraction || 0) * 100),
              );
              const countLabel = coverage
                ? `${coverage.analyzed_stars}/${coverage.targeted_stars} local targets (${Math.round(progress)}%)`
                : "no local campaign targets";
              const stateLabel =
                state === "active"
                  ? "active campaign"
                  : state === "completed"
                    ? "local sector plan complete"
                    : state === "partial"
                      ? "partial inactive coverage"
                      : "no local coverage";
              const label = `TESS Sector ${value} — ${stateLabel}: ${countLabel}. Coverage is scoped to this project's target plans, not every star in the sector.`;
              return (
                <button
                  key={value}
                  aria-label={label}
                  className={`coverage-${state} ${Number(sector) === value ? "selected-sector" : ""}`.trim()}
                  style={{ "--sector-progress": `${progress}%` } as React.CSSProperties}
                  title={label}
                  type="button"
                  onClick={() => setSector(String(value))}
                >
                  <span>{value}</span>
                </button>
              );
            })}
          </div>
          <div className="sector-labels">
            <span>1</span>
            <span>25</span>
            <span>50</span>
            <span>75</span>
            <span>{maxSector}</span>
          </div>
        </section>
      </footer>
    </main>
  );
}

function RangeControl({
  label,
  description,
  value,
  min,
  max,
  step,
  unit,
  onChange,
}: {
  label: string;
  description: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit: string;
  onChange: (value: number) => void;
}) {
  const percent = ((value - min) / (max - min)) * 100;
  return (
    <label className="range-field">
      <span>
        <InfoTerm description={description} focusable={false}>{label}</InfoTerm>
        <b>
          {fmt(value, step < 1 ? 1 : 0)} {unit}
        </b>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        style={{ "--range-progress": `${percent}%` } as React.CSSProperties}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <div>
        <small>{min}</small>
        <small>{max}</small>
      </div>
    </label>
  );
}

function Metric({
  label,
  description,
  value,
  color = "#dce9ee",
}: {
  label: string;
  description: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="metric">
      <div className="metric-label">
        <InfoTerm description={description}>{label}</InfoTerm>
      </div>
      <strong style={{ color }}>{value}</strong>
    </div>
  );
}
