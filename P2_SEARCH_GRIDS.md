# P2 physical search-grid implementation

Measured and implemented 2026-07-28 PDT through the real `batch-hunt`
shipping path. This is a detection-policy change, not a planet, novelty, or
false-alarm claim.

## Decision

The fixed 0.25--6 hour duration grid and hard period ceiling do not ship
unchanged.

1. The reportable period ceiling comes from the observation baseline and the
   minimum-transit rule: two events for one sector, three for multiple
   sectors. The requested minimum remains the search floor, and the requested
   maximum caps the reportable period.
2. BLS searches 8% beyond the reportable ceiling. A best fit in that overscan
   zone is recorded and rejected, so the report boundary is not a search rail.
3. The duration grid is log-spaced over 0.3--1.5 times the central circular
   transit duration implied by stellar density, with named 0.5 and 12 hour
   safety clamps.
4. Density uses catalog stellar mass and radius only when both are finite and
   positive. Missing pairs use an explicit, recorded solar-density fallback.
5. A best period or duration on the actual grid endpoint is recorded and
   rejected. Boundary fits remain diagnostics, never automated survivors.

The production report records the requested scientific configuration, actual
period and duration bounds, density source, overscan state, and rail flags.
Target-list builders now retain TIC stellar mass, and `batch-hunt` carries
both mass and radius into the single-target analysis metadata.

## Locked cohort

The gate reuses the frozen first 150 rows of
`targets/sector100_expansion_5000.csv`, the same TESScut/158 s cohort as
`golden_v0`.

`targets/p2_search_grid_golden_150.csv` preserves the exact ordered
target/sector identity and adds stellar mass only where a saved cross-mission
context contains a complete mass/radius pair.

| Measurement | Value |
|---|---:|
| Cohort rows | 150 |
| Ordered identity SHA-256 | `8af13d49789e5ea5a15dd8f35673b314ee531df8a7f07f6444ab99009df4041b` |
| Frozen golden identity match | yes |
| Catalog mass + radius | 69 |
| Explicit solar fallback | 81 |

The cohort manifest is
`targets/p2_search_grid_golden_150.json`. The source target-list SHA-256
remains
`43a13ce57a2d35a52861312e69e888c71718bfe679c81efe2f66a181def936e0`.

## Shipping-path A/B

Three report sets are compared:

| Arm | Duration policy | Reports | Survivors | Rejected | Errors |
|---|---|---:|---:|---:|---:|
| Frozen golden | fixed legacy grid | 150 | 35 | 115 | 0 |
| New fallback grid | solar-density fallback for all targets | 150 | 5 | 145 | 0 |
| New density grid | catalog density where complete, fallback otherwise | 150 | 6 | 144 | 0 |

The fallback arm isolates the period-boundary and new duration-grid policy.
The density arm changes only the 69 complete stellar-parameter targets.

Input identity checks pass:

- all three arms contain exactly the locked 150 TICs;
- all 150 observation windows match exactly;
- all 150 normalized extraction metadata payloads match after removing only
  the deliberately added stellar mass/radius fields; and
- the 81 solar-fallback targets are exact in strongest signal, triage, search
  grid, and the complete measured science payload across both new arms.

TIC 305567403 required the same known TESScut re-fetch during the density run.
Its normalized extraction metadata and observation window still match, and it
belongs to the exact 81-target fallback subset.

## Measured effects

### Boundary behavior

| Measurement | New fallback arm | New density arm |
|---|---:|---:|
| Best fit in period overscan | 20 | 14 |
| Overscan fits passing triage | **0** | **0** |
| Any grid-rail fit | 124 | 120 |
| Duration-rail fit | 123 | 119 |
| Grid-rail fits passing triage | **0** | **0** |

The offline projection found 118/150 golden fits on the legacy duration
endpoints, including 27/35 historical survivors. The actual new grids do not
make rail-seeking fits disappear: 123 fallback and 119 density fits choose an
effective duration endpoint. They remove the fixed 6-hour pile-up and replace
it with star-specific physical endpoints, but the rail remains a dominant fit
class. The hard gate works as designed--none pass--while its sensitivity cost
remains a later injection/known-planet calibration question.

