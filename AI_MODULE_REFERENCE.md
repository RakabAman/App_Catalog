# AI Module Reference

This file exists specifically so **any AI model working on this codebase**
(Claude, DeepSeek, or anything else) can get oriented quickly without
re-reading every source file end to end. Read this FIRST, then open only
the specific file(s) relevant to the task at hand.

**Keep this file up to date.** Any change that adds/removes/renames a
function, class, config key, or DB column should update the relevant
section here in the same change. `PROGRESS.md` is the detailed,
chronological build log (what changed, why, what was tested) -- this file
is the current-state map (what exists, where, and how it fits together).
Read `PROGRESS.md` for history/reasoning behind a specific decision; read
this file for "where do I make this change".

## Why this file exists

This project has been developed collaboratively across two different AI
builds (this one, originally by Claude; a separate parallel build by
DeepSeek). A structural review of the DeepSeek build informed several
features merged into this one (see `PROGRESS.md`'s "Checkpoint 15" entry
for the full comparison and reasoning). The project was deliberately
**flattened to 12 top-level `.py` files, no subfolders** specifically so
it can be handed to either AI as individual files (a zipped package can't
be uploaded to some AI chat interfaces, and a deep folder structure makes
it harder for a model to build a mental map from a partial upload). Keep
it this flat unless there's a strong reason not to.

Two non-`.py` files now live alongside the 14 modules:
`app_manager.spec` (PyInstaller build spec -- checkpoint 24) and
`requirements.txt`. Neither is part of the "flat, no subfolders" module
map below; both are build tooling, not app code.

---

## 1. File map (14 files, no subfolders)
run_gui.py Entry point. python run_gui.py [path/to/catalog.db]
config.py Pure data: DEFAULT_SETTINGS dict. No logic.
app_paths.py PyInstaller-safe path resolution: app base dir, db path, logs/, manifest/ (checkpoint 21).
database.py SQLite schema + connection + settings read/write.
scanner.py Filesystem walk -> raw_candidates. Never mutates named data.
resolver.py raw_candidates -> apps + variants (naming/clustering logic).
scraper.py Winget manifest + winget-show + Chocolatey/Winget enrichment orchestration.
choco_search.py Chocolatey live-search HTML scraping (standalone, deliberately kept separate).
app_manager.py Cross-catalog tools: duplicates, category rename, structural report, physical reorganize.
app_organizer.py OrganizeDialog (Qt) -- the UI half of app_manager's features.
monitor.py Manual "Monitor" job: watch folders -> dry-run plan -> compress + move into organized structure.
html_report.py Shared self-contained HTML report renderer, used by app_manager.py and monitor.py (checkpoint 21).
gui_backend.py AppsTableModel + all QThread workers + CSV import/export.
gui_main.py Main window, detail panel, and all dialogs (imports gui_backend).

text

What used to be one `gui.py` is now split into `gui_backend.py` (non-UI:
table model, worker threads, CSV I/O) and `gui_main.py` (all widgets and
dialogs). `monitor.py` was added in checkpoint 18 as a deliberately
self-contained module -- it owns its own worker thread and three private
dialogs, and exposes exactly one public name (`MonitorJob`) so
`gui_main.py` doesn't have to know anything about how it works.
`app_organizer.py` was split out from `gui_main.py` for the same reason.
`app_paths.py` and `html_report.py` were added in checkpoint 21, both as
small, deliberately dependency-light leaf modules -- see their own
docstrings, and PROGRESS.md's checkpoint 21 entry, for the full why.

