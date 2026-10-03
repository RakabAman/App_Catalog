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

## 1. File map (17 files, no subfolders)
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
app_manifest.py Variant manifests (<app> <version>.appcatalog.json): schema/triggers, writer, auto-flush, reader, resolver apply (checkpoint 31).
app_curation.py Protection of manual work (aliases, pinned variants, adoption), backups, manual add, installer override, empty-app repair (checkpoint 32).
curation_dialogs.py AddAppDialog and RepairEmptyAppsDialog (Qt) for app_curation.
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

**Dependency direction** (no cycles): `app_curation` is imported by `database` (schema), `resolver`, `gui_backend`,
`gui_main`, `curation_dialogs`; it imports `resolver` lazily inside functions only (checkpoint 32).
`app_manifest` is a leaf-ish module imported by `database`
(schema setup), `scanner`, `resolver`, `scraper`, `app_manager`, `monitor`, `gui_backend`, `gui_main`
(checkpoint 31; it imports `scanner._installer_group_key` lazily inside one function to avoid a
cycle). `config` <- `database` <- `scanner`,
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
| `scan_roots` | One row per folder root ever scanned. `last_scan_started_at`/`last_scan_finished_at`/`last_scan_status` track re-scan state. Checkpoint 25 added `folder_layouts_json`; checkpoint 28 replaced `root_is_catalog`/`root_catalog_name` (unused going forward, kept as harmless dead columns) with `unconfigured_toplevel_role` (see `scanner.py` section below). |
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
- **Checkpoint 25 (Feature B)**: `.get_scan_root_by_path(path)` /
  `.get_scan_root_by_id(id)` -- full row as a dict. `.ensure_scan_root
  (path)` -- insert-if-missing, used by the GUI to open
  `FolderLayoutDialog` BEFORE a scan job runs (`scan_roots` rows are
  normally created/updated by `scanner._upsert_scan_root()` at scan
  time; this is the one place something else creates the row first).
  `.get_folder_layout(scan_root_id)` -- parsed `folder_layouts_json`
  dict (empty if never configured). `.save_folder_layout(scan_root_id,
  layout, unconfigured_toplevel_role=None)` (signature changed in
  checkpoint 28 -- was `root_is_catalog`/`root_catalog_name`).


**Checkpoint 31:** `init_schema()` ends with `app_manifest.ensure_manifest_schema(conn)`: additive columns `apps.app_uid`, `variants.variant_uid / manifest_hash / manifest_dirty / manifest_path / manifest_written_at / manifest_error`, 4 triggers (uid on insert; dirty on real change), uid backfill, 3 indexes. Idempotent; cheap on repeat runs.

**Checkpoint 32:** `init_schema()` also calls `app_curation.ensure_curation_schema(conn)` (before the manifest schema): table `app_aliases`, column `variants.app_pinned` (backfilled once from `audit_log`), trigger `trg_apps_key_alias` (records the old `normalized_key` on any change).
---

## 3. `scanner.py` -- filesystem walk

Read-only with respect to the scanned drive (never writes/moves/deletes
source files -- see `app_manager.py` for the one feature that does, and
note its much stricter safety requirements).

### Key pieces

- `walk_scan_root(root, settings, ..., folder_layout=None,
  unconfigured_toplevel_role="catalog")` -- the main entry, does an
  `os.walk`-style traversal, classifying every folder via
  `classify_folder()` and building `ScanCandidate` objects. Checkpoint
  25/28: the two kwargs feed `resolve_scan_root_layout()` (below); when
  it reports `skip=True` for a folder, `walk_scan_root` sets
  `dirnames[:] = []` before `continue`-ing so the walk never descends
  into a skipped subtree at all, rather than just discarding candidates
  from it after the fact.
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
- `_derive_catalog_subcatalog(root, folder_path, settings)` -- LEGACY
  fixed-2-tier derivation (`catalog=parts[0]`, `subcatalog=parts[1]`,
  always). Checkpoint 15: checks `_apply_taxonomy_rules()` against
  `category_rules`/`subcategory_rules` settings first. Checkpoint 25:
  no longer the real code path for an actual scan -- superseded entirely
  by `resolve_scan_root_layout()` below, kept only for reference.
