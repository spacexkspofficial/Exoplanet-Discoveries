# Brief for Fable: master overhaul plan for an autonomous exoplanet discovery system

Copy everything below the line into Fable, working in
`C:\Users\alexa\OneDrive\Desktop\Codex\Exoplanet Discoveries`.

---

You are being asked to produce a **master overhaul plan** for a working but
underperforming TESS transit-search pipeline. Read the repository first. Do not
start implementing. The deliverable is a plan I will review before any code is
written.

## The goal, stated plainly

**Discover exoplanets.** Not to build a nice dashboard, not to accumulate
statistics — to find real planets that nobody has catalogued, and to produce
evidence packets strong enough that an experienced observer would take them
seriously and put them on ExoFOP.

The system must run **24/7, unattended, on one Windows desktop**, working through
stars continuously: cheap wide screening first, then progressively more
expensive and more decisive tests applied only to what survives. Think of it as a
funnel that never stops running, where each tier costs more per star than the
last and each tier is allowed to kill a candidate.

I want this to draw on **the whole of public astronomy** — every archive, every
catalog, every cross-match, every published vetting technique that can be
automated. If NASA, MIT, or a research group has published a method or a data
product that would sharpen a decision, I want it considered.

## Where the project actually stands

Be under no illusions: **the system has searched 12,168 stars and produced zero
planet candidates.** That is not necessarily wrong — most stars have no
detectable transiting planet — but the reasons for the zero are not all
astrophysical, and that is the problem.

Current dashboard state:

```
5,615  observatory systematic (shared ephemeris)
3,837  strongest signal screened out
1,145  crowding / contamination review
  541  automated survivor
  472  known variable star review
  169  public-catalog coverage gap
   99  no transit detected
   93  known binary host - residual review
   14  vetted false positive
    0  vetted new candidate
    0  confirmed planet
```

### What has been learned the hard way — do not re-derive this

1. **Roughly half of every campaign was instrumental, not astrophysical.**
   Across 12,038 screened targets, 5,615 (46.6%) carry a fitted ephemeris shared
   by many unrelated stars observed at the same time. TESS orbits Earth every
   13.70 days; survivor periods piled up at 6.85 d (half that) and at the search
   ceiling, and 92% of the ~1,000 survivors near 6.85 d shared just two transit
   epochs. Unrelated stars do not share transit times.

2. **The original campaigns used the wrong photometry.** They ran on locally
   extracted TESScut cutouts (aperture sum + local background) when SPOC
   120-second mission light curves existed for **100% of a 40-target random
   sample**. Example: TIC 101170045 showed a 14,355 ppm signal at S/N 181 in
   TESScut and 37 ppm at S/N 5.5 in SPOC. There was nothing there.

3. **Switching to SPOC helped but did not fix it.** On a partial re-run, the
   common-mode flagged fraction fell 46.6% → 15.1%, but survivors still stacked
   at the *same* epochs (BTJD 4074.4 and 4080.8). The residual source was our
   own detrending: a Savitzky-Golay pass splitting at every ~10-minute gap and
   feeding unsupported edge estimates into BLS.

4. **An edge-safe detrending pass is now in place** (`src/exohunt/detrending.py`)
   — segment at real interruptions, discard half a window at each segment edge.
   Measured on 14 real light curves, 11 of 14 artifact detections moved off the
   artifact epochs and their S/N collapsed below the detection gate. Cost: 33%
   of cadences discarded, charged against long-period sensitivity. **This is
   almost certainly not the final answer** — hard-masking a full half-window
   around a 3.8-hour gap is heavier than the missing support warrants.

5. **Both signals that survived every gate were vetted out by hand.**
   TIC 181014443 is real and reproducible to 2 seconds across 7 sectors and 7
   years — and is an eclipsing binary (215 ppm secondary at 5.9σ against a
   1080 ppm primary). Single-sector data had measured that same secondary at
   2.3σ and passed it. TIC 188241769 simply does not reproduce in SPOC.