**Dependency direction** (no cycles): `config` <- `database` <- `scanner`,
`resolver` <- `scraper`, `app_manager` <- `app_organizer`, `gui_backend` <-
`monitor`, `gui_backend` <- `gui_main`, `app_paths` <- `run_gui`/`scraper`/
`app_manager`/`monitor` (a leaf module, imported by four others, imports
nothing from this project itself), `html_report` <- `app_manager`/`monitor`
(also a leaf, imported lazily inside the two report-generating functions
rather than at module level -- same pattern `choco_search` already used).
(Checkpoint 20: corrected -- this
used to say `monitor <- gui_backend`, backwards from the actual code;
`monitor.py` imports `AppPickerDialog` FROM `gui_backend.py`, not the
other way around. Still no cycle: `gui_backend.py` never imports
`monitor.py`.) `choco_search` is used only by
`scraper.py` (imported lazily inside a function, not at module level, so
it's optional at import time). `monitor.py` imports `resolver.extract_fields`
and `resolver.normalize_key` directly (needs the same name-extraction
pipeline the scanner uses) but nothing else from the pipeline modules.
All imports are **flat** (`from database import Database`, not
`from .database import Database`) -- this is not a package, there's no
`__init__.py`, and files are run as plain top-level modules from whatever
directory they live in.

### Pipeline (how data flows through the modules)
run_gui.py
-> gui_main.MainWindow
-> scanner.run_scan() walks a folder tree, writes raw_candidates
-> resolver.run_resolve() reads raw_candidates, writes apps + variants
-> scraper.run_scrape() reads apps, writes back publisher/description/
tags/winget_id/choco_id/etc onto apps
-> app_manager.* reads/writes apps + variants for cross-catalog
cleanup (duplicates, category rename, physical
file moves) -- independent of the above three,
can run any time after resolve has produced apps
-> monitor.MonitorJob scans user-configured "watch" folders, matches
candidates against existing apps (or creates
new apps), compresses+moves into the
organized structure. Shares the _active_worker
lock with the other jobs so only one can run
at a time.

text

`raw_candidates` is **permanent history, never mutated** by the resolver --
re-resolving means re-reading this table with new logic/settings, not
re-scanning the filesystem. This is why "Re-resolve all" is fast and safe
to run repeatedly after a settings change.

---

## 2. `database.py` -- schema + connection

Single SQLite file holds EVERYTHING: schema, settings, scan history, apps,
variants, tags, audit log. No external config file, no separate settings
store.

### Tables

| Table | Purpose |
|---|---|
| `settings` | key -> JSON-encoded value. Live, editable from Settings dialog, no restart needed. |
| `settings_version` | Bumped whenever a resolution-affecting setting changes. `apps`/`variants` record which version produced them. |
| `scan_roots` | One row per folder root ever scanned. `last_scan_started_at`/`last_scan_finished_at`/`last_scan_status` track re-scan state. |
| `raw_candidates` | Scanner output. One row per detected install unit (folder + primary file). PE metadata, archive-inspection bookkeeping, and a fingerprint (for incremental-rescan skip) live here. **Never mutated by the resolver.** |
| `scan_errors` | Folders the scanner couldn't read (permission denied, path too long). Surfaced in the GUI after a scan so failures aren't silent. |
| `apps` | The clean, canonical, user-facing entity. See column list below. |
| `variants` | One row per raw_candidate that got assigned to an app -- a specific version/edition/architecture/language combination. |
| `tags` / `app_tags` | Many-to-many. An app can have multiple tags (catalog+subcatalog are auto-synced here too, plus scraper-sourced tags). |
| `scrape_cache` | Reserved, not actively used by the current scraper implementation (which writes straight to `apps`). |
| `audit_log` | Free-form JSON log of merges/renames/edits, for traceability. |

### `apps` columns worth knowing

Core: `id`, `name`, `catalog`, `subcatalog`, `status`
(resolved|needs_review|verified|ignored), `confidence`, `normalized_key`,
`name_locked`, `catalog_locked`, `subcatalog_locked`, `alt_name_candidate`,
`alt_name_source`.

Scraper-added (checkpoints 12-14): `winget_id`, `choco_id`, `manifest_name`,
`alt_source_name` (pre-auto-rename name -- NOT the same field as
`alt_name_source` above; that one is about the resolver's naming cascade,
this one is about scraper auto-rename), `latest_version`, `last_scraped`,
`scrape_status` (not_scraped|pending|scraped|failed), `license`, `publisher`,
`description`, `homepage_url`.

### Key methods

- `Database(path)` / `.connect()` / `.close()` -- standard lifecycle,
  `check_same_thread=False` so it's safe to reuse across the GUI thread and
  worker QThreads. Each worker actually opens its OWN `Database(path)`
  instance rather than sharing a connection across threads -- see
  `gui_backend.py`'s `*Worker` classes.
- `.init_schema()` -- idempotent, safe to call every startup. Runs
  `_run_migrations()` (additive `ALTER TABLE` list -- see that method for
  the pattern to follow when adding a column), then `_ensure_defaults()`
  (seeds `DEFAULT_SETTINGS` keys that don't exist yet), then
  `_merge_new_keyword_defaults()` (unions newly-added default LIST values
  into a fixed set of existing keyword-list settings on an old DB, so e.g.
  adding a new default noise-word doesn't require the user to manually
  re-add it -- see that method's docstring for the exact key list).
- `.get_setting(key, default)` / `.get_all_settings()` /
  `.set_setting(key, value, bump_version=True)` -- `bump_version=False`
  for settings that don't affect resolution logic (UI prefs, scraper
  config) vs `True` (default) for anything that changes how names get
  resolved.

---

## 3. `scanner.py` -- filesystem walk

Read-only with respect to the scanned drive (never writes/moves/deletes
source files -- see `app_manager.py` for the one feature that does, and
note its much stricter safety requirements).

### Key pieces

- `walk_scan_root(root, settings, ...)` -- the main entry, does an
  `os.walk`-style traversal, classifying every folder via
  `classify_folder()` and building `ScanCandidate` objects.
- `classify_folder(folder_path, file_names, subfolder_names, settings, depth)`
  -- returns a `Classification` (`unit_type`: noise|container|install_unit|
  unresolved). This is where `noise_folder_keywords`/
  `noise_short_only_keywords` skip-folder logic lives.
- `_group_installer_files()` / `_installer_group_key()` -- groups files
  within one folder into installer "groups" (multi-part archives collapse
  into one group; every other distinct file is its own group), so a
  folder with `setup.exe` AND `isdel.exe` AND multiple version-numbered
  installers each become their own `raw_candidates` row rather than one
  folder producing only one candidate.
- `read_pe_metadata(exe_path)` -- optional (off by default,
  `read_exe_metadata_enabled` setting), reads real Product Name/Version/
  Company from a PE file's version resource via `pefile`.
- `list_archive()` / `extract_and_inspect()` -- archive content
  inspection, cheap listing first, escalates to real extraction only when
  ambiguous (see `_evaluate_ambiguity()`).
- `_derive_catalog_subcatalog(root, folder_path, settings)` -- computes
  catalog/subcatalog from the relative path. Checkpoint 15: now checks
  `_apply_taxonomy_rules()` against `category_rules`/`subcategory_rules`
  settings FIRST (regex-against-full-relative-path, first-match-wins),
  falling back to the original "first path segment = catalog, second =
  subcatalog" behavior when no rule matches or none are configured. Empty
  rule lists by default -- zero behavior change until rules are added.
- `compute_folder_fingerprint()` -- used for incremental re-scan
  (unchanged folders are skipped on a re-scan of an existing root; see
  `ScanRootsDialog` in `gui_main.py` for the UI to trigger this).

---

## 4. `resolver.py` -- naming, version extraction, clustering

The most heavily-tuned file in the project (checkpoints 7-11 are almost
entirely resolver accuracy fixes). **Read `PROGRESS.md`'s checkpoint
history before changing the name-cleaning pipeline** -- several past
"obvious improvements" (e.g. a camelCase-acronym-split rule) were tried,
found to regress OTHER real app names, and reverted; that history exists
so the same mistake isn't repeated.

### The naming cascade -- `extract_fields()`

For one `raw_candidate`, up to 4 candidate NAME sources are built (PE
product name, leaf folder name, primary file stem, parent folder name),
each independently cleaned through `_process_candidate()`, then scored;
the highest-scoring valid one wins. See `_process_candidate()`'s
docstring / `config.py`'s big comment block above `DEFAULT_SETTINGS` for
the exact ordered list of cleaning steps (bracket-content strip -> website
tags -> release tags -> portable/build-number detection -> version
parsing -> tidy/camelCase/digit-run split -> ignore-words -> architecture
-> edition (ALL matches stripped, not just first) -> language -> leftover
numeric folding -> tidy/smart-case -> **`_apply_name_synonyms()`**
(checkpoint 15, LAST step, regex-based known-misspelling/rebrand rewrite
via the `app_name_synonyms` setting)).

Key functions:
- `extract_fields(...) -> ExtractedFields` -- the cascade itself.
- `_process_candidate()` -- runs one candidate through the full cleaning
  pipeline, returns a `_CleanResult` (clean_name, version, edition,
  architecture, language, attributes, is_portable).
- `_is_valid_name(name, settings)` -- disqualifies empty/too-short
  (<3 chars)/purely-numeric/ignore-listed names, causing the cascade to
  fall through to the next candidate.
- `_apply_name_synonyms(name, settings)` -- checkpoint 15, applies
  `app_name_synonyms` regex rules (adopted from the DeepSeek build) as the
  final cleaning step.
- `normalize_key(name)` -- the aggressive normalizer (lowercase, strip all
  non-alphanumerics) used for BOTH clustering (`cluster_candidates`) and
  scraper manifest lookup (`scraper.py`'s `build_lookup`/
  `_lookup_key_for_app`) -- always import this one shared function rather
  than reimplementing normalization elsewhere.

### Checkpoint 20 fix -- `parent_folder` no longer beats a valid shallow folder
If the scored winner's source is `parent_folder` (the catalog/subcatalog
folder one level up -- always the weakest source, `source_bonus=1`), and
the depth<=2-disqualified `folder` candidate at the install unit's own
level has a genuinely valid name, that folder name is used instead. Fixes
a real regression where a 2-level `Catalog/AppName/setup.exe` layout (no
separate subcatalog folder -- common) with a generic installer filename
resolved to the CATALOG name (e.g. "Graphics") instead of the app name.
Deliberately a no-op for the original "Burners" case. See PROGRESS.md
checkpoint 20 for the full repro/fix/regression-test writeup.

### Clustering -- `cluster_candidates()`

Groups `ClusterMember`s (one per raw_candidate's extracted fields) into
`Cluster`s (-> apps) by `normalize_key()` equality OR
`rapidfuzz.fuzz.token_sort_ratio` >= `fuzzy_match_threshold` (default 88)
OR a length-guarded prefix match. Canonical name/edition/etc within a
cluster picked by `_majority()` (most common value) with confidence as
tiebreak.

### Apply-layer functions (called directly by the GUI, no batch job)

`edit_app_field`, `unlock_app_field`, `set_app_status`, `merge_apps`,
`move_variant_to_app`, `split_variant_to_new_app`, `set_variant_ignored`,
`delete_variant`, `propose_reresolve_app` / `apply_reresolve_app` (preview
+ apply a single app's re-resolution against current settings, shown via
`ReresolveDialog` in `gui_main.py`).

---

## 5. `scraper.py` -- Winget manifest + winget-show + enrichment orchestration

Merged from three former files (manifest loading / winget-show CLI /
enrichment orchestration) into one, per the flattening goal. Internally
still organized as three clearly-marked parts (search `# Part 1` /
`# Part 2` / `# Part 3` in the file).

### Part 1 -- Winget manifest (svrooij/winget-pkgs-index) + winutil

- `load_manifest(db_path, settings, force_refresh=False) -> ManifestLoadResult`
  -- cache-first (JSON file in `manifest/` next to `catalog.db` --
  checkpoint 21, see `app_paths.get_manifest_dir()`; used to be bare next
  to `catalog.db` directly, if you see that claim anywhere it's stale),
  staleness controlled by `scraper_manifest_staleness_hours`, falls back
  to a stale cache on network failure rather than failing outright.
- `load_winutil_apps(db_path, settings, force_refresh=False) -> dict` --
  same caching pattern for ChrisTitusTech/winutil's `applications.json`,
  keyed by lowercased winget id. Richer per-app metadata (description,
  category, homepage link, choco id) than the bare svrooij manifest, so
  `run_scrape()` tries this FIRST and only falls back to the manifest.
- `build_lookup(raw_entries) -> dict[normalize_key(name) -> ManifestEntry]`
  -- EXACT key lookup only. The svrooij manifest is a flat JSON array of
  `{Name, PackageId, Version, Tags, LastUpdate}`, ~14k entries, NO
  description/publisher/license fields at all (hence the generic
  `"<name> - Windows application."` placeholder description -- see
  Part 2 for the real fix).
- `search_manifest(lookup, term, max_results) -> list[dict]` -- the
  MANUAL fuzzy/substring search (offline, searches the already-loaded
  lookup, no network call) used by `gui_main.py`'s `SearchMatchDialog`
  for cases the exact-key lookup misses (e.g. app resolved to "VLC",
  manifest entry is "VLC media player"). Tiered scoring: exact key >
  starts-with > whole-word-substring > raw substring >
  `rapidfuzz.fuzz.WRatio` fallback.

### Part 2 -- `winget show` CLI fallback

- `fetch_winget_show(winget_id, timeout) -> Optional[WingetShowResult]`
  -- shells out to the real `winget show --id <id> --exact
  --accept-source-agreements --disable-interactivity`, parses
  Publisher/Description/Homepage/License/Version/Tags from the text
  output. Returns `None` gracefully if the CLI isn't installed
  (`is_winget_available()` checks first).
  **IMPORTANT CAVEAT, partially resolved (checkpoint 23)**: this
  finally got its first real run on an actual Windows machine, and it
  DID surface a real bug -- `subprocess.run(..., text=True)` with no
  explicit `encoding=` decodes using the platform default (`cp1252` on
  Windows, not UTF-8), and `winget show`'s real output is UTF-8 and
  regularly contains characters outside cp1252's range, crashing the
  whole scrape run with a `UnicodeDecodeError` from a background reader
  thread. Fixed by passing `encoding="utf-8", errors="replace"`
  explicitly -- **any future `subprocess.run(..., text=True)` call added
  anywhere in this codebase must do the same**, never rely on the
  platform default. The label-parsing logic itself (`_find_label_value()`
  etc.) has NOT yet been confirmed against real `winget show` output --
  only the encoding crash has been fixed and verified (via a fake
  `winget` executable reproducing the exact reported byte). Still worth
  a close look the next time real winget-show output is available.

### Part 3 -- enrichment orchestration

- `run_scrape(db, app_ids=None, ...) -> ScrapeResult` -- BATCH path.
  Per-app source priority: winutil (by `winget_id`, if the app already
  has one) > svrooij manifest (by `normalize_key`). Optional per-app
  `winget show` enrichment gated by `scraper_winget_show_for_auto_scrape`
  (OFF by default -- one subprocess call per app could add real time
  across hundreds of apps). `app_ids=None` restricts to
  `scraper_auto_enrich_statuses` (default: resolved/verified only, skips
  needs_review/ignored). `winget show` is ALSO used as a fallback
  whenever the description from either source is still the generic
  `"<name> - Windows application."` placeholder.
- `apply_manifest_candidate(db, app_id, candidate, choose_name, settings)`
  / `apply_choco_candidate(db, app_id, candidate, choose_name)` -- MANUAL
  path, one user-picked candidate applied to one app. ALWAYS attempts one
  `winget show` call (manifest path only) regardless of the batch
  setting, since it's a single call for a confirmed pick.
- Merge priority for description/publisher/homepage/license, wherever
  multiple sources could supply a value: `winget_show result` >
  `winutil entry` > `manifest placeholder / choco candidate value` >
  `existing stored value`.

---

## 6. `choco_search.py` -- Chocolatey live search

Deliberately kept as its OWN file rather than merged into `scraper.py` --
this is the piece most likely to need standalone tweaking if Chocolatey's
own site HTML changes, and keeping it separate means it can be swapped
out without touching the rest of the scraper logic. User-provided/
maintained; `gui_main.py`'s `ChocoSearchWorker` imports
`search_chocolatey(search_term, max_results) -> list[dict]` from it
lazily (inside a function, not at module level).

Returned dict shape (also what `manifest.search_manifest()` and
`apply_manifest_candidate`/`apply_choco_candidate` all standardize on, so
the GUI can treat a Winget result and a Chocolatey result identically):
`name`, `choco_id`/`id`, `version`, `description`, `company`, `website`,
`tags` (list), `match_percent`, `deprecated`.

---

## 7. `app_manager.py` -- cross-catalog hygiene tools

**New in checkpoint 15**, adopted from a side-by-side review of a
parallel DeepSeek build of this app (see `PROGRESS.md` checkpoint 15 for
the full comparison of what was/wasn't adopted and why). Four independent
groups:

1. **`find_duplicate_groups(db, fuzzy_threshold=None) -> list[DuplicateGroup]`**
   -- read-only. Exact `normalized_key` collisions + fuzzy near-misses
   (same threshold as clustering, by default) that never merged at
   resolve time. Nothing merges automatically; the GUI's
   `OrganizeDialog` shows groups for the user to confirm, then calls
   `resolver.merge_apps()` for real. Checkpoint 17 added a standalone
   `_dup_normalize()` (NOT influenced by resolver settings) used for the
   primary detection pass, plus a multi-pass detection strategy
   (exact-fixed-key, exact-base, exact-normalized, fuzzy-base, token-set,
   partial-ratio). Each `DuplicateGroup.members` entry carries full app
   detail (id, name, catalog, subcatalog, variant_count, sample_paths).
2. **`rename_catalog(db, old, new) -> int`** /
   **`rename_subcatalog(db, new, old, catalog=None) -> int`** -- bulk
   `UPDATE` across every affected app. Renaming to an already-existing
   name IS the merge operation (plain text column, no FK table to
   reconcile).
3. **`generate_structural_report(db) -> StructuralReport`** -- read-only
   diagnostics: apps whose variants are spread across multiple different
   `scan_roots` (same app, multiple backup locations), subcategory names
   duplicated across different catalogs, and `folder_name_aliases`
   entries no current app actually uses (this schema's closest analog to
   DeepSeek's "orphaned category" concept, adapted because this schema
   has no separate categories table to be orphaned from).
4. **`preview_reorganize(db, dest_root) -> list[PlannedMove]`** /
   **`execute_reorganize(db, planned_moves, ...) -> ReorganizeResult`**
   -- **the one feature in this whole app that writes to the scanned
   drive.** Moves each variant's source folder to
   `dest_root/Catalog/Subcatalog/AppName/Version/`. Safety properties
   (all real, all tested against an actual filesystem -- see
   `PROGRESS.md` checkpoint 15 for the test transcript):
   - `preview_reorganize` is **pure** -- computes the plan, flags
     collisions, touches nothing on disk.
   - `execute_reorganize` takes EXACTLY the plan `preview_reorganize`
     returned (never recomputes it), so a caller can't accidentally
     execute a stale/unreviewed plan.
   - A destination that already exists is **always skipped, never
     overwritten** -- rechecked at execute time even if the preview's
     `collision` flag said otherwise, in case time passed.
   - A persisted JSON move log (`reorganize_log_<timestamp>.json`, next
     to `catalog.db` by default) is written incrementally (before each
     move starts, and updated with the result) so a crash mid-run still
     leaves a complete record for manual audit/undo.
   - `variants.source_path` is updated on a successful move.
   - `progress_callback(index, total, plan, status)` is invoked after
     every item regardless of outcome, so the GUI's `_ReorganizeWorker`
     can drive a live progress bar / activity log without this module
     knowing anything about Qt.
5. **`scan_for_missing_sources(db, root_path=None) -> list[MissingItem]`**
   / **`execute_clean_library(db, missing_items) -> CleanLibraryResult`**
   -- **checkpoint 22, "Clean library".** Deliberately the OPPOSITE of a
   scan/re-scan: never looks for new install units, only confirms
   variants ALREADY in the catalog still exist (`os.path.exists()` per
   variant, scoped to variants under `root_path` when given). Removal
   deletes both the `variants` row and its `raw_candidates` row -- the
   latter matters, or a future Resolve run without a fresh Scan first
   could silently re-create the exact variant just removed, since
   Resolve reads `raw_candidates` from the DB, not the live filesystem.
   An app left with zero variants after removal is deleted outright (FK
   cascades handle its tags etc.). Read-only scan / destructive-but-
   DB-only execute, same two-step shape as reorganize -- see
   `PROGRESS.md` checkpoint 22.

---

## 8. `app_organizer.py` -- OrganizeDialog (Qt)

The UI half of `app_manager.py`'s features (kept as its own file so
`app_manager.py` stays importable from `monitor.py` without pulling Qt
into a non-GUI context). Four tabs: **Duplicates** (checkpoint 17
`QTreeWidget`, editable checkboxes per row/group), **Categories** (live
catalog/subcatalog tree + board view, rename/move/promote/demote/merge
context menus), **Report** (collapsible tree from
`generate_catalog_report()`, double-click a finding to jump to that app
in the main table, export as Markdown / CSV / clipboard), and
**Reorganize Files** (the physical-move tab, backed by
`_ReorganizeWorker(QThread)` so a big batch never freezes the GUI).
`OrganizeDialog`'s `closeEvent` blocks closing the dialog while a
reorganize is still running (the worker is parented to the dialog, so
letting it get torn down mid-run would kill a QThread that's still
moving files).

---

## 9. `gui_main.py` + `gui_backend.py` -- all UI (PySide6/Qt)

Note: this section describes what used to be one `gui.py`. It's now split
into `gui_backend.py` (table model, worker QThread subclasses, CSV
export/import -- no widgets beyond the model) and `gui_main.py` (every
widget, dialog, and the main window). The dependency is one-way:
`gui_backend` does not import `gui_main`.

Organized top-to-bottom in `gui_backend.py` as:

1. `AppsTableModel` (`QAbstractTableModel`) -- backs the main apps table.
   `COLUMNS` (module-level list right above/near this class) is the
   single source of truth for which columns exist and their DB-column
   keys/display labels -- add a column here AND to the SQL query in
   `.refresh()` if adding a new one. `.sort()` is a REAL override (the
   base Qt class's `sort()` is a no-op by default -- this was a bug fixed
   in checkpoint 11, don't remove the override). `catalog_filter` /
   `subcatalog_filter` (checkpoint 22) both apply as plain `AND a.catalog
   = ?` / `AND a.subcatalog = ?` in `.refresh()`'s query --
   `subcatalog_filter` is only ever set together with a matching
   `catalog_filter` (see `MainWindow._current_catalog_selection()`).
   `catalog_subcatalog_tree()` returns `{catalog: [subcatalog, ...]}` for
   building the left-panel tree. **Row status colors (checkpoint 22)**:
   `_row_status_key(row)` maps `(status, scrape_status)` to one of
   `needs_review` / `scrape_failed` / `not_yet_enriched` / `ignored` /
   `None` (done -- verified or scraped, no override); `_STATUS_COLORS`
   pairs an explicit (background, foreground) `QColor` for each -- ALWAYS
   pair both, never just a background, or the row becomes unreadable
   on whichever system theme (light/dark) wasn't tested against (this is
   exactly what "yellow too bright, text invisible" turned out to be:
   default system text color combined with a hardcoded pale background).
2. Worker `QThread` subclasses: `ScanWorker`, `ScanAndResolveWorker`,
   `ResolveWorker`, `ScrapeWorker`, `ChocoSearchWorker`,
   `WingetSearchWorker`, `CleanLibraryScanWorker` (checkpoint 22,
   wraps `app_manager.scan_for_missing_sources()`) -- all follow the
   same shape (own `Database(path)` instance constructed INSIDE the
   thread, never share the GUI thread's connection object across
   threads; `progress`/`finished_ok`/`failed` signals). Follow this
   exact pattern for any new long-running/network-touching operation.
3. CSV export/import: `export_apps_csv`, `import_apps_csv`,
   `EXPORT_FIELDS`.

Organized top-to-bottom in `gui_main.py` as:

1. `DetailPanel` -- the right-hand panel. Contains:
   - **App group** (editable name, catalog/subcatalog, status,
     description):
     - **Name**: a `QLineEdit` with larger font (12pt) and a lock
       indicator.
     - **Catalog & Subcatalog**: **editable `QComboBox`es** that are
       dynamically populated with distinct values from the `apps` table.
       Users can select an existing value or type a new one; changes
       commit on focus-out/Enter (`editingFinished`) or explicit dropdown
       pick (`activated`) -- NOT on `currentTextChanged`, which would
       write one UPDATE + one audit_log row per keystroke. Committing a
       change updates the dropdown list for future uses.
     - **Status** and **Scrape source** labels.
     - **Description**: read-only `QTextEdit` (placeholder for scraped
       metadata).
   - **Scraper metadata group** (read-only fields): Publisher, Homepage
     (spans full width), License, Latest version, Winget ID, Chocolatey
     ID, Manifest name, Name before auto-rename, Tags (word-wrapped
     label), Scrape status.
   - **Action buttons**: "Mark verified" and "Re-resolve this app"
     (opens `ReresolveDialog`).
   - **Variants table** (QTableWidget) with columns: Version, File Name,
     Edition, Type, Name Source, Scanned (date), Path. Supports sorting,
     column reordering, and a context menu (Open location, Run,
     Re-evaluate, Scrape, Search & match, Ignore).
   - **Spacing & sizing**: The App and Scraper metadata groups have
     increased vertical spacing (8px) and the app name is larger (12pt).
     The homepage field stretches to fill available space.
2. Search dialogs: `SearchMatchDialog` -- one dialog, two possible
   backing workers (`ChocoSearchWorker` / `WingetSearchWorker`), switched
   via a "Source:" dropdown. This REPLACED an earlier separate
   `ChocoSearchDialog` (checkpoint 12) -- if you see any reference to
   `ChocoSearchDialog` anywhere, it's stale, `SearchMatchDialog` is
   current.
3. `AppPickerDialog` -- used by "Move to different app…" on a variant
   (lists every app except the current parent; picked id returned by
   `selected_app_id()` after `exec()`). Note: `db.connect().execute(...)`,
   not `db.cursor.execute(...)`.
4. `ScanRootsDialog` -- lists `scan_roots`, one-click re-scan (reuses the
   existing `_run_scan_and_resolve()` worker path), plus (checkpoint 22)
   a per-row "Clean library" button -> `MainWindow._run_clean_library()`
   -> `CleanLibraryScanWorker` -> `CleanLibraryReviewDialog` (new
   checkpoint 22 dialog, sits right after `ScanRootsDialog` in this file
   -- checkbox-per-row review + "Remove N selected" ->
   `app_manager.execute_clean_library()`, then auto-opens the HTML
   report the same way `OrganizeDialog` does).
5. `ReresolveDialog` -- review/accept a single app's
   `propose_reresolve_app()` diff.
6. `SettingsDialog` -- tabbed: Resolver / Archives / Scan / Scraper /
   Appearance / Keywords / Filters / Advanced / Monitor / (whatever else
   has accumulated -- check the tab list directly, this grows with each
   new settings group). EVERY setting exposed anywhere in `config.py`
   should have a corresponding field here; if you add a config key, add
   its Settings-dialog field in the same change (this has been missed
   before -- checkpoint 11 had to go back and add fields for settings
   introduced a round earlier).
7. `CsvExportDialog` -- batch size + output directory picker.
8. `MainWindow` -- toplevel: toolbar (`_build_toolbar`), the apps-table +
   catalog/subcatalog TREE (checkpoint 22: `self.catalog_tree`, a
   `QTreeWidget` -- was a flat `QListWidget` called `catalog_list`, if
   you see that name anywhere it's stale; selection tracked via each
   node's `UserRole` `{catalog, subcatalog}` dict, not node text, via
   `_current_catalog_selection()`) + detail-panel splitter
   (`_build_central_widget`), and all the top-level action handlers
   (`_run_scan_and_resolve`, `_run_resolve_all`, `_run_scrape`,
   `_run_monitor` (checkpoint 18), `_run_clean_library` (checkpoint 22),
   `_show_apps_context_menu`, `_open_organize_dialog`,
   `_show_column_picker`/`_apply_column_visibility` (checkpoint 15,
   `visible_columns` setting), `_open_settings`, CSV export/import
   handlers).

`MainWindow.select_app_by_id(app_id)` is the "jump to app" API used by
`OrganizeDialog`'s report tab when a finding is double-clicked -- it
clears any active filter first, then selects and scrolls to the row.

---

## 10. `monitor.py` -- manual "Monitor" job

**New in checkpoint 18.** Deliberately self-contained: `gui_main.py`
imports exactly ONE name (`MonitorJob`) from this module and never sees
its worker thread, its plan dataclasses, or its three private dialogs.
This keeps the feature independently tweakable -- a dialog change here
can't break the main window.

Not a background watcher. Runs when the user clicks the "Monitor…"
toolbar button, and shares the same `_active_worker` lock as
Scan/Resolve/Scrape, so only one job runs at a time.

### Two-phase design

**Phase 1 -- PLAN (pure, read-only)** -- `scan_monitor_folders(db, folders,
settings)`:
- Walks each configured folder (single level; not recursive)
- Filters candidates by: extension in `monitor_extensions`, size >=
  `monitor_min_size_mb`, filename not ending in a partial-download marker
  (`monitor_skip_partial_extensions`), mtime quiet for at least
  `monitor_settle_seconds`
- Runs each surviving file's filename through `resolver.extract_fields()`
  (same cascade the scanner uses) to derive a name / version / edition
- Proposes a match against the `apps` table: exact `normalize_key` hit ->
  `match_status="exact"`; fuzzy candidates above `fuzzy_match_threshold` ->
  `"fuzzy"`; nothing -> `"none"`. Returns `list[MonitorPlanItem]`.
- Writes nothing to disk, nothing to the DB.

**Phase 2 -- EXECUTE (writes)** -- `execute_monitor_plan(db, plan,
dest_root, move_mode, ...)`:
- Takes the possibly user-EDITED plan (via `_MonitorPlanDialog`, below)
  and runs it verbatim. Never recomputes matches or destinations -- the
  user reviewed this exact plan, so we honour it.
- Per item:
  - `skip` -> recorded as `skipped_user`, nothing on disk
  - `attach` -> uses the pre-existing `matched_app_id`
  - `create_new` -> inserts a new `apps` row with `status='needs_review'`
    and `name_locked=1`; syncs catalog/subcatalog as tags via
    `_sync_monitor_app_tags`
  - Computes destination `dest_root/Catalog/Subcatalog/AppName/Version/`
  - If the file extension is in `monitor_already_compressed_extensions`,
    move-or-copies it as-is; otherwise compresses it to
    `monitor_archive_format` (`7z`/`zip`/`rar`) first, then (in move
    mode) deletes the source
  - Records a `variants` row with `raw_candidate_id = NULL` (monitored
    files never entered `raw_candidates`) and `name_source = 'monitor'`,
    plus an `audit_log` entry
- **After the loop** (once, batched -- see `touched_app_ids`), runs one
  `scraper.run_scrape(app_ids=sorted(touched_app_ids))` call for every
  app that was attached/created, if `monitor_auto_scrape_on_attach` is on.

### Safety (mirrors `app_manager.execute_reorganize`)

- Destination collisions are ALWAYS skipped, never overwritten --
  rechecked at execute time even if the plan dialog said otherwise.
- Each item is wrapped individually: one failure is recorded and skipped
  rather than aborting the batch.
- A JSON move log is written incrementally next to `catalog.db`
  (`monitor_log_<timestamp>.json`) so a crash mid-run still leaves a
  complete record for audit/undo.
- Two `create_new` rows with the same `normalize_key(name)` in one run
  collapse to a single app (the `created_in_this_run` dict); the second
  file attaches to the first file's just-created app instead of
  duplicating.

### Compression backends (tiered, matching scanner.py's pattern)

- `7z` -> `py7zr` if importable, else external `7z`/`7za` on PATH, else
  falls back to `zip` with the fallback noted per-item.
- `zip` -> stdlib `zipfile` (always available).
- `rar` -> external `rar` binary only (rarely present -- falls back to
  `7z` then `zip`, noting the fallback in the report).

Archive filename = original filename with the extension swapped
(`setup_v2.1.exe` -> `setup_v2.1.7z`). Already-compressed inputs keep
their original extension and are moved as-is.

### Public API (the only thing `gui_main.py` imports)

    job = MonitorJob(parent_window, db, db_path)
    job.progress.connect(slot)      # status text (str)
    job.job_finished.connect(slot)  # MonitorResult | None (None = cancelled)
    job.job_failed.connect(slot)    # error message (str)
    started = job.start()           # False if user cancelled the start dialog
    job.cancel()                    # ask a running job to stop

`gui_main.MainWindow._run_monitor` is the only caller. It treats the
`MonitorJob` instance as its `_active_worker` for the duration.

### Private dialogs

- `_MonitorStartDialog` -- folders table + Add folder…/Delete selected
  buttons, destination root (dropdown + Browse…), move vs copy, archive
  format. `dest_combo` is seeded from `scan_roots`.
- `_MonitorPlanDialog` -- the editable dry-run table (File / Extracted /
  Status / Action / Target App / New Name / Catalog / Subcatalog). Warns
  (but doesn't block) if a "create new" row has no catalog. Even
  auto-matched rows are fully editable.
- `_MonitorReportDialog` -- end-of-run summary + CSV export of the same
  rows.

### Settings

All `monitor_*` keys live in `config.py`'s `DEFAULT_SETTINGS` and are
editable from **Settings > Monitor**. The folder list uses a
`QTableWidget` (one row per folder) with Add folder…/Delete selected,
mirroring the start dialog's UX.

### Known caveats

- `py7zr` may not be installed; the `_compress_7z` fallback chain handles
  it, but the first `7z` run on a machine without it will fall back to
  `zip` and note the fallback in the report.
- `rar` output requires an external `rar` binary; if absent, the chain
  falls back to `7z` then `zip`.
- Monitor-created apps start at `status='needs_review'` so they highlight
  in the main table (yellow background) and land in the existing review
  workflow. The user clicks **Mark verified** to clear the flag.
- Monitor-created apps use the same fallbacks for empty catalog/subcatalog
  as `_resolve_destination` does on disk (`"Uncategorized"` / `"Misc"`),
  so the app row and its on-disk folder structure tell the same story.
- The main table's "Added" / "Last Scanned" columns show blank for apps
  whose only variants came from the monitor (`raw_candidate_id IS NULL`
  means the LEFT JOIN to `raw_candidates` in `AppsTableModel.refresh()`
  finds nothing to take `MIN(first_seen_at)`/`MAX(last_seen_at)` from).
  Not a bug -- a consequence of the design choice to keep monitored files
  out of `raw_candidates`.

---

## 11. `config.py` -- `DEFAULT_SETTINGS`

Pure data, no functions. One big dict, heavily commented inline (the
comments ARE the documentation for each setting -- read them in place
rather than duplicating here, they explain WHY each default is what it
is, not just what it does). Organized into commented sections:
noise/skip-folder keywords, ignore-word lists, version/build-number
patterns, edition/architecture/language keyword lists,
`folder_name_aliases`, **`app_name_synonyms`** (checkpoint 15),
**`category_rules`/`subcategory_rules`** (checkpoint 15), scanner
behavior flags, scraper settings (manifest URL, staleness, auto-rename,
auto-enrich statuses, `winget show` toggle/timeout, winutil URL),
monitor settings (`monitor_*` keys, checkpoint 18), GUI appearance
(`ui_scale_multiplier`).

**Checkpoint 20**: `portable_indicator_words` was used by `resolver.py`
and editable in `SettingsDialog` but was missing from `DEFAULT_SETTINGS`
-- saving Settings even without touching that field silently persisted an
empty list, permanently disabling portable-app detection. Fixed by adding
it here with its real default (`["portable", "paf"]`). Any setting used
anywhere in the code AND exposed in the GUI must be defined here -- this
is exactly the failure mode that rule exists to prevent.

When adding a new setting: add it here with a comment explaining the
default choice, add the corresponding field to `SettingsDialog` in
`gui_main.py`, and if it's a resolution-affecting setting, make sure
whatever reads it does so via `settings.get(key, ...)` with a safe default
(never assume the key exists, since an old DB won't have it until
`_ensure_defaults()`/`_merge_new_keyword_defaults()` in `database.py` run).

---

## 12. `app_paths.py` -- PyInstaller-safe filesystem layout (checkpoint 21)

Four small, pure functions, no project imports (a true leaf module):
- `get_app_base_dir()` -- `sys.executable`'s folder if `sys.frozen`
  (PyInstaller), else this file's own folder. Used ONLY to compute the
  default db path; never assume CWD equals this.
- `resolve_db_path(raw)` -- anchors a relative path to
  `get_app_base_dir()`; returns an already-absolute path unchanged.
- `get_default_db_path()` -- `<app base dir>/catalog.db`.
- `get_logs_dir(db_path)` / `get_manifest_dir(db_path)` -- `logs/` and
  `manifest/` next to WHATEVER db_path actually points at (so a
  poweruser's second catalog on another drive gets its own independent
  pair, not one shared with the first), creating the folder if missing.
- `get_app_log_path(db_path)` -- `<logs dir>/app.log`.

Every caller (`run_gui.py`'s default db path + logging setup,
`scraper.py`'s two manifest cache path functions, `app_manager.py`
`execute_reorganize`'s move-log dir, `monitor.py`
`execute_monitor_plan`'s move-log dir) goes through this module rather
than deriving `os.path.dirname(db.path)` inline the way all four used
to -- see PROGRESS.md checkpoint 21 for exactly what broke before this
existed and why.

---

## 13. `html_report.py` -- shared HTML report renderer (checkpoint 21)

One public function, `render_operation_html_report(*, title, subtitle,
summary_cards, rows, json_log_path=None) -> str` (returns the HTML
string; the caller decides where to write it), plus the `ReportRow`
dataclass callers normalize their own domain data into before calling
it (`name`, `source`, `dest`, `status_label`, `status_class` -- one of
`"good"`/`"warn"`/`"bad"`/`"neutral"`, `detail`). Deliberately knows
NOTHING about reorganize or monitor specifically -- both
`app_manager.generate_reorganize_html_report()` and
`monitor.generate_monitor_html_report()` build their own list of
`ReportRow` from their own result types and hand it to this one shared
renderer, which is why adding a third "batch file operation with a
report" feature later should mean writing another small
`generate_*_html_report()` wrapper, not touching this file.

Self-contained output: inline `<style>`/`<script>`, no CDN, no external
font -- consistent with this app being offline-first end to end. All
filesystem-derived strings (paths, app names) go through `html.escape()`
before being embedded, since the output is a real file opened in a real
browser. Layout is "Needs attention" (failed/skipped rows, WITH the
reason -- the actual thing the old bare-counts-message-box was missing)
first, then a searchable "Everything" table including successes,
toggleable.

Both generator wrappers write their output into `app_paths.get_logs_dir(db.path)`,
next to that run's JSON log, with a matching timestamp
(`..._report_<timestamp>.html` next to `..._log_<timestamp>.json`), and
both GUI call sites (`app_organizer.py`'s `_on_reorg_finished`,
`monitor.py`'s `_MonitorReportDialog`) auto-open the result via
`webbrowser.open(Path(...).as_uri())`.

---

## 14. Testing conventions used throughout this project

There's no formal test suite/CI -- verification has consistently been
**real, targeted, throwaway scripts run against the actual code**,
documented in `PROGRESS.md` per checkpoint (search for "Verified"
headers). Patterns worth continuing:
- Resolver/scanner changes: reconstruct the EXACT real-world case that
  motivated the change (a real folder/file name from an actual scan log)
  and assert the output, not just a synthetic example.
- GUI changes: headless smoke tests via `QT_QPA_PLATFORM=offscreen`,
  constructing real `MainWindow`/dialog instances against a seeded
  temp SQLite DB, not mocked.
- Network-touching code (Winget manifest, `winget show`, Chocolatey):
  test what CAN be tested for real (the manifest fetch/cache/lookup was
  tested against the live GitHub-hosted file), and be EXPLICIT in
  `PROGRESS.md` about what couldn't be (the `winget show` CLI itself,
  Chocolatey's live site) rather than presenting untested code as
  verified.
- A `QMessageBox.question`/similar blocking modal call is NOT reliably
  mockable for headless testing in this environment (confirmed while
  building `OrganizeDialog` in checkpoint 15 -- `unittest.mock.patch`
  against it caused a hang rather than a clean intercept). When a GUI
  method's only untestable part is a one-line native confirm dialog,
  test the surrounding logic directly (call the underlying db/data
  function the confirm branch would have called) rather than burning
  time trying to force the modal through headlessly.
- Regression-test the SPECIFIC named cases from prior checkpoints
  (WinGlobe/isdel, DAEMON Tools clustering, ACDSee not being
  camelCase-split, K-Lite, etc.) after any resolver/scanner change --
  they're cheap to re-run and this project has a real history of one
  fix quietly breaking an earlier one.