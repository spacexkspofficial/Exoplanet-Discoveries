# Owner decisions, approved 2026-08-08 — awaiting the trigger

The owner approved all seven items below on 2026-08-08 and asked that **nothing
be executed** until they say **"execute the decisions"**. This file is the
execution plan. Read it together with `NEXT_AGENT_HANDOFF.md` and PROGRESS
corrections 55–72.

Do not start any of this on your own initiative. Wait for the phrase.

---

## The seven decisions

| # | Decision | Status |
|---|---|---|
| 1 | **The detection kernel may be modified** | approved |
| 2 | **Promote the three unused artifact flags to vetoes** | approved |
| 3 | **Implement status precedence** (ledger authoritative) | approved |
| 4 | **Install TRICERATOPS** (confirmed free/open source) | approved |
| 5 | **Trusted-first-pass: option B** — calibrate every cohort before searching it | approved |
| 6 | **Shift the science lane to §6.2 (monotransits)** | approved |
| 7 | **Clean the git history, then push** (option 7a) | approved |

---

## The one dependency that shapes everything

**Any change to the detection kernel invalidates the trusted release signature
and forces a re-calibration.** The frozen modules are `search`, `vetoes`,
`detrend`, `detrending`, `detection`, `photometry`, `population`, `screening`,
`campaign`, `commonmode`, `calibration` — byte-identical to calibration commit
`36c935b` (P4 close, decision 4).

Combined with **decision 5 (option B)**, which requires calibrating *every*
cohort before searching it, this means: **decisions 1 and 2 each trigger a full
re-calibration cycle**, measured at ~16.5 h per 1,000-star cohort (correction
58). Batch the kernel work so one re-calibration covers all of it rather than
paying that cost repeatedly.

The owner has stated this machine is effectively dedicated to the project, so
the compute is available — but the *ordering* still matters.

---

## Suggested execution order

### Phase 0 — make the work safe (do this first)

**7a.** Rename the branch off its stale P2 name and push.

- Current: `codex/p2-catalog-matching`, **50 commits ahead** of origin,
  carrying P2 → P5 work.
- Suggested name: `p5-calibration-and-recovery`.
- **Do not squash.** The owner chose 7a without settling the squash question;
  the standing recommendation is to keep the 18 correction commits intact,
  because each carries one correction's evidence and squashing destroys the
  traceability that makes the honesty ledger auditable. **Confirm with the
  owner before squashing anything.**
- Push once, cleanly. Avoid force-pushing published history.

### Phase 1 — cheap unblocks, no kernel change

**4.** Install TRICERATOPS. Verify the licence first. This unblocks *every*
candidate packet from reaching `ready` — currently `false_positive_probability`
is `not_run` and correctly blocks all of them. Do not relax the packet contract
as an alternative; that would manufacture a pass (P4 close, decision 2).

**3.** Implement status precedence. Decided in principle at the P4 close (the
ledger's stage-then-precedence fold is authoritative), never built. Until it
lands, all P4 vetting evidence stays non-voting and cannot change any star's
status. Note correction 50: an earlier attempt drifted into editing the parity
gate until it passed and was reverted. Two findings from that attempt should
shape the real fix — separating the precedence divergence *does* reduce
`star_status_differences` to `{}`, and the exporter has outlived its role as a
field-level parity oracle.

### Phase 2 — the artifact flags (decide the route first)

**2.** Promote `spacecraft_harmonic`, `duration_at_grid_rail` and
`period_at_search_ceiling` to real vetoes. Evidence in correction 72: enriched
**1.63× / 2.92× / 2.40×** among survivors (≈5.7σ / 13.8σ / 7.8σ), together
flagging **311 of 990 survivors (31.4%)** against 14.8% of the population.

Two routes — **ask the owner which**:

- **2a. Vetting-layer reclassification.** Post-hoc, no kernel change, no
  re-signing, no re-calibration. Fastest.
- **2b. Real kernel veto** in the screening path. Scientifically cleaner —
  future runs never promote these at all — but touches a frozen module, so it
  triggers re-signing and, under decision 5B, re-calibration.

If 2b, batch it with the phase-3 kernel work so one calibration covers both.

### Phase 3 — kernel work (approved, expensive)

**1.** Highest-value target, from correction 64: **14 of 53 lost known planets
had the true period among the five recorded BLS peaks and were simply not
selected** — `pi Men c` at rank 4 with relative power 0.9999999, `WASP-169 b`
at rank 3 with the same. These are near-ties resolved the wrong way, recoverable
with no new photometry and no sensitivity change.

Second target: the **~1 d grid-rail pinning** family, the dominant search
failure in three independent cohorts (corrections 63, 65, 66) — 83% of P5
rejections, and the mechanism behind the elevated false-alarm rate.

Also worth fixing while in there: `top_period_peaks` stores only five peaks, so
"absent from the peak list" cannot be distinguished from "deeper in the
periodogram" (correction 64). Storing more is cheap and sharpens the diagnosis.

Then: re-sign the calibration and re-run it (decision 5B).

### Phase 4 — the new lane

**6.** Stand up §6.2, monotransits / long-period single events. Per
MASTER_PLAN §6.2 it rides the same sample as §6.1 — no separate downloads —
plus brighter multi-sector stars. **False-alarm control is the whole game
there**, which lands directly on correction 68's unresolved finding: the P5
cohort's inverted survivor rate is 4.2× over budget and scrambled 2.1× over.
Fix false alarms before trusting any monotransit result, since a monotransit
has no repeat to confirm it.

Lane §6.1 is not deleted — its measurements stand and are recorded. We stop
investing in it.

---

## Standing constraints that survive these decisions

- **Always set inline** in every shell touching science:
  `$env:EXOHUNT_CACHE_DIR = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve'`
- **Run the common-mode screen per campaign**, never pooled. Pooling all
  campaigns silently retracts 1,665 established systematics by diluting the
  expectation (correction 71). Check the old→new transition matrix before
  importing any screen, not the headline counts.
- **Add `camera`/`ccd` to every cohort builder.** Their absence left the P5 dip
  registry at `s14-camunknown-ccdunknown` with zero windows (corrections 57, 69).
- **Cohort CSVs must use `stellar_radius_solar` and `stellar_mass_solar`.**
  The canonical names are load-bearing; `radius_solar` silently disabled two T3
  physical vetoes across a whole 1,000-star pass (correction 57).
- **Checkpoint the WAL after any bulk ledger import**, or the dashboard 503s.
- **PowerShell here is 5.1**: no `&&`, no ternary. Use `;` and `if ($?) { }`.
- Campaign runs exit **1** with `state: retry_pending` whenever any target
  errors. That is by design, not a crash.

## The habit that caught the most this session

Five separate times a plausible result was an artifact of how it was obtained —
a throughput figure 45× wrong, two vetoes silently dead, a "100%" recovery rate
that was 83.6%, a "depth-limited not brightness-limited" claim inverted by
aggregation, and a survey-wide screen that would have destroyed 1,665 verdicts
while looking better on every summary statistic.

Every one was caught the same way: **by checking the thing that would change if
the result were an artifact, rather than the headline number.** The transition
matrix, not the counts. The per-star medians, not the wall clock. The field the
pipeline actually gates on, not the one with a similar name.