### Measured correction during the gate

The first A/B compared Astropy's returned duration against the unquantized
requested grid and therefore counted only 5/4 total rails, producing 22/19
passes. Astropy's fast BLS evaluates durations in bins of
`minimum_duration / oversample` and returns the quantized effective value
(for example, a requested 6.44897 hours returns as 6.45 hours).

Production now records both requested and effective duration grids, compares
against the effective endpoints, and carries a search-policy version in
checkpoint identity. Fresh runs produce the 124/120 rail counts and 5/6 passes
above. The correction changes no fit: the corrected fallback arm is 150/150
exact to the first fallback arm in strongest signal, observation window, and
normalized extraction metadata. Only rail metadata and triage change.

### Isolated density effect

The 81 fallback targets are completely invariant. Among the 69 targets whose
duration grid changes from solar fallback to catalog density:

| Transition | Count |
|---|---:|
| survivor to survivor | 2 |
| survivor to rejected | 1 |
| rejected to survivor | 2 |
| rejected to rejected | 64 |

Passes move 3 to 4 in this subgroup. TIC 36877906 is the one lost survivor; its
density-backed fit is both below the S/N threshold and on a duration rail.
TIC 54147357 and TIC 81699706 are gained because their density-backed fits move
off the fallback duration rail; one also loses the duty-cycle flag.

The recovered period stays within 1% for 25 of 69 density-backed targets, is a
simple harmonic for one, and changes more substantially for 43. That breadth
is expected from changing the duration dimension of the BLS objective and is
why this slice is recorded as a measured behavior change rather than an
equivalence refactor.

These counts do not establish completeness, reliability, or a false-alarm
rate. Injection recovery, inverted-data calibration, and the known-planet
campaign remain P3/P2-exit gates. The claim ceiling remains
`packet_ready_for_review`.

## Verification

Permanent tests cover:

- requested bounds, baseline/minimum-transit ceilings, and period overscan;
- catalog-density and named fallback duration grids;
- period and duration endpoint detection;
- shipping-path overscan and rail rejection;
- batch propagation of stellar mass/radius;
- target-list mass preservation;
- Astropy effective-duration quantization and upper-rail detection;
- frozen-cohort identity and portable provenance; and
- three-arm A/B measurement plus exact fallback invariance.

Complete worktree suite: **228 passed**.

Reproducible cohort command:

```powershell
python scripts/build_p2_search_grid_cohort.py `
  --source-csv targets/sector100_expansion_5000.csv `
  --context-dir results/vetting/all_campaigns/context `
  --context-label results/vetting/all_campaigns/context `
  --output-csv targets/p2_search_grid_golden_150.csv `
  --limit 150
```

Each new arm used:

```powershell
$env:PYTHONPATH = "<worktree>\src"
$env:EXOHUNT_CACHE_DIR = "<frozen-golden-cache>"
python -m exohunt.cli batch-hunt `
  --targets <fallback-or-density-target-csv> `
  --output-dir results/p2_gates/<arm> `
  --author TESScut --cadence-seconds 158 `
  --max-targets 150 --cache-max-gb 8 `
  --workers 4 --download-workers 2 --allow-no-known
```

Comparison:

```powershell
python scripts/measure_p2_search_grid_ab.py `
  --golden-dir results/equivalence/golden_v0 `
  --fallback-dir results/p2_gates/search_grid_shipping_railfix_150 `
  --density-dir results/p2_gates/search_grid_shipping_density_railfix_150 `
  --cohort-csv targets/p2_search_grid_golden_150.csv `
  --output results/p2_gates/search_grid_shipping_ab_150.json
```

Raw, gitignored evidence:

- `results/p2_gates/search_grid_policy_projection/`
- `results/p2_gates/search_grid_shipping_railfix_150/`
- `results/p2_gates/search_grid_shipping_density_railfix_150/`
- `results/p2_gates/search_grid_shipping_ab_150.json`