The lesson running through all of it: **this pipeline's failure mode is
confidently reporting instrument and reduction artifacts as astrophysics.** Any
overhaul must treat that as the primary risk.

## What exists today

- **Search core**: `astropy` BoxLeastSquares over a fixed duration grid, with
  automated gates (S/N ≥ 7.1, ≥2 transits, odd/even, secondary eclipse, duty
  cycle, depth) in `src/exohunt/detection.py` and `cli.py`.
- **Tiering, in embryo**: screen → metadata context vet → measured science vet
  (pixel centroid + multi-sector coherence) → population common-mode screen →
  human outcome. Statuses resolve through an explicit evidence-stage model
  (`src/exohunt/statuses.py`, `status_registry.json`).
- **Catalog work**: NASA Exoplanet Archive (TOI + confirmed), TIC, Gaia DR2/DR3
  cross-match, MAST holdings, TESS EB catalog, SIMBAD, official TESS TCEs
  (`src/exohunt/context.py`, `catalogs.py`, `evidence.py`, `tce.py`).
- **Population screen**: `src/exohunt/commonmode.py` — shared-ephemeris detection
  with detector and sky-spread corroboration.
- **Campaign runner**: checkpointed, resumable, storage-bounded, two-stage
  download/analyse pipeline in `cli.py`.
- **Dashboard**: React 3D star map on a local FastAPI server, 127.0.0.1 only.
- 114 tests pass with a bare `pytest`.

### Known architectural debt — see `HANDOFF.md` §6

`cli.py` is 4,439 lines and 85 functions (`_run_batch_hunt` alone is 508). The
survey payload is a 27 MB JSON monolith the browser refetches every 5 seconds.
Three different checkpoint schemas describe the same concept. Science thresholds
are bare literals scattered across the code (`7.1` appears at six sites).
`scripts/run_science_followup.py` is an 890-line parallel implementation of
machinery that already exists. Sections 6.1, 6.2, and 6.7 are done; 6.3 through
6.9 are not.

**The dashboard looks good but assume none of its wiring is sacred.** Keep the
visual language if it serves; redesign the data flow freely.

## Environment and constraints

- Windows 11, single desktop, 32 logical cores, no cloud, no cluster.
- Python 3.12 in `.venv`; Node for the dashboard; `pip install -e ".[dev]"`.
- Project lives inside a **OneDrive-synced folder** — this has already caused
  file-lock failures. Anything write-heavy should account for it.
- Hard ceilings currently enforced: 20 GB workspace, 10 GB download cache.
  Propose different numbers if the science needs them, but the machine is also
  someone's daily driver — CPU and disk politeness matters.
- Dashboard binds 127.0.0.1 only, by deliberate design. Keep it local.
- Runs must be **resumable and idempotent**. Power cuts, reboots, and archive
  outages are normal, not exceptional.

## What I want from you

A **master overhaul plan**, written for a competent implementer, covering:

### 1. Scientific architecture — the tiered funnel

Design the full sequence of tiers, from a cheap wide scan through to a
submission-ready candidate packet. For each tier specify: what it costs per
star, what data it needs, what decision it is allowed to make, what it can never
conclude, and the quantitative gate for promotion or rejection. I have in mind
something like "5,000 stars/day cheap, then progressively narrower" — tell me
whether that shape is right and what the real numbers should be.

Be explicit about **what kills a candidate at each tier and why**, and about
which rejections are reversible.

### 2. Photometry and detrending

What should we actually be searching? Evaluate the available reductions (SPOC,
TESS-SPOC, QLP, TGLC, CDIPS, TASOC, eleanor, PATHOS…) and say which to prefer
for which stars and why. Assess whether our Savitzky-Golay approach should be
replaced outright — consider biweight/notch filtering, GP-based detrending,
pixel-level decorrelation, cotrending basis vectors, and the published
gap-handling approaches. The current edge-guard costs 33% of cadences; I would
like that back if it can be had honestly.

### 3. Detection

