# App Catalog

**An offline-first catalog for a messy folder full of downloaded Windows installers.**

If you've been collecting installers, portable builds, and scene releases for years — across multiple backup drives, with names like `Adobe.Captivate.v2.0.1177.WinAll.Keygen.Only-ViRiLiTY`, `Deamon Tools 4.40.2`, and `setup(1).exe` — this app turns that pile into a clean, searchable catalog. It reads what's on disk, works out what each thing actually *is*, groups multiple versions/editions of the same product together, and (optionally, with a dry-run first) reorganizes the files themselves into a tidy `Catalog/Subcatalog/App/Version/` tree.

Everything lives in one SQLite file. No background services, no cloud, no telemetry.

---

## What it does

**Scan & resolve**
Walk any folder tree, identify install units (installers, archives, ISO payloads), extract structured fields from messy folder/file names — app name, version, edition, architecture, language, portable flag — and cluster variants of the same product into one canonical app. Every heuristic in the name-cleaning pipeline is a live setting, editable from the GUI with no restart.

**Enrich**
Match resolved apps against a locally-cached [Winget manifest](https://github.com/svrooij/winget-pkgs-index), the [winutil applications list](https://github.com/ChrisTitusTech/winutil), and (on-demand) Chocolatey search. Optionally shell out to `winget show` for real publisher/description/license on a per-app basis. Every scraped field shows its source, and nothing overwrites a name you've locked.

**Review & correct**
A table of every app with filters, sortable columns, and per-row status coloring. A detail panel with editable name/catalog/subcatalog, "Original / Alternative / Winget / Choco" one-click name alternatives, an inline variant list, and per-variant actions (move to another app, split into a new app, ignore).

**Organize**
- **Duplicates tab** — grouped candidates with a checkbox tree and one-click merge, using the same `merge_apps()` the main UI uses.
- **Categories tab** — a live tree + board view of the catalog/subcatalog taxonomy, with rename / merge / move / promote / demote operations that update every affected app in one go.
- **Report tab** — a read-only health report (needs-review queue, unresolved scans, confidence mismatches, metadata gaps, duplicate groups, stale scan roots, scan errors), exportable as Markdown, CSV, or to the clipboard. Double-click any finding to jump straight to that app.
- **Reorganize Files tab** — the one feature that touches disk. Mandatory dry-run, per-item collision handling (never overwrites), per-item failure isolation, incremental JSON move log, and a self-contained HTML report with a "Needs attention" section up top. Move *or* copy; optionally archive the installer file itself (`Setup.exe` → `Setup.7z`) while leaving sidecars (readme, crack, theme, serial…) exactly where they were. Portable-tagged apps can be routed into a dedicated `Portable/` tree.

**Monitor**
Point the app at one or more "drop folders" — new `.exe`/`.msi`/archive files land there, get identified, get proposed a match against your existing catalog (or flagged for a new app), and after a review of the dry-run plan are archived + moved into the organized tree. A batched post-attach Winget scrape keeps metadata fresh without stalling the file loop.

**Clean library**
The inverse of a scan: verify that everything *already in* the catalog from a given scan root still exists on disk — for apps you deleted, moved, or replaced outside this app. Reviewed before removal, logged, HTML report generated.

**Import / export**
CSV export (optionally batched) of one row per app for review or bulk editing in a spreadsheet; CSV import applies changes back by app ID.

---

## How it works

Two layers, deliberately kept separate:

- **`raw_candidates`** — pure structural output of the scanner. "What did we find on disk, and what does each file look like?" Never rewritten by resolve.
- **`apps` + `variants`** — the clean, canonical entity. One app, many variants (versions / editions / architectures / languages).

Scanning never mutates existing resolve output; resolving never re-touches the filesystem. Re-running resolve with new settings is a read of `raw_candidates` and an upsert of `apps`/`variants`, matched by a normalized key so it's idempotent. Any field you manually edit is *locked*, so a subsequent re-resolve won't silently overwrite your correction.

The name-cleaning pipeline itself is a documented multi-stage cascade (bracket-strip → website tags → release tags → ignore words/patterns → build numbers → version → architecture → edition → language → tidy → smart-case → synonyms). Every stage is editable from Settings > Filters. All of it is deterministic and inspectable — no ML.

---

## Installation

**Requires Python 3.10+ on Windows.** Non-Windows may work for the GUI + scan/resolve/scrape paths, but PE metadata reading, the `winget` integration, the `rar` fallback, and `explorer /select` integration are Windows-specific.

```bash
git clone https://github.com/<your-username>/app-catalog.git
cd app-catalog

python -m venv .venv
.venv\Scripts\activate

pip install PySide6 rapidfuzz requests beautifulsoup4

# Optional — enable extra features:
pip install pefile          # read PE version resources from .exe files
pip install py7zr           # 7z archive inspection + compression
pip install rarfile         # .rar listing (also needs unrar/7z on PATH)
pip install pycdlib         # .iso inspection
```

Run it:

```bash
python run_gui.py                # opens/creates catalog.db next to run_gui.py
python run_gui.py D:\other.db    # use a different catalog file
```

---

## Quick start

1. **Add scan root** → point it at a folder of installers. Scan + resolve run automatically.
2. **Review** — filter by `needs_review`, check the detail panel, use the alternative-name buttons, mark verified. Every manual edit locks that field.
3. **Scrape all (Winget)** — background-enriches every resolved/verified app from the locally-cached manifest. Hit **Manual-Scrape** on a single app when the auto-match misses.
4. **Organize** — open the Organize dialog. Check **Duplicates** to collapse accidentally-split apps. Check **Categories** to normalize the taxonomy. Check **Report** for a health overview. When you're ready, use **Reorganize Files** — preview first, review the plan, then execute.
5. **Monitor** (optional) — configure a drop folder in Settings, then hit **Monitor…** on the toolbar whenever you want to process new arrivals.

---

## Building a standalone `.exe`

Two PyInstaller spec files are included: `app_catalog_onedir.spec` and `app_catalog_onefile.spec`. Both produce a `console=True` build (the console window is a useful live view of long scans) and stamp `ico.ico` on the exe.

```bash
pip install pyinstaller
pyinstaller --clean app_catalog_onedir.spec     # → dist/AppCatalog/
pyinstaller --clean app_catalog_onefile.spec    # → dist/AppCatalog.exe
```

The runtime window/taskbar icon is set by `run_gui.py` via `QApplication.setWindowIcon()` plus a Windows `AppUserModelID` — that's what makes Windows show `ico.ico` on the taskbar instead of the Python icon. `ico.ico` is bundled as a data file in both specs.

At runtime, `catalog.db`, `logs/`, and `manifest/` are anchored to the folder containing the `.exe`, not the current working directory — so the whole `dist/AppCatalog/` folder (or the single onefile `.exe`'s folder) stays self-contained and portable.

---

## Where things live

```
<catalog folder>/
    catalog.db                     ← everything: apps, variants, settings, audit log
    logs/
        app.log                    ← general application log (rotating)
        reorganize_log_*.json      ← reorganize run logs
        monitor_log_*.json         ← monitor run logs
        clean_library_log_*.json   ← clean-library run logs
        *_report_*.html            ← shareable HTML reports
    manifest/
        winget_manifest_cache.json ← cached Winget index (delete to force refresh)
        winutil_apps_cache.json    ← cached winutil applications list
```

---

## Design notes

- **SQLite + WAL.** A background scan/resolve can write while the GUI reads, without locking the table.
- **Single-file catalog.** Settings, scan roots, audit log, and every scraped field live in the same `.db` file. Copy the file, take the whole catalog.
- **Live settings.** Every threshold, keyword list, regex, and enabled-flag is editable in the GUI and takes effect immediately — no restart. (Exception: the UI scale multiplier, which Qt reads before the window is created.)
- **Locked fields.** Editing a name, catalog, or subcatalog locks it. Re-resolve will never overwrite a lock.
- **Everything is audited.** Manual edits, merges, splits, category operations, and clean-library removals all write to `audit_log`.
- **Safety on anything destructive.** Reorganize and Monitor both require an explicit preview + confirmation. Every file operation is collision-safe (never overwrites). Failures are per-item, not per-batch. A JSON move log is written incrementally *before* each item is processed, so a mid-run crash still leaves a complete record of intent.
- **Offline-first.** Scanning, resolving, organizing, reporting, and the Winget manifest lookup all work without a network connection. Only the initial manifest download, Chocolatey search, and `winget show` need one.

---

## Known limitations

- **Windows-focused.** PE metadata, `winget`, and `explorer /select` integration won't work elsewhere. The scanner/resolver/GUI are otherwise OS-agnostic.
- **RAR writing needs an external `rar` binary.** Python has no library that can *write* the proprietary rar format. The 7z and zip backends are pure-Python-or-stdlib.
- **Manifest-sourced publisher/description are heuristics.** The bare Winget index has no real publisher/description fields — the app guesses a company name from the PackageId's first segment and synthesizes a one-line description. For real metadata, enable `scraper_winget_show_for_auto_scrape` or use the Manual-Scrape dialog.
- **No automatic undo button.** Reorganize/monitor move logs have everything you need to reverse a run by hand, but there's no one-click revert.
- **Fuzzy clustering is a heuristic.** It's tuned to favor precision over recall. The Duplicates tab exists precisely because no automatic threshold is right for every catalog.

---

## Requirements

| | |
|---|---|
| Python | 3.10+ |
| GUI | PySide6 |
| Required deps | `rapidfuzz`, `requests`, `beautifulsoup4` |
| Optional deps | `pefile` (PE metadata), `py7zr` (7z), `rarfile` + external `unrar`/`7z` (rar), `pycdlib` (iso) |
| Optional external tools | `winget` (for `winget show` enrichment), `rar` / WinRAR (for rar *writing*) |

---

## Status & license

This is a personal-scale tool that grew into a fairly complete one. It's stable enough for daily use on catalogs of a few thousand apps, and the destructive operations (reorganize, monitor, clean library) all default to dry-run-then-confirm.

Parts of the codebase were written with AI assistance and then reviewed and reworked module by module; several specific design decisions in `app_manager.py`, `monitor.py`, and the name-cleaning pipeline are documented inline as coming from a side-by-side comparison of two parallel implementations.

Licensed under the MIT License — see `LICENSE`.

---

## Contributing

Bug reports and pull requests are welcome. If you're filing a bug about naming or clustering, please include the raw folder/file name and what you expected the resolver to produce — the pipeline is entirely settings-driven, so a specific example is usually the fastest path to a fix.
