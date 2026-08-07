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
| Backlog resolution | **98.1%** (1,337/1,363), of which **351 stars (25.8%) carry a terminal verdict** and 986 are filed leads (correction 53) |
| TIC 234994474 | **Done, and it is a downgrade** — `science_vetted_lead` → `single_sector_unconfirmed` (corrections 51, 52) |

**All three exit clauses are satisfied, and the measurement now says what it
means.** The 98.1% is split: **351 stars (25.8%) carry a terminal verdict** —
330 killed by the calibrated red-noise floor, 11 localized off target, 10 EB
rediscoveries — and 986 are review-lane leads, meaning every declared source
was checked and none explains them. Both count toward the plan's exit; only
the first is an answer about the signal.

That is a defensible P4 exit. The remaining judgement is whether 986 filed
leads is an acceptable state to enter P5 with, which is an owner's call.

## Built and committed

- **Snapshots** (`snapshots.py`): 8 sources fetched and content-hashed —
  nasa_toi 8,113 · nasa_ps 6,336 · tess_eb 4,584 · gaia_dr3 8,453 · vsx 252 ·
  gaia_nss_sb1 79 · asassn_variables 61 · gaia_nss_eb 1.
- **Identity graph** (`identity.py`, ledger schema v2): 1,363 canonical nodes,
  9,847 ranked counterpart edges. **616 stars (45.2%) have more than one
  plausible Gaia counterpart inside one TESS pixel.**
- **Adjudication** (`adjudicate.py`): §4.3 period **and** epoch matching.
- **Pixel vetting v2** (`pixel.py`) — **run on real pixels**, below.
- **T7 cross-reduction** — **run on the real cohort** (`results/p4/t7_pilot_v3/`):
  60 stars, 0 errors, 0 search failures. 37 genuinely have no alternate
  reduction; 9 have ≥2 informative independent reductions; **5 agree on depth
  and survive the undetrended SAP fold**; 0 promoted, all blocked on stacked
  vetoes a single-sector cohort cannot measure (correction 54).
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

## Decisions taken at the close

The owner delegated the three open questions. PROGRESS records these in full
under "Decisions taken at the P4 close".

1. **Status precedence: the ledger is authoritative** — its stage-then-
   precedence fold is the principled rule, and keeping a survivor lead alive
   is the safer error in a discovery survey. **Recorded, not implemented.** An
   attempt to re-scope the parity gate was reverted (correction 50): it was
   turning into "edit the gate until it passes". Two findings survive that
   attempt and should shape the real fix — separating the precedence
   divergence *does* reduce `star_status_differences` to `{}`, and P4's
   identity enrichment has created a second, legitimate divergence class the
   exporter structurally cannot match. **The exporter has outlived its role as
   a field-level parity oracle**; re-scoping it deserves its own session.
2. **TRICERATOPS: unchanged, and do not relax the packet contract.** Making
   FPP optional would manufacture a pass. `incomplete` with
   `false_positive_probability` named as the blocker is the correct state.
   Installing TRICERATOPS is the fix.
3. **Missing release row: fixed.** The ledger now holds
   `trusted sig1:f78342a7…`, re-recorded by the project's own finalizer with
   every gate re-validated on the way in.
4. **Signature churn: deliberately not changed.** Every detection-kernel
   module is byte-identical to the P3 calibration commit `36c935b` and
   `ScienceConfig` still digests to the pinned P3 value, so correction 39 is a
   defect in the identity scheme rather than in the science. Switching the
   scheme now would un-match the release just restored; it must land together
   with a re-signed calibration. **This is P5's entry work.**

## Still open

- Promoting any P4 evidence to voting, which waits on decision 1 landing.
- The first assembled packet, which waits on decision 2.

## Next actions, in order

1. **A multi-sector cohort, so the stacked-fold vetoes can run.** Every one of
   the 60 T7 stars was blocked on stacked secondary/odd-even, which
   single-sector data cannot measure. That single missing check is now the
   only thing standing between the 5 best leads and a full §4.5 evaluation.
2. **Widen both pilots.** Pixel vetting and T7 agree where they overlap (see
   TIC 76804724 below), and both runners are now correct and fast — ~10 s per
   star with the cache warm.
3. **TRICERATOPS**, which blocks every packet from `ready`.

### The five leads worth looking at first

TICs **4809705, 18654235, 55757565, 67013276, 76804724** — depth agreement
across informative independent reductions *and* presence in the undetrended
SAP fold. Two of §4.5's four requirements each.

**TIC 76804724 comes with a warning**: it also localizes 4.31 pixels off
target at 16.9σ. Two independent stages agree its signal is real and that it
does not belong to that star.

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

Correction 50 is the same failure pointed at the tooling instead of the data:
adding exemptions to a parity gate until it went green, one defensible step at
a time. It was caught by noticing the *direction*, not any single step.

Where two independent checks disagree in this codebase, the disagreement is
usually the signal — and when a gate starts failing, the first question is
whether it has found something, not how to make it stop.
