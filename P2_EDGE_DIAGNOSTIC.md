# P2 narrow-guard + edge-diagnostic experiment

Measured 2026-07-27 PDT (2026-07-28 UTC) after the owner selected option 2:
replace the production half-window edge guard with a quarter-window guard, but
prevent automatic promotion unless at least two fitted transit events have
real in-transit sampling and a two-sided local baseline.

## Decision

**Rejected and reverted.** The mechanism improved cadence retention and
quarantined edge-dependent detections, but it failed both real-data gates that
can be evaluated before an injection/population run. Production remains the
Savitzky-Golay half-window guard.

## Implementation tested

- `edge_guard_window_fraction`: 0.50 → 0.25.
- Every strongest periodic signal was evaluated by `vetoes.per_event_support`.
- A signal with at least two nominal observed transits but fewer than two
  locally supported events received an explicit edge-dependent rejection.
- Edge-dependent signals used the canonical `screened_rejected` status and an
  `edge_only_diagnostic` vetting tier, so they could not enter the automated
  survivor queue.
- Detrending settings and the new policy were included in scientific identity,
  preventing reuse of reports from the production signature.

Focused mechanism tests and the complete experimental suite passed
(**190 tests**). After the measured rejection the behavior and its temporary
tests were reverted; the production suite is again **186 tests**.

## Locked 371-target result

Command: shipping `batch-hunt`, SPOC 120 s, Sector 100, four workers, cached
photometry, `--allow-no-known`. There were **0 execution errors**.

| arm | median retention | artifact enrichment | one-sided p | survivors | survivors on artifact epochs |
|---|---:|---:|---:|---:|---:|
| Production half-window guard | 0.66933 | 1.137 | 0.046 | 24 | **1** |
| Support-weighted biweight, alpha=5 | 0.99287 | 1.140 | 0.039 | 51 | **9** |
| Quarter-window + event-support lane | **0.83584** | **1.14193** | **0.04805** | 21 | **3** |

The quarter-window recovered roughly half of the production guard's lost
cadences, as expected, but missed the required 0.85 retention by 0.01416. More
importantly, artifact enrichment did not improve and artifact-aligned survivors
rose from one to three.

The edge lane marked **34** signals edge-dependent. Fifteen had no other
rejection and would otherwise have entered the survivor queue; all 15 were
successfully held in the diagnostic tier. That safety layer worked as designed,
but it did not catch the three artifact-aligned survivors. A two-sided local
baseline proves sampling support; it does not prove that the Savitzky-Golay
trend is unbiased near the edge. The remaining failure is model bias, not
missing local samples.

## Stop condition

The full injection-recovery and authorized 500-target population gates were not
run. The behavior had already failed retention and artifact regression, so
those runs could not make it releasable. The 500-target authorization remains
unspent.

## Reproducibility

Raw reports and the complete measurement JSON are in:

`results/p2_gates/artifact_narrow_guard_edge_diagnostic/`

The reusable measurement command is:

```powershell
python scripts/measure_p2_artifact_gate.py `
  results/p2_gates/artifact_narrow_guard_edge_diagnostic `
  --label "Narrow 0.25 guard + two-sided edge diagnostic lane"
```

It uses 20,000 deterministic empirical-null draws (seed 20260727), choosing two
control epochs uniformly over the observed time span and applying the same
fitted ephemerides and duration-scaled phase tolerances used at BTJD 4074.4 and
4080.8. Running the same method over the two historical arms reproduces their
recorded enrichment to Monte Carlo precision.

## What this rules out

Do not retry:

- another `(window, floor, alpha)` support-weight sweep;
- the quarter-window guard with local sample support alone;
- a looser event-support cadence count.

The next edge-recovery design must measure or avoid trend-model bias itself
(for example, an independently prepared edge model or a promotion rule based
on agreement across genuinely independent reductions). Until such a design is
approved and passes the locked gates, the production half-window guard remains
the honest choice.

**Followed up 2026-08-04 — see `P2_EDGE_BIAS.md`.** That bias is now measured
directly rather than inferred from survivor counts: it is roughly 89% of the
edge error at every offset, and the ratio to a pure-variance null is flat in
support fraction, which is why no `(window, floor, alpha)` combination could
have worked. The measurement also gives the guard width a number for the first
time, and it turns out to depend on the shallowest depth being claimed.