Is BLS the right search? Assess alternatives and complements (TLS with realistic
limb-darkened templates, harmonic/alias handling, monotransit and single-event
searches, TTV-tolerant searches). Address the grid-rail problem: fits currently
pin to the edges of the period and duration grids and those fits are usually
junk.

### 4. Vetting and catalog cross-matching — go deep here

This is where I most want ambition. Design the cross-match strategy across
everything publicly available: TIC, Gaia DR3 (including RUWE, astrometric excess
noise, non-single-star tables, variability), SIMBAD, VSX, the TESS EB catalog,
ExoFOP, MAST TCE/DV products, Kepler/K2, ground surveys (ZTF, ASAS-SN, WASP,
HATNet, NGTS), and anything else that earns its place. Specify what each source
can and cannot settle, how to handle disagreement between sources, and how to
avoid the trap of treating "absent from a catalog" as evidence of novelty.

Include the standard automated vetting products and statistical validation
approaches (difference-image centroids, per-sector coherence, odd/even,
secondaries, ephemeris matching, and tools of the TRICERATOPS / DAVE /
LEO-vetter family). Say what a defensible "validated candidate" claim requires.

### 5. Completeness and false-alarm calibration

We currently have essentially no measured sensitivity and no measured
false-alarm rate. Design injection-recovery at survey scale and an inverted-flux
(or equivalent) null test, and make both routine rather than occasional. A
discovery claim without a completeness curve is not publishable.

### 6. Target selection

What sample maximises the chance of finding something genuinely new, given that
SPOC and QLP have already searched the bright stars with better tooling than
ours? Be honest if the answer is that certain lanes are hopeless. Candidate
directions worth assessing: monotransits and long-period single events, EB
residuals and circumbinary searches, faint or crowded fields, multi-sector
stitching beyond single-sector period limits.

### 7. Software architecture

How the codebase should be restructured to support the above, addressing
`HANDOFF.md` §6.3–6.9. Include the data model for tier state, how a star's
history is stored, and how a 24/7 scheduler orchestrates tiers with different
costs and cadences. Assume long-running autonomous operation with restarts.

### 8. Dashboard and observability

What an operator actually needs to see for a system running unattended for
weeks: funnel state, throughput, per-tier yield, systematics health, storage,
and the queue of things needing human judgement. Propose the data flow, not just
the visuals.

### 9. Sequenced roadmap

Ordered phases, each with entry criteria, exit criteria, and the measurement
that says it worked. Call out what must happen before any result can be trusted
again. Flag anything that risks silently changing scientific results, and say
how to prove it did not.

## Ground rules

- **Scientific honesty is the point.** This project's whole history is a
  cautionary tale about confident wrong answers. Never let a tier conclude more
  than its evidence supports. "Absent from a catalog" is not novelty. Surviving
  a screen is not a detection. A screening result is not a candidate.
- Prefer measurement to assertion. Where you claim a technique will help, say
  what number would demonstrate it.
- Say plainly where you are uncertain, and where the honest answer is that a
  whole lane is not worth pursuing.
- Assume the implementer will follow your plan literally. Ambiguity becomes bugs.
- Behaviour-preserving refactors must be provable: characterisation tests first,
  existing tests stay green rather than being edited to match.

## Read these first

- `HANDOFF.md` — full history, the systematics evidence, and the §6 refactor
  mandate with file:line citations.
- `REFACTOR_REVIEW.md` — the most recent code-review checkpoint.
- `SURVIVOR_VETTING.md` — current vetting lanes and their definitions.
- `DETECTION_LIMITS.md` — what this workflow can and cannot detect.
- `VALIDATION.md` — known-planet recovery results.
- `src/exohunt/` — `detection.py`, `detrending.py`, `commonmode.py`,
  `context.py`, `statuses.py`, and the `cli.py` monolith.
- `results/campaign/` — real outputs, including the contaminated TESScut
  campaigns and the partial SPOC re-run.

Ask me questions if the brief is ambiguous. I would rather answer five questions
now than review a plan built on a wrong assumption.
