# Documentation map

Everything that is not a live, load-bearing document lives under `docs/`.
Five markdown files remain at the repository root, and each is there for a
reason that is worth knowing before you move one.

## What stays at the root, and why

| File | Why it cannot simply be moved |
|---|---|
| `README.md` | Repository entry point. |
| `CONTRIBUTING.md`, `SECURITY.md` | GitHub reads these from the root. |
| `MASTER_PLAN.md` | **Referenced from inside the detection kernel.** |
| `PROGRESS.md` | **Referenced from inside the detection kernel.** The living honesty ledger; append corrections here. |
| `HANDOFF.md` | The one current handoff. Rewritten in place, never forked into a second file. |

### The kernel-reference rule

`src/exohunt/config.py` digests the **source text** of every module in
`DETECTION_KERNEL_MODULES` to produce `kernel_version()`. A calibration is
valid only for one kernel digest, and re-earning one costs roughly 16–21 hours
of compute.

`MASTER_PLAN.md` is named in comments inside `detrend.py` and `population.py`,
and `PROGRESS.md` inside `config.py` — all three are kernel modules. Moving
either document means editing those comments, which changes the digest, which
retires the current calibration for a documentation change.

So: **before moving any document, check whether a kernel module names it.**

```bash
grep -rn "YOUR_DOC.md" src/exohunt/
```

If the answer includes any of `calibration.py`, `campaign.py`, `cli.py`,
`commonmode.py`, `config.py`, `detection.py`, `detrend.py`, `detrending.py`,
`photometry.py`, `population.py`, `screening.py`, `search.py` or `vetoes.py`,
leave the document where it is. The same caution applies to the smaller
digested sets — `adjudicate.py`/`identity.py`/`snapshots.py` (vetting),
`pixel.py`, and `crossreduction.py`.

After any edit near these modules, confirm the digest did not move:

```bash
.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'src'); from exohunt.config import kernel_version; print(kernel_version())"
```

## `docs/p2/` — P2 measurement records

One document per measured question from the P2 detector-characterisation
phase. These are evidence, not plans: each records what was measured, on what
cohort, and what it settled.

- `P2_CATALOG_MASKING.md` — masking known catalog ephemerides
- `P2_CATALOG_MATCHING.md` — matching detections to known objects
- `P2_EDGE_BIAS.md` — measured trend-model bias at segment edges
- `P2_EDGE_DIAGNOSTIC.md` — the two closed edge-recovery mechanisms
- `P2_HARMONIC_MATCHING.md` — harmonic and alias families
- `P2_SEARCH_GRIDS.md` — period-grid construction and its A/B
- `P2_T3_VETOES.md` — the T3 veto set

## `docs/science/` — standing science references

Current, non-historical documents about what the survey claims and how it is
allowed to claim it.

- `DETECTION_LIMITS.md` — what this pipeline can and cannot detect
- `SURVIVOR_VETTING.md` — the lanes a survivor passes through
- `VALIDATION.md` — interpreted results
- `RESEARCH_REVIEW.md` — the long-form research review
- `REPORTING_GUIDE.md` — how findings get written up

## `docs/archive/` — superseded, kept for provenance

Historical handoffs and completed plans. See `docs/archive/README.md` for the
rename map. **Nothing here is current.** If an archived document contradicts
`PROGRESS.md` or `HANDOFF.md`, the root document wins.
