import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

type Status =
  | "searched"
  | "no_transit_detected"
  | "screened_rejected"
  | "single_event_lead"
  | "automated_survivor"
  | "search_error"
  | "rediscovery"
  | "known_tce_rediscovery"
  | "false_positive"
  | "vetted_candidate"
  | "confirmed_planet";

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

type ActiveCampaign = {
  name: string;
  state: "running" | "finalizing" | "retry_pending";
  target_list: string;
  sectors: number[];
  total_targets: number;
  completed_targets: number;
  counts: Record<string, number>;
  runtime: {
    analysis_workers?: number;
    download_workers?: number;
    prefetch_targets?: number;
    downloads_in_flight?: number;
    analyses_in_flight?: number;
    downloaded_waiting?: number;
    targets_remaining?: number;
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
  generated_at_utc: string;
  stats: Record<string, number | string | Record<string, number>>;
  status_counts: Record<string, number>;
  observed_sectors: number[];
  active_campaigns: ActiveCampaign[];
  stars: Star[];
};

type ViewMode = "3d" | "sky" | "earth";

const STATUS_META: Record<
  Status,
  { label: string; short: string; color: string; className: string }
> = {
  searched: {
    label: "Searched — Awaiting Classification",
    short: "Awaiting class",
    color: "#35d7e8",
    className: "cyan",
  },
  no_transit_detected: {
    label: "No Transit Detected in Search Window",
    short: "No transit found",
    color: "#55c6d8",
    className: "cyan",
  },
  screened_rejected: {
    label: "Strongest Signal Screened Out",
    short: "Signal screened",
    color: "#8098a5",
    className: "muted",
  },
  single_event_lead: {
    label: "Single-Event Lead — Longer Baseline Needed",
    short: "Single event",
    color: "#ffd166",
    className: "amber",
  },
  automated_survivor: {
    label: "Automated Survivor — Deeper Vetting Needed",
    short: "Needs vetting",
    color: "#62e6a7",
    className: "green",
  },
  search_error: {
    label: "Search Error — Retry Needed",
    short: "Retry needed",
    color: "#ff7b54",
    className: "red",
  },
  rediscovery: {
    label: "Mapped Planet Recovery",
    short: "Planet recovered",
    color: "#ffad20",
    className: "amber",
  },
  known_tce_rediscovery: {
    label: "TCE Rediscovery",
    short: "TCE recovered",
    color: "#bf7aff",
    className: "violet",
  },
  false_positive: {
    label: "Vetted False Positive",
    short: "False positive",
    color: "#ff563d",
    className: "red",
  },
  vetted_candidate: {
    label: "Vetted New Candidate",
    short: "Candidate",
    color: "#77ff9f",
    className: "green",
  },
  confirmed_planet: {
    label: "Confirmed Planet",
    short: "Confirmed",
    color: "#f8ffb1",
    className: "green",
  },
};

const ALL_STATUSES = Object.keys(STATUS_META) as Status[];

const STATUS_HELP: Record<Status, string> = {
  searched:
    "The search finished under an older result format that cannot be assigned one of the newer screening classes.",
  no_transit_detected:
    "No repeating transit-like signal reached the automated threshold in the searched TESS window. This does not prove the star has no planet.",
  screened_rejected:
    "The strongest repeating signal failed one or more automated plausibility checks. The rejection applies to that signal, not to every possible planet around the star.",
  single_event_lead:
    "One promising dimming event was present, but the observed time span was not long enough to establish a repeating orbit. Longer-baseline data should be checked.",
  automated_survivor:
    "A signal survived the automated gates and has been placed in the deeper follow-up queue. It is not yet a vetted candidate or discovery.",
  search_error:
    "This target did not finish successfully because data retrieval or analysis failed. It needs a retry and is not counted as a no-signal result.",
  rediscovery:
    "The search recovered a planet that was already known. This checks the pipeline; it is not a new discovery.",
  known_tce_rediscovery:
    "The signal matches an existing TESS threshold-crossing event, so TESS has already flagged it.",
  false_positive:
    "Follow-up checks indicate that the signal is probably not a transiting planet.",
  vetted_candidate:
    "A promising signal has passed the current checks, but it is still not a confirmed planet.",
  confirmed_planet:
    "Independent evidence has established this object as a planet.",
};

const HELP = {
  filters: "Controls that decide which analyzed stars are visible on the map.",
  statusFilters:
    "Show or hide mapped stars based on classification. These per-star totals refresh from the live campaign checkpoint every five seconds; they do not include aggregate validation benchmarks.",
  legend: "Explains what each map marker color and shape means.",
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
    "Targets processed after the deeper-vetting feature was added, compared with eligible completed targets. Legacy-unmeasured targets are retained without pretending those newer checks ran retroactively.",
  parallelWorkers:
    "The live coordinator runs several analysis workers while a bounded download queue stages upcoming stars. One coordinator remains responsible for the checkpoint and dashboard.",
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
  sectorsRepresented: "How many distinct TESS observing sectors appear in the local results.",
  campaignRuns: "Completed batches of stars recorded in the permanent survey ledger.",
  polling: "How often the browser asks the local server for new campaign data.",
  timeline: "All TESS observing sectors, with local coverage and the current campaign highlighted.",
  searchedSector: "At least one locally analyzed star has data from this sector.",
  activeSector:
    "This sector is being processed now. Its orange fill grows as more targets finish.",
  noLocalTarget: "No searched target in the local ledger currently uses this sector.",
  sectorFootprint:
    "A rigid outline around the full local star envelope for the highlighted TESS sector. In 3D it is a Galactic XYZ bounding volume; in projected views it encloses the mapped local targets. It is spatial context, not the exact four-camera detector footprint.",
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
      style={{ "--marker-color": STATUS_META[status].color } as React.CSSProperties}
      aria-hidden="true"
    >
      {status === "known_tce_rediscovery"
        ? "✦"
        : status === "rediscovery"
          ? "✶"
          : status === "single_event_lead"
            ? "1"
            : ""}
    </span>
  );
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
  sectorStars,
  highlightedSector,
  selected,
  onSelect,
  mode,
}: {
  stars: Star[];
  sectorStars: Star[];
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

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
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

    if (highlightedSector !== null && sectorStars.length) {
      ctx.save();
      ctx.strokeStyle = "rgba(255, 173, 32, .92)";
      ctx.fillStyle = "rgba(255, 173, 32, .08)";
      ctx.lineWidth = 1.35;
      ctx.setLineDash([7, 4]);
      let labelX = 18;
      let labelY = 24;

      if (mode === "3d") {
        const paddedBounds = (coordinate: (star: Star) => number) => {
          let low = Number.POSITIVE_INFINITY;
          let high = Number.NEGATIVE_INFINITY;
          for (const star of sectorStars) {
            const value = coordinate(star);
            if (!Number.isFinite(value)) continue;
            low = Math.min(low, value);
            high = Math.max(high, value);
          }
          if (!Number.isFinite(low) || !Number.isFinite(high)) return null;
            const padding = Math.max(2, (high - low) * 0.06);
            return [low - padding, high + padding] as const;
        };
        const xBounds = paddedBounds((star) => star.x);
        const yBounds = paddedBounds((star) => star.y);
        const zBounds = paddedBounds((star) => star.z);
        if (xBounds && yBounds && zBounds) {
          const [minX, maxX] = xBounds;
          const [minY, maxY] = yBounds;
          const [minZ, maxZ] = zBounds;
          const corners: Array<[number, number, number]> = [
            [minX, minY, minZ],
            [maxX, minY, minZ],
            [maxX, maxY, minZ],
            [minX, maxY, minZ],
            [minX, minY, maxZ],
            [maxX, minY, maxZ],
            [maxX, maxY, maxZ],
            [minX, maxY, maxZ],
          ];
          const projectedCorners = corners.map(([pointX, pointY, pointZ]) =>
            projectGalacticPoint(pointX, pointY, pointZ),
          );
          const edges = [
            [0, 1],
            [1, 2],
            [2, 3],
            [3, 0],
            [4, 5],
            [5, 6],
            [6, 7],
            [7, 4],
            [0, 4],
            [1, 5],
            [2, 6],
            [3, 7],
          ];
          for (const [start, end] of edges) {
            ctx.beginPath();
            ctx.moveTo(projectedCorners[start].x, projectedCorners[start].y);
            ctx.lineTo(projectedCorners[end].x, projectedCorners[end].y);
            ctx.stroke();
          }
          labelX = Math.max(
            12,
            Math.min(w - 190, Math.min(...projectedCorners.map((point) => point.x))),
          );
          labelY = Math.max(
            18,
            Math.min(h - 12, Math.min(...projectedCorners.map((point) => point.y)) - 7),
          );
        }
      } else {
        let minX = Number.POSITIVE_INFINITY;
        let maxX = Number.NEGATIVE_INFINITY;
        let minY = Number.POSITIVE_INFINITY;
        let maxY = Number.NEGATIVE_INFINITY;
        for (const star of sectorStars) {
          const point = project(star);
          minX = Math.min(minX, point.x);
          maxX = Math.max(maxX, point.x);
          minY = Math.min(minY, point.y);
          maxY = Math.max(maxY, point.y);
        }
        minX -= 8;
        maxX += 8;
        minY -= 8;
        maxY += 8;
        ctx.fillRect(minX, minY, maxX - minX, maxY - minY);
        ctx.strokeRect(minX, minY, maxX - minX, maxY - minY);
        labelX = Math.max(12, Math.min(w - 190, minX));
        labelY = Math.max(18, Math.min(h - 12, minY - 7));
      }
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(255, 202, 92, .96)";
      ctx.font = "700 10px var(--font-geist-mono)";
      ctx.fillText(`SECTOR ${highlightedSector} · LOCAL ENVELOPE`, labelX, labelY);
      ctx.restore();
    }

    const projected = stars
      .map((star) => ({ star, ...project(star) }))
      .sort((a, b) => a.depth - b.depth);
    const hitPoints: Array<{ star: Star; x: number; y: number; r: number }> = [];
    for (const point of projected) {
      const { star, x, y } = point;
      const meta = STATUS_META[star.status];
      const important = star.status !== "searched";
      const selectedPoint = selected?.tic_id === star.tic_id;
      const radius = selectedPoint ? 7 : important ? 4.4 : 2.1;
      if (x < -20 || x > w + 20 || y < -20 || y > h + 20) continue;
      ctx.save();
      ctx.globalAlpha = important ? 0.95 : 0.7;
      ctx.shadowColor = meta.color;
      ctx.shadowBlur = selectedPoint ? 18 : important ? 9 : 4;
      ctx.fillStyle = meta.color;
      ctx.strokeStyle = meta.color;
      if (star.status === "false_positive") {
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.arc(x, y, radius, 0, Math.PI * 2);
        ctx.stroke();
      } else {
        ctx.beginPath();
        ctx.arc(x, y, radius, 0, Math.PI * 2);
        ctx.fill();
      }
      if (star.status === "known_tce_rediscovery" || star.status === "rediscovery") {
        ctx.shadowBlur = 0;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(x, y, radius + 3, 0, Math.PI * 2);
        ctx.stroke();
        if (star.status === "known_tce_rediscovery") {
          ctx.beginPath();
          ctx.arc(x, y, radius + 6, 0, Math.PI * 2);
          ctx.stroke();
        }
      }
      if (selectedPoint) {
        ctx.setLineDash([3, 4]);
        ctx.globalAlpha = 0.58;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(x, y);
        ctx.stroke();
      }
      ctx.restore();
      hitPoints.push({ star, x, y, r: Math.max(8, radius + 4) });
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
        ctx.strokeStyle = STATUS_META[hovered.status].color;
        ctx.lineWidth = 1;
        ctx.fillRect(bx, by, boxW, boxH);
        ctx.strokeRect(bx, by, boxW, boxH);
        ctx.fillStyle = STATUS_META[hovered.status].color;
        ctx.font = "700 11px var(--font-geist-mono)";
        ctx.fillText(`TIC ${hovered.tic_id}`, bx + 10, by + 18);
        ctx.fillStyle = "#c8d9e0";
        ctx.font = "10px var(--font-geist-mono)";
        ctx.fillText(`${fmt(hovered.distance_pc, 1)} pc`, bx + 10, by + 36);
        ctx.fillText(STATUS_META[hovered.status].short, bx + 10, by + 52);
      }
    }
  }, [
    highlightedSector,
    hovered,
    mode,
    pan,
    rotation,
    sectorStars,
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
              OUTLINE: SECTOR {highlightedSector} LOCAL ENVELOPE
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
            ? `□ Sector ${highlightedSector} spatial envelope`
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

  const loadSurvey = useCallback(async () => {
    const sequence = ++loadSequence.current;
    try {
      const response = await fetch(`/data/survey.json?t=${Date.now()}`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`Survey data returned ${response.status}`);
      const next = (await response.json()) as SurveyData;
      if (sequence !== loadSequence.current) return;
      setSurvey(next);
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
  const activeCampaigns = survey?.active_campaigns || [];
  const activeCampaign = activeCampaigns[activeCampaigns.length - 1];
  const highlightedSector =
    sector !== "all"
      ? Number(sector)
      : activeCampaign?.sectors?.length === 1
        ? activeCampaign.sectors[0]
        : null;
  const highlightedSectorStars = useMemo(
    () =>
      highlightedSector === null || !survey
        ? []
        : survey.stars.filter((star) => star.sectors.includes(highlightedSector)),
    [highlightedSector, survey],
  );
  const campaignPerformance = activeCampaign?.runtime?.performance;
  const activeProgress = activeCampaign?.total_targets
    ? Math.min(100, (activeCampaign.completed_targets / activeCampaign.total_targets) * 100)
    : 0;
  const activePercent = activeCampaign?.total_targets
    ? Math.round(activeProgress)
    : 0;
  const maxSector = Math.max(105, ...(survey?.observed_sectors || [105]));
  const observed = new Set(survey?.observed_sectors || []);
  const activeSectors = new Set(activeCampaign?.sectors || []);
  const latestObservedSector = Math.max(0, ...(survey?.observed_sectors || []));
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
        <div className="freshness">
          <i />
          {survey
            ? activeCampaign
              ? `${activeCampaign.name}: ${activeCampaign.completed_targets}/${activeCampaign.total_targets} · ${activeCampaign.runtime?.analysis_workers || 1} workers · updated ${relativeUpdate(activeCampaign.updated_at_utc)}`
              : `Data updated ${relativeUpdate(survey.generated_at_utc)}`
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
                    {STATUS_META[status].label}
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
            <div className="legend">
              <h2>
                <InfoTerm description={HELP.legend}>LEGEND / MARKER STATUS</InfoTerm>
              </h2>
              {ALL_STATUSES.map((status) => (
                <div key={status}>
                  <Marker status={status} />
                  <InfoTerm description={STATUS_HELP[status]}>
                    {STATUS_META[status].label}
                  </InfoTerm>
                </div>
              ))}
              <p>Candidate styling is shown as a legend example only—not a claimed discovery.</p>
            </div>
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
              sectorStars={highlightedSectorStars}
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
              <div className={`status-banner ${STATUS_META[selected.status].className}`}>
                <InfoTerm description={STATUS_HELP[selected.status]}>
                  {selected.status === "known_tce_rediscovery"
                    ? "REDISCOVERED / NOT A NEW PLANET"
                    : STATUS_META[selected.status].label.toUpperCase()}
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
                  <dd className={STATUS_META[selected.status].className}>
                    {selected.status_label}
                  </dd>
                </div>
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
                      color={STATUS_META[selected.status].color}
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
              </div>
            ) : (
              <b>LIVE</b>
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
          <div className="metric-meta">
            <InfoTerm description={HELP.coverage}>Display scale 0–150 pc</InfoTerm>
            {activeCampaign ? (
              <InfoTerm
                className="active-campaign"
                description={`${activeCampaign.completed_targets} of ${activeCampaign.total_targets} campaign targets have finished processing.`}
              >
                <i style={{ "--campaign-progress": `${activePercent}%` } as React.CSSProperties} />
                {activePercent}% overnight run
              </InfoTerm>
            ) : null}
            <InfoTerm description={HELP.sectorsRepresented}>
              {survey?.observed_sectors.length || 0} sectors represented
            </InfoTerm>
            <InfoTerm description={HELP.campaignRuns}>
              {Number(stats.campaign_runs_logged || 0)} campaign runs
            </InfoTerm>
            {activeCampaign ? (
              <InfoTerm description={HELP.parallelWorkers}>
                {activeCampaign.runtime?.analysis_workers || 1} analysis workers ·{" "}
                {activeCampaign.runtime?.downloads_in_flight || 0} downloading ·{" "}
                {activeCampaign.runtime?.downloaded_waiting || 0} staged
              </InfoTerm>
            ) : null}
            {activeCampaign ? (
              <InfoTerm description={HELP.vettingCoverage}>
                Deep vetting{" "}
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
                  ? `Sector ${[...(activeCampaign.sectors || [])].join(", ") || "—"} active • ${activeCampaign.completed_targets}/${activeCampaign.total_targets} targets • ${activePercent}% • updated ${relativeUpdate(activeCampaign.updated_at_utc)}`
                  : "Hover for sector number • cyan sectors contain searched targets"}
              </p>
            </div>
            <div className="timeline-legend">
              <InfoTerm description={HELP.searchedSector}>
                <i className="observed" /> Searched
              </InfoTerm>
              <InfoTerm description={HELP.activeSector}>
                <i className="active" /> Active campaign
              </InfoTerm>
              <InfoTerm description={HELP.noLocalTarget}>
                <i /> No local target
              </InfoTerm>
            </div>
          </div>
          {activeCampaign ? (
            <div
              className="timeline-progress"
              aria-label={`${activeCampaign.completed_targets} of ${activeCampaign.total_targets} targets complete`}
              title={`${activeCampaign.completed_targets} of ${activeCampaign.total_targets} targets complete`}
            >
              <i style={{ "--timeline-progress": `${activeProgress}%` } as React.CSSProperties} />
            </div>
          ) : null}
          <div
            className="sector-strip"
            style={{ gridTemplateColumns: `repeat(${maxSector}, minmax(2px, 1fr))` }}
          >
            {Array.from({ length: maxSector }, (_, index) => index + 1).map((value) => {
              const isActive = activeSectors.has(value);
              const label = isActive
                ? `TESS Sector ${value} — active: ${activeCampaign?.completed_targets}/${activeCampaign?.total_targets} targets (${activePercent}%)`
                : `TESS Sector ${value}${observed.has(value) ? " — represented" : " — no local target"}`;
              return (
                <button
                  key={value}
                  aria-label={label}
                  className={`${isActive
                    ? "active-sector"
                    : observed.has(value)
                      ? "observed"
                      : value === latestObservedSector
                        ? "latest"
                        : ""} ${Number(sector) === value ? "selected-sector" : ""}`.trim()}
                  style={
                    isActive
                      ? ({ "--sector-progress": `${activeProgress}%` } as React.CSSProperties)
                      : undefined
                  }
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