- **`resolve_scan_root_layout(root, folder_path, layout,
  unconfigured_toplevel_role, settings) -> (catalog, subcatalog, depth,
  skip)`** (checkpoint 25, **rewritten in checkpoint 28** around a
  cleaner model -- read this note over the docstring's own worked
  examples if the two ever seem to disagree, the docstring is
  authoritative) -- the REAL catalog/subcatalog/depth derivation
  `walk_scan_root()` actually calls. Every folder in `layout` (flat map,
  key = lowercased relative path, value = a role string `"catalog"` |
  `"subcatalog"` | `"app"` | `"skip"`, or `{"role":.., "name":..}` for a
  renamed folder) is SELF-describing -- a folder marked `"app"`
  unambiguously means "I am the app," never "my children are." An
  unconfigured folder's role cascades from its parent
  (`_resolve_role_chain()`: catalog->subcatalog->app->app, walked
  top-down since roles cascade forward now, not backward-searched like
  checkpoint 25's original mode system) or from `unconfigured_toplevel_
  role` for a top-level folder with no parent in the chain. `catalog` =
  the deepest folder in the chain with role `"catalog"` (normally
  exactly one, at the top -- marking one deeper too is allowed and
  intentionally restarts categorization from there). `subcatalog` = the
  deepest `"subcatalog"`-role folder below that catalog, or `None`.
  Depth still gets boosted to `max(raw_depth, 3)` whenever the folder
  ITSELF has role `"app"`, same "don't let the resolver's shallow-name
  distrust misfire on a direct catalog child" idea as the original
  design, just driven off the folder's own resolved role now rather
  than a matched ancestor's mode. Because roles are self-describing, the
  self-match-vs-ancestor-match distinction that made checkpoint 25's
  version tricky (and buggy once) doesn't exist anymore. One real
  trade-off worth knowing: since a Catalog's children default to
  Subcatalog (never App), a catalog with NO real subcategory tier (every
  child folder directly holds its installer) needs each such child
  EXPLICITLY marked `"app"` -- marking the catalog folder itself `"app"`
  does not work, there'd be no folder left with role `"catalog"` to name
  anything under (see `PROGRESS.md` checkpoint 28 for the confirming
  test).
- `list_top_level_folders(root)` / `list_child_folders(root, rel_parent)`
  (checkpoint 25) -- single, non-recursive `os.scandir()` calls, used by
  `gui_main.FolderLayoutDialog`'s lazy tree (levels 1-2 populated
  eagerly, level 3+ on-demand when a row is expanded) and by
  `MainWindow._maybe_show_folder_layout_dialog()` to detect new
  top-level folders on a re-scan.
- `compute_folder_fingerprint()` -- used for incremental re-scan
  (unchanged folders are skipped on a re-scan of an existing root; see
  `ScanRootsDialog` in `gui_main.py` for the UI to trigger this).


**Checkpoint 31 (manifests):** `walk_scan_root` filters `*.appcatalog.json` out of the file list, classification and folder fingerprint; confirmed manifests pin their entry file (`_mf.claim_groups`, `forced_file` for single_app folders) and their dependent sub-folders are pruned from the walk (`_mf.claimed_dir_names`). See section 16.
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


**Checkpoint 31 (manifests):** `run_resolve` looks up a manifest per candidate (`ManifestCache.find`), lets a trusted manifest name a brand-new variant (`override_fields_from_manifest`), then calls `apply_manifest` after `_upsert_variant` (which now returns `(variant_id, created)`). `ResolveProgress` has `manifests_found/applied/db_kept`. See section 16.

**Checkpoint 32 (protection):** `run_resolve` first adopts orphan/moved variants (`_cur.adopt_orphan_variants`), builds a pin map (`_cur.load_pin_map`: variants of locked/verified apps or `app_pinned`), clusters only the UNPINNED candidates, then processes pinned ones separately (`_upsert_variant` into their current app + `learn_alias`), and finally deletes empty unprotected apps. `_upsert_app` falls back to `app_aliases` and never renames an app found by alias. `merge_apps` / `move_variant_to_app` / `split_variant_to_new_app` set `app_pinned = 1`; `merge_apps` also aliases the absorbed app. `ResolveProgress` gained `variants_pinned / variants_adopted / empty_apps_removed`. Never overwrite `variants.app_id` for an existing variant without checking the pin map.

**Checkpoint 33 (manifest identity):** candidates whose identity comes from a manifest (`override_fields_from_manifest` -> `bound_ids`, with `uid_by_id` / `updated_by_id`) are NOT passed to `cluster_candidates`; `cluster_manifest_members` groups them by the manifest's `app_uid` (fallback exact name; same name + same catalog/subcatalog merge) and returns `Cluster(app_uid=..., manifest_bound=True)`. `_upsert_app` matches `app_uid` first, then name in the SAME catalog/subcatalog (bound clusters never fall back to other catalogs or aliases), and a new app keeps the manifest's `app_uid`. Do not fuzzy-merge manifest-bound members.
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


**Checkpoint 31 (manifests):** `run_scrape`, `apply_manifest_candidate` and `apply_choco_candidate` call `_flush_manifests(db)` (-> `app_manifest.flush_dirty`) once their writes are committed.
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
the full comparison of what was/wasn't adopted and why). Checkpoints
25-27 added a sixth group (scan root lifecycle management -- listed
first below since it now runs before the duplicate-detection group in
the file itself). Six independent groups:

0. **`update_scan_root_path(db, scan_root_id, new_root_path) -> dict`**
   (checkpoint 25, "repath") -- for a moved/remounted drive. Rewrites
   the scan root's `path` plus every already-catalogued path derived
   from it (`raw_candidates.folder_path`, `variants.source_path` via a
   join, `scan_errors.path`) in one transaction. Prefix-match only
   (`_starts_with_root()`/`_rewrite_prefix()`), never a substring
   replace. Does NOT re-scan/re-resolve. Logged to `audit_log`.
   **`delete_scan_root(db, scan_root_id) -> dict`** (checkpoint 26) --
   deletes the `scan_roots` row (cascading `raw_candidates`/
   `scan_errors` via existing FKs); deliberately does NOT touch
   `apps`/`variants` -- `variants.raw_candidate_id` is `ON DELETE SET
   NULL`, so already-resolved apps just lose their link back to the
   deleted root's raw scan data rather than disappearing. GUI: "Change
   path…" / "Delete selected root…" buttons per row in
   `ScanRootsDialog`.
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
   -- **checkpoint 22, "Clean library"**; extended in checkpoint 27.
   Deliberately the OPPOSITE of a scan/re-scan: never looks for new
   install units, only confirms variants ALREADY in the catalog are
   still valid, scoped to variants under `root_path` when given (`None`
   = whole catalog, wired to `ScanRootsDialog`'s "Clean library (all
   roots)" in checkpoint 26). Checks, per variant, in order: (1)
   `raw_candidate_id IS NULL` (its scan root was deleted via
   `delete_scan_root()`) -> `MissingItem.reason="scan root no longer
   tracked"`; (2) its scan root is still registered but
   `os.path.isdir(root.path)` is `False` (drive unplugged/remounted) ->
   `reason="scan root path unreachable"`, flagged WITHOUT checking the
   individual file (one `isdir()` per root, cached, not per-variant);
   (3) otherwise the original `os.path.exists()` per-file check ->
   `reason="file not found"`. `CleanLibraryReviewDialog` (`gui_main.py`)
   shows the reason in its own column. Removal deletes both the
   `variants` row and its `raw_candidates` row (when it has one -- case
   1 above won't) -- the latter matters, or a future Resolve run without
   a fresh Scan first could silently re-create the exact variant just
   removed, since Resolve reads `raw_candidates` from the DB, not the
   live filesystem. An app left with zero variants after removal is
   deleted outright (FK cascades handle its tags etc.). Read-only scan /
   destructive-but-DB-only execute, same two-step shape as reorganize --
   see `PROGRESS.md` checkpoints 22 and 27.


**Checkpoint 31:** `execute_reorganize` is now a wrapper (suspends the manifest auto-flusher, flushes at the end) around `_execute_reorganize_impl`. Manifests are never copied as 'sidecars'; `_write_reorg_manifest` writes the manifest at the destination after each successful move (copy mode: `record=False`, DB row untouched; archive mode: records `orig_name` / `orig_fp`).
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


**Checkpoint 31:** the Reorganize preview table (`reorg_table`) has fully resizable columns (`QHeaderView.Interactive`, last column stretches, middle-elided paths with tooltips); widths persist in setting `reorg_table_col_widths` (saved debounced by `_save_reorg_col_widths`). Do not use `w`/`v` as loop variables in the `_build_*_tab` methods -- they are the page widget/layout.
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
4. `ScanRootsDialog` -- lists `scan_roots`. Consolidated into the single
   entry point for scan-root management in checkpoint 26 (the main
   toolbar's separate "Add scan root…" `QAction` was removed): "Add new
   scan root…" (checkpoint 28: now asks "is this itself a single
   catalog?" first -- see `FolderLayoutDialog` entry below for what that
   triggers), "Delete selected root…" (checkpoint 26,
   `app_manager.delete_scan_root()` -- non-destructive, see that
   function's own doc in the `app_manager.py` section), per-row
   "Re-scan now" / "Change path…" (checkpoint 25, `update_scan_root_
   path()`) / "Clean library" (checkpoint 22) / "Edit folder layout…"
   (checkpoint 25, opens `FolderLayoutDialog` directly without a scan),
   plus checkpoint 26's "Re-scan all roots" (sequential batch, see
   `MainWindow._run_rescan_all_roots()`/`_advance_batch_rescan()` below)
   and "Clean library (all roots)" (`_run_clean_library(None)`).
4b. `FolderLayoutDialog` (checkpoint 25, **redesigned checkpoint 28**) --
   per-scan-root folder layout editor, lazy `QTreeWidget` (levels 1-2
   populated eagerly, deeper levels resolve on-demand via `scanner.
   list_child_folders()` the moment a row is expanded). Every row gets
   an identical 4-item role dropdown (`ROLE_ITEMS`: Catalog / Subcatalog
   / App / Skip) regardless of depth -- no more separate top-level-vs-
   child dropdowns or a symbolic "inherit" state; an unconfigured row's
   default is computed by `_default_role_for()`, cascading down from its
   parent's role (or this root's `unconfigured_toplevel_role` for a
   top-level row), and is a concrete, editable value the moment the row
   appears. The Preview column just calls `scanner.resolve_scan_root_
   layout()` on the row's OWN path directly and formats by role -- no
   synthetic child-folder trick needed anymore (see the `scanner.py`
   section for why that's now unnecessary). Opened three ways:
   automatically before a scan via `MainWindow._maybe_show_folder_
   layout_dialog()` (first-ever scan of a root, or a re-scan that found
   new top-level folders -- shows the FULL dialog either way, new rows
   just marked "(NEW)"), directly via `ScanRootsDialog`'s "Edit folder
   layout…" button (no scan triggered), or right after `ScanRootsDialog.
   _add_new_root()`'s single-catalog promotion (see below).
   **Checkpoint 28's "-1 level" single-catalog promotion**: picking a
   folder as "itself a single catalog" (a `QMessageBox.question` prompt
   in `_add_new_root()`) does NOT store that folder as the scan root --
   it prompts for a display name (`QInputDialog.getText`, default =
   folder's basename), walks up one level (`os.path.dirname`), and
   `db.ensure_scan_root()`s the PARENT as the actual root, writing an
   explicit `"catalog"` (or `{"role":"catalog","name":...}` if renamed)
   entry for the originally-picked folder plus setting that root's
   `unconfigured_toplevel_role` to `"skip"`. Adding a second single-
   catalog folder that shares the same parent (e.g. `C:\Program\
   Graphic` then `C:\Program\Desktop`) needed NO new dedup code at all --
   it lands on the same `scan_roots` row for free, since `ensure_scan_
   root()` is already insert-if-missing keyed on the unique `path`
   column; both catalogs just merge into the same `folder_layouts_json`.
4c. `CleanLibraryReviewDialog` (checkpoint 22) -- sits right after
   `ScanRootsDialog` in this file. Checkbox-per-row review (default
   checked) of what `MainWindow._run_clean_library()` ->
   `CleanLibraryScanWorker` -> `app_manager.scan_for_missing_sources()`
   found; checkpoint 27 added a "Reason" column (`file not found` /
   `scan root path unreachable` / `scan root no longer tracked`).
   "Remove N selected" -> `app_manager.execute_clean_library()`, then
   auto-opens the HTML report the same way `OrganizeDialog` does.
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
   `_run_monitor` (checkpoint 18), `_run_clean_library` (checkpoint 22;
   `root_path=None` = whole catalog, checkpoint 26),
   `_run_rescan_all_roots`/`_advance_batch_rescan` (checkpoint 26 --
   sequential batch across every scan root; continuation is driven from
   `_on_scan_resolve_finished`/`_on_job_failed` via a `_batch_rescan_
   current_path` flag, NOT a fresh `.connect()` made after the worker's
   `.start()` already returned -- that was tried first and lost a race
   against a fast-finishing worker, see `PROGRESS.md` checkpoint 26),
   `_maybe_show_folder_layout_dialog` (checkpoint 25), `_show_apps_
   context_menu`, `_open_organize_dialog`, `_show_column_picker`/
   `_apply_column_visibility` (checkpoint 15, `visible_columns` setting),
   `_open_settings`, CSV export/import handlers). **`_release_worker()`**
   (checkpoint 27) -- EVERY completion/failure handler clears
   `self._active_worker` through this helper, never by assigning `None`
   directly; it calls `worker.wait()` first, which is a no-op if the
   thread's already finished (the common case) but closes a real race
   that caused an intermittent `QThread: Destroyed while thread is still
   running` crash (the custom `finished_ok`/`failed` signals fire from
   inside `run()`, which doesn't guarantee the OS thread has fully wound
   down by the time the slot runs on the main thread). Any NEW worker
   completion handler must go through this helper too, not
   `self._active_worker = None` directly.

`MainWindow.select_app_by_id(app_id)` is the "jump to app" API used by
`OrganizeDialog`'s report tab when a finding is double-clicked -- it
clears any active filter first, then selects and scrolls to the row.


**Checkpoint 32:** toolbar **Add app…** (`_add_app_dialog`) and **Repair empty apps…** (`_repair_empty_apps`); `_run_resolve_all` shows a confirmation whose default/escape button is Cancel (always use the button object from `addButton` for `setDefaultButton`); `DetailPanel._change_variant_installer_file` offers an override for files outside the variant folder via `_offer_installer_override`. `gui_backend.ResolveWorker` / `ScanAndResolveWorker` take DB backups first (`app_curation.create_backup`).

**Checkpoint 33:** toolbar order is Scan roots… -> Add app… -> Re-resolve all ...; **Repair empty apps…** lives in the app-table context menu (`_show_apps_context_menu`, calls `_repair_empty_apps`). `DetailPanel._split_selected_variant` opens `AddAppDialog(split_variant=, split_app=)` and `app_curation.split_variant_with_details` does the work.
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


**Checkpoint 31:** `execute_monitor_plan` is now a wrapper (holds `app_manifest.suspend_auto_flush()` during the run, flushes once at the end) around `_execute_monitor_plan_impl`; `_record_variant` returns the new variant id and the manifest is written right after each attach (with the original file's name/fingerprint when the file was archived). Syntax-checked only, not run on real folders yet.

**Checkpoint 32:** `_lookup_app_by_key` falls back to `app_aliases` (an app renamed by hand/scraper is still found by its old key).
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


**Checkpoint 31:** added `manifest_auto_enabled` (True), `manifest_auto_only_valuable` (True), `manifest_companion_words`. `reorg_table_col_widths` is stored by the GUI and has no default.
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
  
  ---

## 15. Layout-change propagation (`app_manager.apply_layout_change`, checkpoint 29)

Added to `app_manager.py`, grouped under its existing "0b. Layout-change
propagation" heading (right after `delete_scan_root()`, before the
duplicate-detection section). Nothing else in any module changed to
support it -- it lives entirely inside `app_manager.py` plus one call
site in `gui_main.py`'s `FolderLayoutDialog._on_ok()` and one status-bar
read in `ScanRootsDialog._edit_layout()`.

### Why it exists

The scanner upserts `raw_candidates` and the resolver only creates/
updates apps -- neither ever deletes. So when a user edits a scan root's
folder layout in `FolderLayoutDialog` and changes a folder's role
(e.g. `App` -> `Single App/Variant`, or anything else that alters which
folders count as install units), the next re-scan correctly emits new
candidates for the affected subtree, but the OLD layout's apps stay in
the catalog. Live reproduction on a real ADOBE/PHOTOSHOP/CS6 subtree:
9 apps under the old `App` role, role change to `Single App/Variant`,
re-scan produced 10 apps instead of 1. Nothing in the pipeline removes a
`raw_candidate` that's no longer walked under the current layout, so
the layout editor was only usable on a fresh DB.

### New public API

- **`apply_layout_change(db, scan_root_id, old_layout, new_layout, *,
  old_unconfigured_toplevel_role=None, new_unconfigured_toplevel_role=None)
  -> LayoutChangeResult`** -- brings the catalog into agreement with an
  edited folder layout. Called by `FolderLayoutDialog._on_ok()` right
  after `db.save_folder_layout()` writes the new JSON;
  `save_folder_layout()` itself stays dumb (just writes the JSON), the
  propagation is a separate concern the caller orchestrates. Two classes
  of change, classified per-key from the diff of old vs. new layout:

  - **Label-only** (role unchanged; only the optional `name` differs):
    `UPDATE raw_candidates.catalog/subcatalog` in place across the
    affected subtree, computed via `scanner.resolve_scan_root_layout()`
    (the SAME function the scanner itself uses, so labels can't drift
    between the two). No deletion, no re-scan needed.
  - **Role change** (effective role differs, or an entry was added/
    removed): delete `variants` + `raw_candidates` under the affected
    subtree, then delete any app left with zero variants. Because the
    fingerprints go with the deleted `raw_candidates`, the next
    incremental scan cannot skip these folders even if their mtime is
    unchanged -- a natural re-scan of just the affected subtree is
    automatic. Apps that still have variants from outside the affected
    scope are left untouched (`name_locked` / `catalog_locked` /
    `subcatalog_locked` / `status` / scraper fields all survive because
    the app itself survives).

  All steps run in one transaction, no filesystem operations, logged to
  `audit_log` as `entity_type="scan_root", action="layout_change"` with
  the affected folder list and removal counts. Never triggers a re-scan
  itself; the next scan (manual, or the user's existing "Re-scan now")
  rebuilds.

- **`LayoutChangeResult`** dataclass -- `folders_affected`,
  `raw_candidates_deleted`, `variants_deleted`, `apps_deleted`,
  `label_updates`, `role_change_folders`, `label_only_folders`. Read by
  `ScanRootsDialog._edit_layout()` to build the post-save status-bar
  message.

### Classification uses effective roles, not raw entries

`apply_layout_change()` classifies keys via a small recursive helper,
**`_effective_role(layout, key, unconfigured_toplevel_role)`**, that
mirrors `scanner._resolve_role_chain()`'s forward cascade exactly
(catalog -> subcatalog -> app -> app; a top-level key with no explicit
entry gets the root's own `unconfigured_toplevel_role`). This matters
because `FolderLayoutDialog._on_ok()` deliberately writes every visible
top-level row explicitly on every save (so a future re-scan doesn't
treat an unchanged folder as "new") -- comparing raw explicit-entry
values would misclassify "no entry, so the default applies" against
"explicit entry that happens to equal the default" as a role change on
every single open-OK cycle, deleting untouched subtrees for no reason.
`_effective_role()` treats those as identical, which is what the
scanner does too, so a no-change save correctly returns zero changes.

### Reuses existing helpers, does not reimplement

- `_starts_with_root()` (existing) for the path prefix check -- same
  case-insensitive, separator-boundary-aware ancestor check
  `update_scan_root_path()` uses, so a path like `D:\PROGRAMS2\Foo` is
  never treated as under root `D:\PROGRAMS`.
- `_delete_zero_variant_apps(conn, app_ids)` -- extracted from
  `execute_clean_library()` so `apply_layout_change()` and
  `execute_clean_library()` share the identical "an app only exists
  because of its variants" deletion rule. Do not reimplement it in a
  third place.
- `scanner.resolve_scan_root_layout()` -- imported lazily inside the
  function so `app_manager` stays importable from `monitor.py` without
  pulling in `scanner`'s optional PE/archive dependencies.

### Caller wiring (in `gui_main.py`)

- `FolderLayoutDialog.__init__` gains a `self.layout_change_result =
  None` slot; `_on_ok()` populates it after calling
  `db.save_folder_layout()` and before `self.accept()`.
- `ScanRootsDialog._edit_layout()` reads it back via `getattr(dialog,
  "layout_change_result", None)` and sets the main window's status bar
  to "Layout saved. Removed N app(s) / M variant(s) (K folder(s)
  affected). Re-scan to rebuild." on any counts, or "Layout saved. No
  catalog changes needed." on a no-op. No confirmation dialog: per the
  plan, save is already a deliberate action and this one-line status is
  the signal to inspect before re-scanning. The previous modal "Layout
  saved" info box is removed entirely.

### `database.py` / `scanner.py` companion fix (same checkpoint)

An older build had shipped `DEFAULT 'skip'` for
`scan_roots.unconfigured_toplevel_role`, and `ALTER TABLE` cannot change
a column default once the column exists -- so every fresh scan root on
a DB created by that older build silently inherited `'skip'`, and
`FolderLayoutDialog` then wrote that `'skip'` explicitly into every
top-level layout entry on first save, silently skipping the entire root
on every scan. Two changes close this permanently:

- Every `INSERT INTO scan_roots` site (`Database.ensure_scan_root()`
  and `scanner._upsert_scan_root()`) now writes
  `unconfigured_toplevel_role='catalog'` and `folder_layouts_json='{}'`
  EXPLICITLY rather than relying on the table default.
- `Database._fix_bad_skip_roots()` (called from `init_schema()` right
  after `_run_migrations()`) is a one-time data migration for
  already-poisoned roots: any root whose `unconfigured_toplevel_role` is
  `'skip'` AND whose layout contains either no entries or only `'skip'`
  entries with no name overrides is reset to `'catalog'` with those
  auto-written entries dropped. Any root where the user made a real
  choice is left alone.
  
  | `variants` | One row per raw_candidate that got assigned to an app -- a specific version/edition/architecture/language combination. Checkpoint 30: `file_locked` (0/1) marks the user's manual installer-file override -- when set, the scanner keeps using `variants.file_name` for that folder instead of auto-picking, and `resolver._upsert_variant()` never overwrites `file_name` on a locked row. |
  
  - **Checkpoint 30 -- variant file lock (`locked_files`)**:
  `walk_scan_root(root, ..., locked_files=None, ...)` accepts a dict of
  `{folder_path: file_name}` (built once per scan in `run_scan()` from
  `variants.file_locked = 1`, joined via `raw_candidates`), scoped to the
  scan root. `locked` is looked up ONCE per folder, before the
  `is_single_app` branch. For single_app folders it goes through
  `_build_single_app_candidate(..., forced_file=locked)` which filters
  `group_files` to the matching relpath (exact-then-basename). For
  install_unit folders it does a case-insensitive basename match against
  `file_names` and collapses the folder's candidates to `[[matched]]` on
  hit, or logs a warning and falls back to `_group_installer_files()` on
  miss. Never touched by resolver or GUI directly -- `variants.file_locked`
  is the only storage.
  
    Checkpoint 30: also "Change installer file…" / "Clear installer file
  override" (both gated on `raw_candidate_id IS NOT NULL`; see
  `_change_variant_installer_file` / `_clear_variant_installer_file`).
  Stored value is `os.path.relpath(chosen, source_path)` -- native
  separators, never converted to forward slashes (Explorer's `/select,`
  silently ignores forward slashes and opens Desktop instead).
  `_variant_full_path()` and `_open_file_location()` both call
  `os.path.normpath()` on the joined path as belt-and-braces for any
  legacy DB row. `load_app()` prepends 🔒 to the File Name cell when
  `file_locked` is set.


---

## 16. `app_manifest.py` -- variant manifests (checkpoint 31)

**Purpose.** Persist the manual / scraped work next to the files so a fresh
`catalog.db`, an app update, or a reorganized library can recognise it again.
One JSON file per **variant (install unit)**, independent of catalog and
subcatalog (those are never stored).

### File name and placement

| Case | File |
|---|---|
| variant owns its folder (`unit.owned = true`) | `<folder>/<App name> <version> [edition] [arch] [language].appcatalog.json` |
| folder shared by several variants | `<folder>/<entry file name>.appcatalog.json` |
| legacy first draft | `<folder>/appcatalog.json` (read, migrated on next write) |

`is_manifest_filename()` = `appcatalog.json` or `*.appcatalog.json`. The writer
never overwrites a manifest describing a different unit
(`choose_manifest_path` + `same_unit`: same `variant_uid` OR same entry file in
the same folder counts as "ours"; otherwise ` [<uid6>]` is appended). Superseded
files of the same unit are removed after a write (rename, legacy).

### Format (schema_version 1, `kind = "appcatalog.variant"`)

```
app      app_uid, name, locked[], publisher, description, homepage, license,
         winget_id, choco_id, latest_version, scrape_status, last_scraped, tags[]
variant  variant_uid, version, edition, architecture, language, ignored, verified,
         locked[] (version, entry), original_name, name_source
unit     root ".", owned, entry{path, kind, size, fp, confirmed, orig_name?, orig_fp?},
         members[]{path, type file|dir, role entry|required|companion,
                   size | files+bytes+tree_fp}
```
Paths are relative to the manifest folder. `entry.fp` = size + head/middle/tail
1 MB hash (`quick_fingerprint`); folders use a structural fingerprint
(`tree_stats`: sorted relative paths + sizes, no content read).
`entry.confirmed` is true when the variant is verified, `file_locked`, or monitor-created;
only confirmed manifests change scanning. Unknown future keys: `read_manifest`
accepts a newer `schema_version` with a warning.

### Public API (what other modules use)

| Function | Used by |
|---|---|
| `ensure_manifest_schema(conn)` | `database.init_schema` |
| `write_variant_manifest(db, variant_id, force=, dry_run=, folder=, file_name=, record=, orig_name=, orig_fp=)` | reorganize, monitor, workers |
| `write_manifests(db, variant_ids=None, progress=, cancel=)` -> `BatchResult` | `ManifestWorker`, variant menu |
| `flush_dirty(db)` / `has_dirty(db)` / `manifest_stats(db)` | GUI timer, scraper, reorganize/monitor wrappers, Settings |
| `suspend_auto_flush()` / `auto_flush_suspended()` / `auto_enabled(settings)` | reorganize/monitor wrappers, GUI timer |
| `variant_ids_for_apps(db, app_ids)` | app context menu |
| `remove_manifests(db, variant_ids)` | (helper, no UI yet) |
| `ManifestCache`, `load_folder_manifests`, `folder_manifest_names`, `is_manifest_filename` | scanner, resolver |
| `claim_groups`, `claimed_dir_names`, `confirmed_entry_for_folder` | scanner |
| `override_fields_from_manifest`, `apply_manifest`, `check_unit` | resolver |

### Database additions

Columns: `apps.app_uid`; `variants.variant_uid`, `manifest_hash` (hash of the
content last written/applied = baseline), `manifest_dirty`, `manifest_path`,
`manifest_written_at`, `manifest_error`. Triggers: `trg_apps_uid_ins`,
`trg_variants_uid_ins` (also dirties), `trg_apps_manifest_dirty`,
`trg_variants_manifest_dirty` (fire only when `OLD.x IS NOT NEW.x` for a column in
`_APP_DIRTY_COLS` / `_VARIANT_DIRTY_COLS`; catalog/subcatalog are NOT in the
lists). To make a new field part of the manifest: add it to `build_payload`, to
the matching `_*_DIRTY_COLS` tuple, and to `apply_manifest`.

### Auto mode (settings `manifest_auto_enabled`, `manifest_auto_only_valuable`)

edit/scrape/merge/split/move -> trigger sets `manifest_dirty` -> `flush_dirty`
writes it. Flush points: GUI timer every 4 s (`ManifestFlushWorker`), end of
scrape, after each reorganize/monitor item, window close. `is_valuable(row)`
(name locked, version locked, file locked, ignored, verified, scraped, or
name_source manual/monitor) is the "only valuable" filter; manual writes ignore it.
Failures never interrupt the triggering action: logged + `variants.manifest_error`.

### Reader / precedence

See `apply_manifest` (table in PROGRESS.md checkpoint 31): new variant -> full
apply; baseline equal -> nothing; manifest newer and DB clean -> apply; both
changed -> DB wins + `audit_log manifest_conflict`; no baseline (old DB) -> DB
wins, only empty app details filled. DB locks always win. Integrity problems
-> `audit_log manifest_integrity` and `needs_review` (unless verified).

### GUI

Toolbar **Write manifests...**; app and variant context menus **Create/update
manifest**; Settings -> Scanning & Noise group *Variant manifests*;
`ManifestWorker` / `ManifestFlushWorker` in `gui_backend.py`;
`MainWindow._manifest_tick`, `_write_manifests_for`, `closeEvent`.

### Gotchas

- Several files have mixed CRLF/LF endings (`gui_main.py`, `gui_backend.py`,
  `monitor.py`); edits must preserve each file's endings.
- `execute_reorganize` / `execute_monitor_plan` are wrappers now; call them as before.
- `_upsert_variant` returns `(variant_id, created)`.
- Test pattern used: build an "old" DB with the ORIGINAL code (`git archive HEAD`),
  hand-edit it with sqlite to simulate manual work, then open it with the new code.


---

## 17. `app_curation.py` + `curation_dialogs.py` -- protecting and curating manual work (checkpoint 32)

**Why.** Resolve matched apps only by `normalized_key` and overwrote `variants.app_id`; any rename (manual or
the scraper's auto-rename), merge, move or split was undone by the next rescan / "Resolve all" (duplicate app +
EMPTY renamed app). See PROGRESS.md checkpoint 32 for the full analysis.

### Schema (additive, `ensure_curation_schema`)
`app_aliases(normalized_key PK, app_id FK cascade)`; `variants.app_pinned`; trigger `trg_apps_key_alias`
(`AFTER UPDATE OF normalized_key`, records OLD key, `INSERT OR REPLACE`); one-off pin backfill from `audit_log`.

### Resolver support
| Function | Role |
|---|---|
| `load_pin_map(conn)` | raw_candidate_id -> app_id for variants that must stay (app name/catalog/subcatalog locked, verified, or `app_pinned`) |
| `adopt_orphan_variants(conn, rows)` -> `(count, stale_raw_ids)` | re-link variants with no/stale raw row to the fresh raw row of the same folder+file; delete the old raw row if its folder is gone |
| `find_app_by_alias`, `learn_alias`, `alias_absorbed_app` | alias lookup / learning / merge support |
| `remove_empty_unprotected_apps(conn)` | delete empty apps with no lock/verified/scrape/description |

### Manual curation
| Function | Role |
|---|---|
| `create_backup(db_path, label, keep, min_interval_s)` | `backups/<db>-<label>-<ts>.db`; never raises |
| `suggest_app_fields`, `find_existing_app`, `find_variant_at`, `add_app_manually` | Add-app (no scan root); variant has no raw row, `file_locked`, `app_pinned`, `name_source='manual'`; app verified + name/catalog/subcatalog locked |
| `plan_installer_override` / `apply_installer_override` | installer outside the variant folder: widen the unit to the common parent, `single_app` layout entry, locked entry file; refuses scan-root/drive-root parents and >`MAX_OTHER_APPS_SWALLOWED` other apps |
| `find_empty_app_repairs` / `apply_empty_app_repairs` | repair apps already emptied by the old bug (merge / delete / review) |

### GUI
`AddAppDialog` (toolbar **Add app…**), `RepairEmptyAppsDialog` (toolbar **Repair empty apps…**, resizable
columns), Resolve-all confirmation, installer-override dialog in `DetailPanel`.

### Rules for future code
* Never set `variants.app_id` from a re-derived cluster without consulting `load_pin_map`.
* Any code that changes `apps.normalized_key` is covered by the trigger -- do not bypass it with raw SQL on another table.
* Code that merges/moves/splits variants must set `app_pinned = 1` (and alias absorbed apps).
* Manifests carry `variant.pinned`; a manifest is trusted when confirmed / verified / pinned / scraped / name-locked.
* Test pattern: build the "old" DB with the ORIGINAL code (`git archive HEAD`), reproduce the bug, then rerun on the new code.

### Checkpoint 33 additions to section 17 / 16
* `split_variant_with_details(db, variant_id, name=, installer_path=, unit_folder=, catalog=, subcatalog=, version=, edition=, architecture=, language=, target_app_id=)` -- split a variant into a new app (or move into `target_app_id`) applying the dialog's edits; pins the variant; see PROGRESS.md checkpoint 33 for installer/folder edit semantics.
* `AddAppDialog(db, parent, start_dir=None, split_variant=None, split_app=None)` -- split mode prefills from the variant and calls `split_variant_with_details`.
* Section 16 (`app_manifest.py`): `override_fields_from_manifest` now applies to EVERY manifest (not only trusted ones) for fresh variants; the manifest's `app_uid` is the cluster identity (see resolver, section 4).
