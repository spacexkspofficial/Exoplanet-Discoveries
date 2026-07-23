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
  x: number;
  y: number;
  z: number;
};

type ActiveCampaign = {
  name: string;
  state: "running" | "finalizing";
  target_list: string;
  sectors: number[];
  total_targets: number;
  completed_targets: number;
  counts: Record<string, number>;
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
    label: "Searched — No Vetted Signal",
    short: "No signal",
    color: "#35d7e8",
    className: "cyan",
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
    "The automated search finished, but no signal has passed the full human-vetting process.",
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
    "Places stars in a rotatable Galactic coordinate frame using their sky position and estimated distance.",
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
  distance: "Estimated distance from Earth, shown in parsecs. One parsec is about 3.26 light-years.",
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
    "Many repeating cycles are stacked on top of one another so a recurring dip is easier to see.",
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
    "The search ran, but no signal passed the current automated and human-vetting gates. This does not mean the star has no planet: a planet can be non-transiting, too weak, outside the searched period range, hidden in a data gap, or missed by this pipeline.",
  planetRecoveries:
    "Mapped survey stars whose search recovered an already-known planet. This uses the same per-star classification and live count as the status filter.",
  validationRecoveries:
    "Known planets recovered by the separate validation benchmark suite. This measures pipeline performance and is deliberately kept separate from mapped-star classifications.",
  tceRecoveries: "Signals that match existing TESS threshold-crossing events.",
  falsePositives: "Signals rejected after additional vetting because they are probably not planets.",
  newCandidates: "Signals that passed the defined vetting steps but are not confirmed planets.",
  coverage: "The distance span currently summarized by the survey metrics.",
  sectorsRepresented: "How many distinct TESS observing sectors appear in the local results.",
  campaignRuns: "Completed batches of stars recorded in the permanent survey ledger.",
  polling: "How often the browser asks the local server for new campaign data.",
  timeline: "All TESS observing sectors, with local coverage and the current campaign highlighted.",
  searchedSector: "At least one locally analyzed star has data from this sector.",
  activeSector:
    "This sector is being processed now. Its orange fill grows as more targets finish.",
  noLocalTarget: "No searched target in the local ledger currently uses this sector.",
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
      {status === "known_tce_rediscovery" ? "✦" : status === "rediscovery" ? "✶" : ""}
    </span>
  );
}

function PhaseCurve({ star }: { star: Star }) {
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
    ctx.strokeStyle = "rgba(88, 129, 151, .22)";
    ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) {
      ctx.beginPath();
      ctx.moveTo(0, (h * i) / 4);
      ctx.lineTo(w, (h * i) / 4);
      ctx.stroke();
    }
    const depth = Math.min(0.03, Math.max(0.002, (star.depth_ppm || 1800) / 1_000_000));
    const duration = Math.min(0.1, Math.max(0.018, (star.duration_hours || 2) / 48));
    const color = STATUS_META[star.status].color;
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (let i = 0; i <= 140; i++) {
      const phase = (i / 140 - 0.5) * 0.24;
      const dip = depth * Math.exp(-Math.pow(phase / duration, 4));
      const y = 16 + (dip / depth) * (h - 40);
      const x = (i / 140) * w;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    for (let i = 0; i < 75; i++) {
      const phase = (i / 74 - 0.5) * 0.24;
      const dip = depth * Math.exp(-Math.pow(phase / duration, 4));
      const jitter = Math.sin(i * 12.91 + star.tic_id) * 3.4;
      const y = 16 + (dip / depth) * (h - 40) + jitter;
      const x = (i / 74) * w;
      ctx.globalAlpha = 0.72;
      ctx.beginPath();
      ctx.arc(x, y, 1.35, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }, [star]);
  return <canvas ref={canvasRef} className="phase-canvas" aria-label="Phase-folded transit visualization" />;
}

function StarMap({
  stars,
  selected,
  onSelect,
  mode,
}: {
  stars: Star[];
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
  }, [hovered, mode, pan, rotation, selected, stars, zoom]);

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
        onWheel={(event) => {
          event.preventDefault();
          const rect = event.currentTarget.getBoundingClientRect();
          changeZoom(Math.exp(-event.deltaY * 0.0015), {
            x: event.clientX - rect.left,
            y: event.clientY - rect.top,
          });
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
        <span>{mode === "3d" ? "◎ Rigid Galactic-plane grid" : "◎ Hover for info"}</span>
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

  const loadSurvey = useCallback(async () => {
    try {
      const response = await fetch(`/data/survey.json?t=${Date.now()}`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`Survey data returned ${response.status}`);
      const next = (await response.json()) as SurveyData;
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
      if (star.distance_pc > distance) return false;
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
              ? `${activeCampaign.name}: ${activeCampaign.completed_targets}/${activeCampaign.total_targets} · updated ${relativeUpdate(activeCampaign.updated_at_utc)}`
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
              <InfoTerm description={HELP.noVettedSignal}>
                Mapped-star outcomes only. “No vetted signal” does not mean planet-free.
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
              {mode === "3d" ? "Real TIC distance + sky position" : "TIC celestial coordinates"}
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
                  <dd>{fmt(selected.distance_pc, 2)} pc</dd>
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
                  <dt><InfoTerm description={HELP.catalogueStatus}>Catalogue Status</InfoTerm></dt>
                  <dd className={STATUS_META[selected.status].className}>
                    {selected.status_label}
                  </dd>
                </div>
                <div>
                  <dt><InfoTerm description={HELP.coordinateSource}>Coordinate Source</InfoTerm></dt>
                  <dd className="cyan">TESS Input Catalog</dd>
                </div>
              </dl>
              <section className="mini-section">
                <h2>
                  <InfoTerm description={HELP.phaseFolded}>PHASE-FOLDED SIGNAL ⓘ</InfoTerm>
                </h2>
                <PhaseCurve star={selected} />
                <div className="phase-labels">
                  <span>−0.10</span>
                  <InfoTerm description={HELP.phase}>Phase</InfoTerm>
                  <span>+0.10</span>
                </div>
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
            <b>
              {activeCampaign
                ? `RUNNING ${activeCampaign.completed_targets}/${activeCampaign.total_targets}`
                : "LIVE"}
            </b>
          </div>
          <div className="metric-row">
            <Metric
              label="Targets mapped"
              description={HELP.targetsMapped}
              value={fmtInteger(survey?.stars.length)}
            />
            <Metric
              label="No vetted signal"
              description={HELP.noVettedSignal}
              value={fmtInteger(survey?.status_counts.searched)}
              color="#35d7e8"
            />
            <Metric
              label="Mapped planet recoveries"
              description={HELP.planetRecoveries}
              value={fmtInteger(survey?.status_counts.rediscovery)}
              color="#ffad20"
            />
            <Metric
              label="TCE rediscoveries"
              description={HELP.tceRecoveries}
              value={fmtInteger(stats.known_tce_rediscoveries as number)}
              color="#bf7aff"
            />
            <Metric
              label="False positives"
              description={HELP.falsePositives}
              value={fmtInteger(stats.false_positives_after_vetting as number)}
              color="#ff563d"
            />
            <Metric
              label="New candidates"
              description={HELP.newCandidates}
              value={fmtInteger(stats.vetted_new_candidates as number)}
              color="#77ff9f"
            />
          </div>
          <div className="metric-meta">
            <InfoTerm description={HELP.coverage}>Coverage 0–150 pc</InfoTerm>
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
                  className={
                    isActive
                      ? "active-sector"
                      : observed.has(value)
                        ? "observed"
                        : value === latestObservedSector
                          ? "latest"
                          : ""
                  }
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
