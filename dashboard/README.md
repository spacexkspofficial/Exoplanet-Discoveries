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
Search/download errors are shown as `Retry needed`, separately from completed
searches. Completed targets are split into `No transit detected in search
window`, `Strongest signal screened out`, `Single-event lead`, and `Automated
survivor` classes. None of those labels means a star is planet-free, and an
automated survivor is a follow-up lead rather than a vetted candidate. During a
parallel campaign the live snapshot also reports analysis workers, downloads
in flight, and staged targets.

The selected-sector overlay is mission geometry, not a box fitted to the local
stars. It renders four TESS cameras and their sixteen CCD science-pixel
boundaries in RA/Dec; the 3D and Earth views extend the same angular boundaries
as sight lines from the observer. Their far end is only a visualization cutoff.
The small bundled Sector 1–107 file is generated from the `tess-point` focal-
plane model, while calibrated image WCS remains the final pixel-level authority.
