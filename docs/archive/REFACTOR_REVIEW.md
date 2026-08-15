# EXOHUNT refactor review checkpoint

Prepared 2026-07-26. This is a code-review checkpoint, not authorization to
start another campaign.

## Stop condition

The currently running `results/campaign/sector100_spoc` campaign may finish.
After that, do not start Sector 105, a 200-target equivalence campaign,
injection/recovery, context vetting, science vetting, or any other
campaign-scale/science-download job without explicit owner approval.

## Scientific finding that motivated the detrending change

The mission-processed Sector 100 run improved the old TESScut result but did not
remove the shared timing. A read-only partial screen at 1,688 completed targets
measured:

- 418 survivors: 24.8%, versus the expected well-under-5% acceptance criterion.
- 108 common-mode/localized flags: 6.4% of fitted ephemerides at this partial
  sample size.
- 99 survivors within 6% of 6.85 days: 23.7% of survivors.
- The flagged epoch histogram still peaks at BTJD 4074.4 and 4080.8.

Those epochs occupy trend-unsupported light-curve edges. The current live run
uses the old `flatten(window_length ~= 2 days, break_tolerance=5)` path and is
therefore diagnostic; its survivors are not planet candidates.

## Review slice A: classification architecture (HANDOFF 6.1/6.2)

The reviewed implementation lives under
`.refactor-staging/handoff-architecture` until the active campaign exits.

- `src/exohunt/status_registry.json` defines all 23 statuses once: slug,
  canonical label, short label, help, symbol, color, CSS class, evidence stage,
  and within-stage precedence.
- `src/exohunt/statuses.py` loads the registry and resolves evidence through
  `in_light_curve < catalog_context < measured_science < population_screen <
  human_outcome`.
- `src/exohunt/status_codegen.py` and
  `scripts/generate_status_registry.mjs` generate the checked-in TypeScript
  registry. `npm run build` regenerates it before type checking.
- `dashboard/src/App.tsx` imports the generated status type and tables; its
  hand-maintained duplicates are removed.
- `src/exohunt/dashboard.py` calls one resolver instead of comparing a flat
  magic-integer dictionary in four places.
- Characterization tests preserve legacy labels, equal-rank "later wins"
  behavior, common-mode authority over per-star science, and human authority
  over every automated status.

## Review slice B: edge-safe detrending

- `src/exohunt/detrending.py` provides the one shared detrending path for
  processed light curves and TESScut fallback data.
- A routine 13-minute interruption no longer creates a separately fitted
  Savitzky–Golay segment. Only gaps of at least 0.5 day split the trend.
- Half of the two-day trend window is excluded at every physical/segment edge,
  because those estimates lack samples on both sides.
- Reports retain the existing cadence/window fields and add the complete named
  detrending configuration, derived gap threshold, segment count, and removed
  cadence count.
- Regression tests show that BTJD 4074.4 and the downlink-edge zone are
  excluded while an interior event remains searchable.

This is a behavior change and must not be represented as a structure-only
commit.

## Test and build evidence

- `112 passed` from the isolated checkout.
- Bare `pytest` now imports the checkout's own `src` and uses a unique
  system-temporary basetemp; it no longer resolves the editable install from
  main or uses OneDrive's locked `.pytest-tmp`.
- TypeScript checking passed.
- Vite production build passed.
- Regenerating `dashboard/src/generated/statusRegistry.ts` produces no drift.
- No science products were downloaded and no campaign was started for these
  checks.

## Files intended for integration

Existing files:

- `dashboard/package.json`
- `dashboard/src/App.tsx`
- `pyproject.toml`
- `src/exohunt/cli.py`
- `src/exohunt/dashboard.py`

New files:

- `dashboard/src/generated/statusRegistry.ts`
- `scripts/generate_status_registry.mjs`
- `src/exohunt/detrending.py`
- `src/exohunt/status_codegen.py`
- `src/exohunt/status_registry.json`
- `src/exohunt/statuses.py`
- `tests/conftest.py`
- `tests/test_dashboard_status_precedence.py`
- `tests/test_detrending.py`
- `tests/test_statuses.py`

## Git cleanup required

Commit `0802edf` on `origin/main` accidentally tracks the complete
`.refactor-staging` snapshot (116 files, about 10.1 MB). `.gitignore` now
contains `.refactor-staging/`, but this Codex session has read-only `.git`
metadata and could not update the index.

Run these commands manually from the project root; they do not rewrite
published history and do not delete the local staging files:

```powershell
git rm -r --cached -- .refactor-staging
git add .gitignore
git commit -m "Stop tracking the local refactor staging tree"
git push
```

Do not use `git filter-repo` or force-push unless the owner separately chooses
to rewrite published history.

## Pending owner-approved validation

The following remain deliberately unexecuted:

1. Diff a fixed 200-target before/after result set with input held at explicit
   `--author TESScut --cadence-seconds 158`.
2. Run a small processed-photometry smoke set to quantify edge removals and
   verify known transits remain recoverable.
3. Re-run Sector 100 with the reviewed detrending configuration.
4. Run Sector 105 on processed photometry.
5. Run campaign-path injection/recovery and false-alarm validation.

Integration should run only code-level checks (`pytest`, TypeScript, and the
frontend production build), then stop for owner review.
