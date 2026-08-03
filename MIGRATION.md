# Migration and reproducible checkout

The GitHub repository is the portable source of truth. A fresh clone contains
the source code, tests, dashboard, pinned target lists, research notes, and
dependency lock file needed to install and develop EXOHUNT on another machine.

## Move to a new computer

1. Clone the repository and enter it:

   ```bash
   git clone https://github.com/spacexkspofficial/Exoplanet-Discoveries.git
   cd Exoplanet-Discoveries
   ```

2. Create a new environment instead of copying `.venv` or `node_modules`:

   ```bash
   python -m venv .venv
   python -m pip install --upgrade pip
   python -m pip install -e ".[dev]"
   cd dashboard
   npm ci
   npm run build
   cd ..
   python -m pytest -q
   ```

3. If you need the historical survey evidence, download the optional migration
   assets from the `migration-2026-08-02` GitHub release. Extract them into the
   repository root in this order:

   1. `exohunt-results-campaign-2026-08-02.tar.zst`
   2. `exohunt-results-supporting-2026-08-02.tar.zst`
   3. `exohunt-results-p2-2026-08-02.tar.zst`

   The P2 archive is last because it contains the newest ignored gate evidence
   produced in the final staging checkout. The release also includes
   `SHA256SUMS.txt`; verify each asset before extraction.

   BSD tar, available with current Windows and many Unix installations, can
   extract an asset with:

   ```bash
   tar -xf exohunt-results-campaign-2026-08-02.tar.zst
   ```

4. Rebuild the local SQLite projection from the restored append-only evidence:

   ```bash
   python -m exohunt.cli ledger-import --workspace . --parity
   ```

   Mutable cache and database state defaults to the platform’s local state
   directory and can be redirected with `EXOHUNT_STATE_DIR`,
   `EXOHUNT_CACHE_DIR`, and `EXOHUNT_DB_PATH`.

## What is intentionally not migrated

- `data/lightkurve/`: a multi-gigabyte, re-downloadable FITS cache.
- `.venv/`, `node_modules/`, package-manager caches, and build output: these are
  machine-specific and recreated by the install commands above.
- `.git/`, linked worktrees, agent/editor state, temporary files, and test
  caches: these do not belong in a portable checkout.
- Credentials and environment files: create them locally if a future workflow
  needs them; never copy them into the public repository.

Generated research evidence is kept in release assets rather than Git history
so ordinary contributors can clone the source repository quickly. The source
and the optional evidence archives are both public and independently
downloadable.
