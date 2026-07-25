# EXOHUNT local dashboard

This dashboard is intentionally local-only. It has no hosting manifest, cloud
runtime, external database, analytics, or authentication provider.

Build the browser assets:

```powershell
npm.cmd install
npm.cmd run build
```

Start the FastAPI service from the repository root:

```powershell
.\.venv\Scripts\exohunt-dashboard.exe
```

Then open `http://127.0.0.1:8765`. The server binds to the loopback interface,
so it is not reachable from other computers on the LAN or from the internet.

`/data/survey.json` is generated from the local append-only search ledger and
active campaign checkpoints, then served with `Cache-Control: no-store`. The UI
polls it every five seconds and ignores late responses from older polls.
When more than one nonterminal checkpoint exists, a `running` or `finalizing`
campaign takes precedence over `retry_pending`; within the same state class,
the freshest checkpoint is selected. This keeps a completed partial campaign
visible without allowing it to replace the live campaign progress display.
Search/download errors are shown as `Retry needed`, separately from completed
searches. Completed targets are split into `No transit detected in search
window`, `Strongest signal screened out`, `Single-event lead`, and `Automated
survivor` classes. None of those labels means a star is planet-free, and an
automated survivor is a follow-up lead rather than a vetted candidate. During a
parallel campaign the live snapshot also reports analysis workers, downloads
in flight, staged targets, and active versus configured worker slots. Analysis
and download pools are reported separately so idle capacity is not shown as
active work.

When a schema-v2 context report exists under `results/`, its authoritative
metadata disposition overlays the initial screening label. The filters and
metrics distinguish known EB rediscoveries, known-binary residual review,
known-variable review, crowding review, catalog gaps, incomplete context
queries, and unresolved transit-like signals. “Unresolved” still does not mean
candidate or planet. The selected-target panel lists the follow-up lane and
per-source completion states.

The selected-sector overlay is mission geometry, not a box fitted to the local
stars. It renders four TESS cameras and their sixteen CCD science-pixel
boundaries in RA/Dec; the 3D and Earth views extend the same angular boundaries
as sight lines from the observer. Their far end is only a visualization cutoff.
The small bundled Sector 1–107 file is generated from the `tess-point` focal-
plane model, while calibrated image WCS remains the final pixel-level authority.

The sector timeline reports local campaign coverage, not the fraction of every
star or detector pixel in a TESS sector. Its denominator is the deduplicated
target plan stored in the local campaign CSVs, and its numerator is targets with
successful result reports. Blue means a sector-specific local plan is complete,
orange means that plan is the active campaign, and gray fill shows partial
coverage from an inactive plan or opportunistic targets. An empty gray cell
means no local target has been successfully analyzed there. Tooltips always show
the analyzed/targeted counts and state this scope explicitly.

The 2D and 3D star maps share the same flat status symbols and colors as the
status filters. Marker shape remains meaningful at small sizes, and no glow is
used to imply scientific confidence.
