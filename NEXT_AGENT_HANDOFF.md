# P4 handoff — 2026-08-06

Supersedes the 2026-08-05 reboot handoff, which described a shutdown state
that no longer applies.

## What this session did

Built P4's vetting layer end to end and ran the parts that could be run.
18 commits on `codex/p2-catalog-matching`, `bc05dba..9e5bb23`, worktree clean.
**465 tests pass** from a clean checkout (327 at session start).

Nothing here has changed any star's status. Every P4 evidence row is written
**non-voting and with no verdict**, so `rebuild_star_state` skips it — see the
open decision on precedence below.

## Where P4 stands against its own exit

The plan's exit is "regression suite green; ≥80% of the backlog resolves into
a terminal or review lane with full evidence chains; TIC 234994474 carries a
real verdict."

| | |
|---|---|
| Known-object regression suite | **Green**, 502 cases, and measured against three deliberate breaks (34 failures each) |
| Backlog resolution | **98.1%** (1,337/1,363) — but read correction 43 before quoting it |
| TIC 234994474 | **Not addressed.** It never entered the backlog cohort; it needs the promised multi-sector QLP run |

**P4's exit is not met.** The 98.1% passes on catalog coverage alone: 94% of
it is `unresolved_transit_like_signal`, meaning every declared source was
checked and none explains the signal. That is a filed lead, not an adjudicated
one. The vetting depth the phase is named for is built and tested, but only
pixel vetting has been run against real data.

## Built and committed

- **Snapshots** (`snapshots.py`): 8 sources fetched and content-hashed —
  nasa_toi 8,113 · nasa_ps 6,336 · tess_eb 4,584 · gaia_dr3 8,453 · vsx 252 ·
  gaia_nss_sb1 79 · asassn_variables 61 · gaia_nss_eb 1.
- **Identity graph** (`identity.py`, ledger schema v2): 1,363 canonical nodes,
  9,847 ranked counterpart edges. **616 stars (45.2%) have more than one
  plausible Gaia counterpart inside one TESS pixel.**
- **Adjudication** (`adjudicate.py`): §4.3 period **and** epoch matching.
- **Pixel vetting v2** (`pixel.py`) — **run on real pixels**, below.
- **T7 gate** (`crossreduction.py`), **T8 fit** (`transitfit.py`),
  **packet assembly** (`packet.py`), **review queue** (`/api/review-queue`),
  **vetting panel** (`/api/vetting`).
- `packet_ready_for_review` added as the 24th status (Appendix C additive).

## Real measurements worth carrying forward

**Backlog re-adjudication** (`results/p4/readjudication_v1/`): **330 stars
fail the calibrated red-noise floor** — screened before P3 made red-noise an
enforced verdict, and failing on numbers their own reports already carried.
52 more are explained by a catalog. Verified against the source reports, not
just self-consistency.

**Pixel pilot** (`results/p4/pixel_pilot_v3/`, 60 stars, 57 measured, all on
the target's own discovery sector): **11 stars localize significantly off
target**, offsets 0.72–4.31 px at 4.6σ–42.5σ. These are the first genuine
`pixel_offset_contamination` candidates this stack has produced. 3 more show
aperture growth; the two tests flag **disjoint sets**, which is worth an
owner's eye rather than summing to 14. **0 host reassignments** — TESS cannot
resolve a counterpart inside one 21″ pixel.

Do **not** read the 31 `no_depth_in_target_aperture` verdicts as evidence
against those signals: they are raw undetrended TESScut sums, and the campaign
found these signals in detrended photometry.

## Decisions waiting on you

1. **Status precedence (correction 38).** The P1 parity gate is red on 13
   stars. The exporter is last-campaign-wins; the ledger is
   highest-precedence-wins. 2,104 stars carry both verdicts and they agree on
   2,091. Neither is obviously right, and it grows with P5. Nothing was
   changed. **This blocks promoting any P4 evidence to voting.**
2. **TRICERATOPS (§4.6).** Deferred by your decision, so the FPP section reads
   `not_run` — which correctly **blocks every packet** from reaching `ready`.
   Either wire it, or decide FPP may be optional. It is currently the single
   thing standing between this stack and its first assembled packet.
3. **Signature churn (correction 39) and the missing release row
   (correction 49), together.** `settings_signature` uses whole-repo
   `git rev-parse HEAD`, so any commit retires the trusted release — it was
   already unreachable two doc commits after P3. And the `release_report`
   table is empty despite PROGRESS claiming otherwise. A trusted first pass
   today is refused for two independent reasons. Do not re-record the release
   without settling the signature question first.

## Next actions, in order

1. **T7 on real reductions.** `scripts/run_p4_t7_pilot.py` is written and
   correct but was not measured: MAST began closing connections on this
   session after three 60-target pixel runs plus the catalog snapshots.
   Instrumentation now separates the two cases cleanly — in the last smoke,
   3 of 4 targets showed `ConnectionError` while TIC 7146022 returned a
   genuine zero from every author. **Run this first in a fresh session**, and
   expect part of the answer to be real: these are faint FFI targets in recent
   sectors, where QLP and TESS-SPOC processing lags.
2. **TIC 234994474** — P4's named exit item, still untouched.
3. Re-run the pixel pilot wider once T7 lands, if the two agree.

## Operational notes

- **Always set this inline**, in every shell that touches science:
  `$env:EXOHUNT_CACHE_DIR = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve'`
- Cache is **83.1 GB**; catalog snapshots live in `%LOCALAPPDATA%\exohunt\snapshots`.
- Ledger: 83,555 stars, 217,156 evidence rows, schema v3.
  **Checkpoint the WAL after any bulk import** — an uncheckpointed 381 MB WAL
  made the dashboard return 503 (correction 40).
- Dashboard: `.venv\Scripts\exohunt-dashboard.exe`, port 8765. `/api/summary`
  is back inside its gate at **22.5 ms warm** (was 1,546 ms).
- **Be polite to MAST.** Three of this phase's five corrections came from
  data-acquisition mistakes, not science mistakes.

## The pattern worth reading before you trust anything here

Corrections 46, 47 and 48 are all the same failure in different clothes: a
plausible result that was an artifact of how it was obtained. 22 host
reassignments from apertures that overlapped; a whole pilot run against
sectors 2–12 for signals discovered in 98–105; an empty archive response that
was really a throttled connection. Each looked like a finding. Two of the
three were caught only because a *second* check disagreed.

Where two independent checks disagree in this codebase, the disagreement is
usually the signal.
