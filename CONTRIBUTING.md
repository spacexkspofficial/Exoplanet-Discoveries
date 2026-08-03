# Contributing

Contributions are welcome. EXOHUNT is a research prototype, so changes must
preserve both software correctness and the limits of the scientific claims.

## Development setup

Use Python 3.11 or 3.12 and Node.js 22 or newer.

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest -q
```

On Windows PowerShell, activate the environment with
`.\.venv\Scripts\Activate.ps1`; on Linux or macOS, use
`source .venv/bin/activate`.

Build the dashboard separately:

```bash
cd dashboard
npm ci
npm run build
```

## Pull requests

- Keep each commit focused on one concern.
- Add or update tests for behavior changes.
- Run the Python suite and dashboard build before opening a pull request.
- Keep generated caches, results, local databases, credentials, and editor or
  agent state out of commits.
- Record any measured scientific behavior change in `PROGRESS.md` and preserve
  rejected/null outcomes rather than rewriting the record.
- Never raise the project’s autonomous claim ceiling above
  `packet_ready_for_review`. A transit-like signal is not a confirmed planet.

For security issues, follow [SECURITY.md](SECURITY.md) instead of filing a
public issue.
