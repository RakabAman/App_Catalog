# App Catalog — Design & Progress Log

## Pipeline (agreed)
```
Scan (unattended)  →  raw_candidates table  (permanent, never mutated by resolver)
Resolve (unattended, runs right after scan)  →  apps + variants  (re-runnable anytime,
                                                  never touches filesystem again)
Scrape (manual "Scrape selected"/"Search Chocolatey…" + toolbar "Scrape all",
        checkpoint 12 — Winget manifest background enrich + Chocolatey manual
        search both wired up and live-tested; see checkpoint 12 below)
```

## Decisions locked in so far
- **DB**: single-file SQLite. Settings live in the same DB (not a separate config file),
  are versioned (`settings_version`), and every resolved app/variant records which
  settings version produced it — enables "re-resolve everything touched by rule change X".
- **Archives**: list contents first (cheap). Escalate to full extraction only when
  ambiguous (no clear single installer match against the folder name). Extracted files
  are purged after PE metadata is read — only metadata persists, not the payload.
  `.zip`/`.7z`/`.iso` are pure-Python (work anywhere). **`.rar` requires an external
  `unrar`/`7z`/`bsdtar` binary on PATH at scan time** — not present in this sandbox,
  will need one on the Windows machine that does the real scan. Missing backend
  degrades gracefully (flagged, not crashed).
- **Folder classification** (`install_unit` / `container` / `noise` / `unresolved`):
  keyword-based noise/container detection (e.g. "payloads", "redist", "_files") only
  applies from **depth 3 down**. Catalog (depth 1) and subcatalog (depth 2) folders are
  always structural containers regardless of name — this was a real bug caught in
  testing (a catalog literally named "TUTORIALS" was being pruned as noise).
- **Incremental scans**: folders fingerprinted by (name, size, mtime) of direct
  children — not content hashes (too slow at 800GB). Unchanged folders are skipped on
  re-scan but `last_seen_at` still updates.
- **Editing safety**: any manually-edited field on an `app` sets a `*_locked` flag;
  re-resolve never overwrites locked fields unless explicitly forced.
- **GUI toolkit**: PySide6.
- **mcp-server-appcatalog / mpkg**: placeholder only for now (`scrape_cache` table and
  `apps.scrape_status` exist), user will supply details later. Not blocking Phase 1.

## Built & tested so far
- `db/schema.py`, `db/connection.py` — schema + live settings accessor
- `config/defaults.py` — all tunable resolver/scanner behavior, DB-backed
- `scanner/pe_metadata.py` — PE version-resource reader (pefile)
- `scanner/archive_inspect.py` — tiered zip/7z/rar/iso inspection
- `scanner/classify.py`, `scanner/fingerprint.py`, `scanner/walker.py` — folder walk
  + classification + fingerprinting
- `scanner/scan_job.py` — orchestrator: walk → upsert `raw_candidates`, incremental,
  progress callback (GUI-ready, no Qt dependency in this layer)
- Verified end-to-end against a synthetic tree mirroring real patterns from the
  user's actual folder dump (nested Captivate versions, Dreamweaver payloads,
  a zip requiring inspection, empty/junk tutorial folders).

## Not built yet
- **Resolver**: name/version/tag extraction, release-tag stripping, fuzzy clustering
  of variants into one app, confidence scoring, re-resolve diff preview.
- **GUI**: table + detail view, inline editing, merge/split, live settings panel,
  scan/resolve job controls with progress.
- **Scraper**: fully deferred, placeholder tables only.

## Next step
Build the Resolver module against the `raw_candidates` produced above.

## Checkpoint 2: Resolver — built & tested
- `resolver/extract.py` — field extraction (name/version/edition/arch/language/
  release-tags) from PE metadata + folder/file names. Token-based edition/language
  matching (avoids "K-Lite" -> wrongly stripping "Lite" as an edition). Smart-case
  normalization (only re-cases ALL-CAPS or all-lowercase names; mixed-case/branded
  names left untouched).
- `resolver/cluster.py` — fuzzy clustering via rapidfuzz, NOT restricted to same
  catalog/subcatalog (per requirement: duplicates "mistakenly stored randomly"
  must still merge). Blocked by first-2-chars of normalized key for scale.
- `resolver/resolve_job.py` — orchestrator. Idempotent (matches existing apps by
  normalized_key, doesn't duplicate on re-run). Respects locked fields. Includes
  `_contextual_folder_name()`: climbs to parent folder for naming context when the
  install-unit's own leaf folder is just a thin version label (e.g.
  "ADOBE DREAMWEAVER\CS3\<installer.exe>" -- leaf "CS3" alone has no product name).

### Bugs found & fixed via testing (all real patterns from user's actual tree)
1. Release-group/flag words leaking into names ("Adobe Captivate **Only**") --
   expanded release_tag_patterns word list.
2. Leaf-folder-is-just-a-version case losing the product name entirely
   ("CS3" alone, should be "Adobe Dreamweaver CS3") -- added parent-folder climb.
3. Edition keyword matching via regex \b wrongly split brand names containing
   edition-like substrings ("K-Lite" -> "K-" + edition "Lite") -- switched to
   whitespace-token equality matching instead of substring/word-boundary regex.
4. ALL-CAPS folder names produced ALL-CAPS app names -- added smart-case
   normalization that only touches mono-case (all-upper or all-lower) strings.

### Verified via test suite
- Cross-folder clustering: a duplicate stashed in a completely unrelated
  catalog (GRAPHICS\MISC) correctly merged into the right app.
- Idempotency: running resolve twice produces zero duplicate apps/variants
  (apps_created=0, apps_updated=N on the second pass).
- Locked-field protection: a user-renamed + locked app name survives a
  subsequent re-resolve untouched.

## Next step
GUI (PySide6): table + detail view, live settings panel, scan/resolve job
controls with progress, inline editing with lock-on-edit, merge/split actions.

## Checkpoint 3: GUI (PySide6) — built & smoke-tested
- `gui/models.py` — AppsTableModel: search/filter (text + catalog + status),
  inline-editable name/catalog/subcatalog cells, needs_review rows highlighted,
  editing a cell writes through to the DB immediately and locks the field.
- `gui/detail_panel.py` — selected app's editable fields, variants table,
  per-variant actions (ignore / move to different app / split into new app),
  "Mark verified", and "Re-resolve this app" (opens diff preview before applying).
- `gui/reresolve_dialog.py` — shows old-vs-proposed values as checkboxes;
  locked fields shown disabled; only checked changes get applied.
- `gui/app_picker_dialog.py` — searchable picker used by move/merge actions.
- `gui/settings_dialog.py` — tabbed live settings (resolver thresholds, archive
  handling, scan behavior, keyword lists), writes through db.set_setting()
  immediately, bumps settings_version once per Save.
- `gui/jobs.py` — QThread workers (ScanAndResolveWorker for the unattended
  scan-then-resolve flow, ResolveWorker for "Re-resolve all") so the UI thread
  never blocks during a long scan; progress surfaces via Qt signals.
- `gui/main_window.py` — assembles catalog-list filter + table + detail panel
  in a splitter, toolbar (Add scan root…, Re-resolve all, Settings), status
  bar with live progress text.
- `run_gui.py` — entry point: `python run_gui.py [path/to/catalog.db]`.

### Verified via headless smoke test (QT_QPA_PLATFORM=offscreen)
- MainWindow constructs and populates from real resolved data (5 apps from
  the checkpoint-2 test tree).
- Row selection correctly loads the detail panel (name, variants).
- Catalog-list filter and text search both correctly narrow the table,
  including combined (catalog + search) filtering.
- Inline table cell edit writes to the DB and sets the lock flag.
- SettingsDialog and ReresolveDialog construct without error against real data.

### Known gap
Only smoke-tested (construction + programmatic interaction), not visually
inspected by a human yet -- worth a first real run on your machine to check
layout/spacing before trusting it for the full 800GB pass.

## Checkpoint 4: Diagnosed & fixed "0 apps found" real-world report
User ran the GUI on real data: "Scanning... 1000 folders seen, 0 install units
found". Root cause identified: **Windows MAX_PATH (260 char) limit**. This
collection has long, deeply-nested folder names (scene-release style names +
multiple catalog levels), and on Windows, filesystem calls on paths over
~260 chars raise OSError -- which the scanner was silently catching and
skipping, with zero visibility into what happened or how often.

### Fixes
1. **Long-path support**: `scanner/walker.py` now applies the `\\?\` Win32
   long-path prefix to all filesystem calls (no-op on non-Windows), bypassing
   MAX_PATH. Display paths stored in the DB are still the clean, unprefixed
   form.
2. **No more silent skips**: every unreadable folder is now caught, logged,
   and written to a new `scan_errors` table (path + error message) instead
   of vanishing. `ScanProgress.errors_count` surfaces the count.
3. **Console logging throughout**: `run_gui.py` now configures
   `logging.basicConfig` to stdout. Run from a terminal (not double-clicked)
   to see, live: every install unit found, every skipped/errored folder,
   periodic progress every 500 folders, and start/finish summaries for both
   scan and resolve. Background job (QThread) exceptions now log full
   tracebacks to console in addition to the GUI error popup.

### Verified
Re-ran the full scan+resolve test suite with logging enabled -- confirmed
live per-folder output, correct final summary counts, and (via code review,
sandbox runs as root so permission errors don't trigger here) the
error-catching path itself.

## Troubleshooting note for the user
If "0 apps found" happens again after this fix, the console output will now
show exactly why (skipped-folder warnings with the specific OSError, or
"INSTALL UNIT: ..." lines confirming detection is working) -- please share
that console output rather than just the GUI screenshot if it recurs.

## Checkpoint 5: Deep inspection made optional (two separate toggles, default OFF)
Per user request, archive-content inspection and .exe metadata reading are
now independently toggleable in Settings > Archives, both **default OFF**:
- `read_exe_metadata_enabled` -- read PE version resource (ProductName/Version/
  Company) from .exe files. Off = name-based only.
- `inspect_archive_contents_enabled` -- list/extract .zip/.rar/.7z/.iso
  contents to find and identify the installer inside. Off = archives
  identified by filename only.
Extraction-on-ambiguity additionally requires read_exe_metadata_enabled too
(there's no reason to extract an archive if PE reading is off -- nothing
would be done with what's found inside).

With both off (the default), scanning is purely name/structure-based: much
faster over 800GB, zero archive-extraction risk, and matches what most users
will want for a first full pass. Turn either on later and use "Re-resolve
all" / re-scan to enrich existing data without starting over.

Verified all 4 on/off combinations produce correct, distinct behavior while
leaving install-unit detection and clustering counts unaffected.

## Checkpoint 6: Real-usage feedback round — GUI workflow + naming pipeline overhaul

### Scanning/resolving fixes (root-caused from screenshot: dvd_fab_*.zip wrongly
### named "Burners" because it sat directly under the BURNERS category folder)
1. **File-name-first naming with transparency**: resolver now prefers the
   installer FILE name over the folder name when the install unit sits
   directly under a catalog/subcatalog folder (depth<=2) or the folder name
   is in `ignore_folder_names` -- exactly the "Burners" bug. The non-chosen
   source is kept as `alt_name_candidate` (+ `alt_name_source`) on both the
   app and each variant, shown in the GUI with a "Use this instead" button
   so the user decides rather than the resolver guessing silently.
2. **Website/domain tag stripping**: new `website_tag_patterns` (regex,
   step 1 of the pipeline) strips both dot-form ("HaxPC.net",
   "www.site.com") and space-obfuscated form ("www islamdigit blogspot
   com") site tags from names, before any other cleaning.
3. **`ignore_filename_words`**: plain whole-word list (setup, patch,
   serial, beta, etc.) stripped from file names -- simpler to tune than
   regex, separate from the release-tag regex list.
4. **`ignore_folder_names`**: folder names that are never used as an app
   name (bare "32"/"64"/"x86" bitness folders, "setup", "new", etc.) --
   the resolver climbs to the parent for context instead, fixing the
   "a 32-bit folder became the app name '32'" bug.
5. **`folder_name_aliases`**: catalog/subcatalog display-name conversion
   map (e.g. "BURNERS" -> "CD/DVD Burner"), applied without touching the
   underlying folder structure or app names.
6. **Full documented pipeline order** (website tags -> release tags ->
   ignore words -> version -> arch -> edition -> language -> tidy ->
   smart-case) now lives in `config/defaults.py` and is fully editable,
   in order, from Settings > Filters -- satisfies "show all filters/regex
   so more can be tuned without code changes."
7. Underscore-normalization fix: underscores count as word characters in
   regex `\b`, which was silently breaking both version-end matching and
   `\bwww` website-tag matching on filenames like "...8.6_www...". Fixed
   by normalizing underscores to spaces before pattern matching.

### GUI improvements
- **CSV export/import** (`gui/csv_io.py`, `gui/csv_dialogs.py`): exports
  one row per app (name/catalog/subcatalog/status/confidence/alt-name/
  versions/sample file/path) with an optional batch size so a large
  catalog splits into small files easy to upload to a chat for review.
  Import reads corrected rows back by app_id and applies name/catalog/
  subcatalog changes as locked manual edits.
- **Variants table redesigned**: removed Arch/Language columns, added
  File Name (exact installer filename) and Name Source columns, switched
  to Interactive column sizing so the table scrolls horizontally instead
  of squeezing the Path column unreadably narrow.
- **Context menu on variants table**: Open file location, Run/open file
  (with a confirmation prompt since this can launch installers), Re-
  evaluate selected, Scrape selected (placeholder message, scraper isn't
  wired up yet), Ignore selected.
- **Column reordering** enabled on both the apps table and variants table
  (drag column headers to rearrange).
- **Alt-name UI**: when the resolver's non-chosen naming source differs
  from the winner, the detail panel shows it with a one-click "Use this
  instead" swap.

### Bug caught during this round's testing
`_show_variant_context_menu` was wired to `customContextMenuRequested`
but the method didn't exist -- app crashed on construction. Found via
headless smoke test before packaging, implemented, retested clean.

### Verified
- Reproduced both reported bugs in a dedicated test tree (installer
  directly under a category folder; website-suffixed filenames in both
  dot and space-obfuscated form) -- both now resolve correctly, with
  alt-name transparency preserved.
- Full GUI smoke test: variant table columns/headers, column-reorder
  flags, context-menu row selection + path computation, alt-name display,
  CSV export (batched) + import round-trip with correct locking.
- Regression: both existing test trees re-scanned/resolved clean, zero
  duplicate apps on repeated resolve (idempotency intact).

## Checkpoint 7: Real CSV data review — cascade scoring + settings-driven keyword lists

User exported and shared a real CSV (300+ apps) from an actual scan. Root-caused
four concrete accuracy issues from it:

1. **Build numbers leaking into names** ("ACDSee Build 212", "ACDSee Build 221"
   as separate apps instead of one "ACDSee" with build-qualified versions).
   Root cause: the version-pattern cascade returned on the FIRST match (e.g.
   "6.2") and never got to the build-number pattern, leaving "Build 212" in
   the name. Fixed: build-number extraction now always runs regardless of
   whether a main version was already found, folding into the version string
   ("6.2 Build 212") and stripping from the name -- confirmed all 3 ACDSee
   variants now cluster as one app.
2. **Edition variants not clustering** ("Able2Extract" vs "Able2Extract
   Professional" as separate apps). Root cause was actually the deeper
   cascade-ordering bug below, not edition detection itself (which already
   worked) -- fixed as a side effect of #4. Also expanded the edition keyword
   list (platinum, advanced, expert, elite, essentials, plus) and moved both
   `EDITION_KEYWORDS` and `LANGUAGE_KEYWORDS` out of hardcoded Python
   constants into DB-backed settings (`edition_keywords`, `language_keywords`),
   editable from Settings > Filters without touching code.
3. **Settings customization**: confirmed/completed -- every filter in the
   naming pipeline (website tags, release tags, ignore words, ignore folder
   patterns, ignore filename patterns, catalog aliases, edition keywords,
   language keywords, build-number pattern) is now DB-backed and editable
   from Settings > Filters, in the exact order applied. Filters tab wrapped
   in a scroll area since the field count grew substantial.
4. **Root cause of both #1(clustering) and #2, plus a "Converter" mis-naming
   bug**: the naming cascade picked the FIRST *valid* candidate in a fixed
   source order (folder -> file -> parent_folder), not the *best* one. This
   meant a shallow catalog/subcatalog-level folder (e.g. installer sitting
   directly in "CONVERTER") could still "win" over the file name since its
   text was technically non-generic -- reproducing the exact "Burners" bug
   from checkpoint 6 in a new guise ("Able2Extract.rar" under "CONVERTER" ->
   named "Converter"). And a genuinely richer parent-folder candidate (e.g.
   "xplorer2 Pro v2.4.0.0" with a real version+edition) lost to a bare file
   name ("Xplorer2", no version) just because file came first once the
   immediate "32 - Bit" leaf was disqualified.

   Fixed by replacing "first valid wins" with **scoring**: every valid
   candidate is scored (recognized version: +10, edition found: +3, name
   length, small per-source bonus), and folder candidates where
   `folder_depth <= 2` (the folder IS the catalog/subcatalog itself) are
   disqualified outright rather than merely de-prioritized. This is a more
   general, principled version of the depth-based fix from checkpoint 6.

### Verified
- Reconstructed the exact real folder/file structures from the user's CSV
  for all 4 issues (ACDSee builds, Able2Extract editions, xplorer2 32/64-bit
  subfolders, DAEMON Tools "Setup" folder) -- all now resolve correctly:
  single clustered apps, versions include build numbers, editions correctly
  separated, 32/64-bit variants merge into one app instead of becoming
  fake "32"/"64" apps.
- Full regression across all three existing test trees: zero duplicate apps
  on repeated resolve (idempotency intact), no unexpected naming changes
  apart from the expected "Platinum" now correctly parsing as an edition.
- Settings dialog Filters tab: new edition_keywords/language_keywords/
  build_number_pattern fields save and reload correctly.

## Checkpoint 8: Multi-file bug, portable detection, tags, file-location fix, UI scale

### Serious bug fixed: folder with multiple installers only kept one
Root cause: `raw_candidates` had `UNIQUE(scan_root_id, folder_path)` -- one
row per FOLDER, not per file. A folder containing both a portable build and
a regular installer (e.g. FreeFileSync's `FreeFileSyncPortable_6.2.paf.rar` +
`FreeFileSync_11.29_Windows_Setup.rar`) could only ever produce one
candidate. Fixed:
- Schema constraint changed to `UNIQUE(scan_root_id, folder_path,
  primary_file_name)`. **Existing databases need a fresh scan** -- this
  changes what "the same row" means, there's no safe in-place migration.
- `scanner/walker.py` now groups files in an install_unit folder by a
  "same underlying archive" key: multi-part segments (`name.part1.rar`/
  `name.part2.rar`, `name.rar`/`name.r00`/`name.r01`, `name.7z.001`/
  `.002`) correctly collapse into ONE candidate (picking the best
  representative part), while genuinely distinct installers in the same
  folder (like the FreeFileSync case) now each produce their own
  raw_candidates row / variant.

### Portable/paf apps
- Detected via regex (`portable_indicator_words` setting, default
  `["portable", "paf"]`) -- matches even while still dot-attached
  ("...6.2.paf") since regex `\b` boundaries don't require whitespace,
  unlike the token-split approach used elsewhere.
- No longer silently discarded: appended as `(Portable)` to the clean
  name, and recorded as `is_portable` through ExtractedFields ->
  ClusterMember -> Cluster.has_portable_variant.

### Tags column (comma-separated, many-to-many)
- Reused the existing `tags`/`app_tags` schema (previously unused).
  `resolve_job._sync_app_tags()` links catalog, subcatalog, and "Portable"
  (when applicable) as tags on every resolved app -- built to extend
  naturally once the scraper adds real categories later.
- Added a "Tags" column (GROUP_CONCAT) to the GUI apps table and to the
  CSV export.

### Naming pipeline fixes from this round of real-data feedback
- **"Free" false-positive**: `free` was in the default edition-keyword
  list, incorrectly stripping the literal first word of real brand names
  ("FreeFileSync", "FreeArc"). Removed from the default list.
- **Multi-part `.partN` leaking into names**: `BigApp.part1.rar` was
  resolving to "Big App part1" -- added `\bpart\s*\d+\b` to
  release_tag_patterns.
- Added "ultra", "aio" to edition_keywords (so DAEMON Tools Ultra/
  Advanced/Pro/Lite variants correctly cluster as one app via shared
  edition-stripped base name) and "sptd"/"with" as filler words (for
  "DAEMON Tools ... with SPTD" style bundling mentions).
- Added serial/license/license-key/key(s) to ignore_filename_words,
  standalone "+" and dual-architecture mentions ("32+64 Bits", "x86+x64")
  to release_tag_patterns, and space-separated bitness forms ("32 bit",
  "64 bit") to architecture_keywords (previously only had "32bit"/
  "32-bit" without-space and with-dash forms).
- Confirmed already-working from prior session: redundant bare
  major-version-number stripping (e.g. duplicate "13" before
  "13.0.1.194" in "FileMaker Pro 13 Advanced 13.0.1.194").

### "Open file location" bug fixed
Root cause: `subprocess.run(["explorer", f"/select,{full_path}"])` -- when
the path contains spaces (extremely common in this dataset), Python's
list-based subprocess quoting wraps the ENTIRE `/select,<path>` argument
in quotes, but Explorer expects quotes only around the path portion
(`/select,"C:\path\file.exe"`). The mismatch makes Explorer silently fall
back to its default folder -- exactly the "opens only Documents" symptom
reported. Fixed by using `shell=True` with a manually-quoted command
string, matching Explorer's actual expected syntax. Also added an
explicit file-existence check beforehand with a clear dialog instead of
silently opening the wrong place when a recorded path is stale.

### Global UI scale multiplier
`ui_scale_multiplier` setting (default 1.0), applied via Qt's
`QT_SCALE_FACTOR` mechanism, which must be set before `QApplication` is
constructed -- `run_gui.py` now reads it via a raw DB connection first.
Takes effect on next launch (documented in the Settings > Appearance tab
rather than promised as live, since Qt doesn't support live-rescaling
reliably).

### Verified
- Reconstructed FreeFileSync (two distinct installers, one folder) and a
  3-part multi-archive test case: multi-file bug fixed, multi-part
  archives still correctly collapse to one candidate, "(Portable)"
  suffix and Portable tag applied correctly, "Free" and ".part1" no
  longer leak into names.
- Full regression across all four test trees: zero regressions, zero
  duplicate apps on repeated resolve.
- GUI smoke test: Tags column populated correctly, Appearance tab
  save/reload works, open-file-location code path runs without crashing.

## Checkpoint 9 (partial): Real CSV review round 2 — resolver correctness fixes

### Fixed and verified
1. **"by UploaderName" not stripping mid-string**: pattern required end-of-
   string anchor ($), but a version often follows ("...by Jiri Mahel-v2.2").
   Removed the anchor.
2. **Dot-glued words never tokenized**: "CubeDesktop.Pro", "FileMaker.Pro.13.
   Advanced" -- dots were only converted to spaces at the very end (_tidy_
   name), so edition/language token-matching, which runs on whitespace-split
   tokens, never saw "Pro"/"Advanced" as separate tokens at all. Fixed by
   converting dots/underscores to spaces (and re-running camelCase split +
   ignore-word filtering) immediately after version extraction -- late enough
   that the version regex still sees intact dotted numbers, early enough
   that edition/language/architecture matching sees real tokens.
3. **"Setup"/"Install" glued directly to a version with no separator**
   ("AirtableSetup1.3.2" -> was "AirtableSetup1" + version "3.2"): root
   cause was my own `\bsetup\d*\b` pattern from checkpoint 8, which
   greedily consumed the leading digit of the version along with the word.
   Removed that pattern; replaced with a lookahead that strips only the
   word ("setup"/etc, reusing ignore_filename_words) without touching the
   digits that follow, so the version regex sees the complete number
   afterward. Verified: Airtable -> "1.3.2", NexusFont -> "2.5.8", both
   correct.
4. **Version regex silently truncating 5+ digit build numbers**: `\d{1,4}`
   per segment meant "15.0.02200" (5-digit final segment) could only match
   as far as "15.0" -- no `\b` boundary existed after a 4-digit cap left a
   5th digit dangling. Widened to `\d{1,6}`.
5. **Year glued directly to a word with no separator** ("Nero2014"): can't
   be split by camelCase (no case transition) or dot-conversion (no dot).
   Added a narrow, safe fix: only a recognizable 19xx/20xx year immediately
   after a letter gets space-inserted -- deliberately NOT any trailing
   digits, since short numeric suffixes are frequently real brand identity
   (Windows7, GTA5, Office365) and splitting those would be wrong.
6. **Leftover bare numeric tokens** (2-4 digits, no letters) remaining
   after edition/version processing now fold into the version string
   instead of staying stuck in the name -- fixes "Nero 2014 Platinum
   15.0.02200" style names losing the year into the wrong field.
7. **"+serial"/"+" not stripping when glued with no surrounding spaces**:
   added a bare `\+` release-tag pattern, and added "+" to the punctuation-
   strip charset used by every token-boundary check (edition/language/
   ignore-word matching), which previously only stripped
   `,.-_()[]` and left a leading "+" attached to the token.
8. **Crack/Patch/Keygen/Update/Serial/License folders now excluded
   entirely** (not scanned as install candidates at all), not just
   deprioritized for naming -- confirmed via the user's Nero example
   (a `Crack` subfolder containing a `_patch.rar` no longer produces a
   spurious extra "app"). Important refinement caught via regression
   testing: a naive word-boundary match on the WHOLE folder name would
   have wrongly excluded legitimate install folders that merely *mention*
   "Keygen" as one release-tag descriptor among much more content (e.g.
   "Adobe.Captivate.v2.0.1177.WinALL.Keygen.Only-ViRiLiTY" -- 50+ chars,
   clearly the real install folder, not a bare keygen dump). Fixed with a
   length guard (`noise_short_only_max_len`, default 25 chars): these
   specific words only trigger exclusion when the folder name is short
   enough to plausibly BE just that word.
9. **Catalog/subcatalog (and therefore Tags) title-cased**: reused the
   existing smart-case function (only re-cases ALL-CAPS/all-lowercase,
   leaves genuinely mixed-case text alone) via `_apply_alias`, so
   "GRAPHICS"/"CD DVD RECORDER" display as "Graphics"/"Cd Dvd Recorder".
   Explicit `folder_name_aliases` entries still take priority for exact
   wording needs.

### Verified
Reconstructed all of the above as a dedicated test tree and confirmed
correct output for every case. Full regression across all 5 accumulated
test trees: zero regressions, idempotent (no duplicate apps on repeated
resolve).

### Still pending from this round of feedback (not started)
- Collection folders (Rainmeter skins, screensavers) currently still
  become one "app" per file/skin rather than being grouped as variants of
  a single consolidated entry.
- Left panel: dropdown to filter by Catalog / Subcatalog / Tag (currently
  catalog-only).
- Confirmed conceptually working (crack/patch exclusion) but no dedicated
  GUI-level test yet.

## Checkpoint 10: Module consolidation
Per request, restructured from ~20 fine-grained files across 6 subpackages
into 5 flat top-level modules, function-for-function identical:
- `appcatalog/database.py` (was db/schema.py + db/connection.py)
- `appcatalog/scanner.py` (was scanner/pe_metadata.py, archive_inspect.py,
  classify.py, fingerprint.py, walker.py, scan_job.py)
- `appcatalog/resolver.py` (was resolver/extract.py, cluster.py,
  resolve_job.py, edit_ops.py)
- `appcatalog/gui.py` (was gui/models.py, detail_panel.py, main_window.py,
  settings_dialog.py, jobs.py, csv_io.py, csv_dialogs.py,
  reresolve_dialog.py, app_picker_dialog.py)
- `appcatalog/config.py` (was config/defaults.py)
- `appcatalog/scraper/` left as its own placeholder package since it's
  still unimplemented (phase 2, deferred).

`run_gui.py` updated: `from appcatalog.gui import MainWindow`,
`from appcatalog.database import Database`.

Verified behavior-identical: ran the full scan+resolve regression suite
(all 5 accumulated test trees, idempotency check) against the new
structure and got byte-identical results to the pre-consolidation
structure, then re-verified against the actual files being delivered
(not just the scratch copy used to build them). GUI construction,
selection, and Settings dialog also re-smoke-tested successfully.

## Checkpoint 11: GUI sortable columns + Scanned date, naming/scanning fixes from screenshot + DAEMON Tools log review

### GUI
1. **Columns weren't actually sortable.** `setSortingEnabled(True)` was
   already set on the apps QTableView, but `QAbstractTableModel.sort()` is
   a no-op unless overridden -- the header showed a sort arrow but nothing
   reordered. Added a real `sort()` to `AppsTableModel` (text columns
   case-insensitive, confidence/variant_count numeric, blanks sort last).
   Variants table (QTableWidget) needed `setSortingEnabled(True)` too, plus
   toggling it off/on around row population (QTableWidget re-sorts on every
   `setItem()` otherwise, which both scrambles population and is slow).
2. **Added a "Scanned" column** to both tables, sourced from
   `raw_candidates.first_seen_at` (already existed in the schema, no
   migration needed) via `MIN(r.first_seen_at)` per app / the variant's own
   row. Shown as a date, full timestamp in the variant row's tooltip.

### Scanner/naming fixes (from the Isdel/WinGlobe screenshot + DAEMON Tools log)
1. **"Isdel" winning over the real app name.** `isdel.exe` (a common
   InstallShield self-delete stub) isn't noise-worthy enough to already be
   filtered, and it was even winning over `setup.exe` sitting in the same
   folder because *each* installer file becomes its own candidate/variant
   (correct, per checkpoint 8) -- the name cascade should have fallen
   through to the parent folder ("WinGlobe") but had nothing to disqualify
   "isdel" as a name. Added "isdel" (and "contactpack", another release-tag
   word from the same log) to `ignore_filename_words`.
2. **Version lost when the winning name source has none.** In the WinGlobe
   case the leaf folder ("1.1"/"2.1") correctly loses the NAME cascade
   (it's just a bare version number) but was also silently losing the
   VERSION with it, since only the winning candidate's own extraction was
   ever used. Added a fallback: if the winning candidate has no version,
   scan the other candidates (folder/file/parent) for one and adopt it.
   Verified: WinGlobe 1.1 and 2.1 now resolve to name="WinGlobe"-derived,
   version="1.1"/"2.1" respectively, instead of both losing their version
   entirely.
3. **Skin/Theme/Patch/Serial/Crack/Update folders should be excluded
   entirely**, same treatment as the crack/patch/keygen exclusion added in
   checkpoint 9. Added "skin", "skins", "theme", "themes", "patches",
   "cracks" to `noise_folder_keywords`/`noise_short_only_keywords` (already
   surfaced in Settings > Filters, no new UI needed).
4. **Bracket-wrapped content (`{...}`, `[...]`, `(...)`) is release/
   uploader noise and should be stripped entirely**, not just have its
   brackets removed -- added a new `bracket_content_patterns` setting,
   applied as the very first pipeline step (before website/release-tag
   patterns) so junk like uploader tags or "(with SPTD 1.83)" can't
   confuse later version/edition matching.
5. Added "suite", "hd" to `edition_keywords`.
6. **Multiple edition keywords in one name** ("DAEMON Tools Pro Advanced"
   mentions two edition words) previously only stripped the first match,
   leaving the second stuck in the name and preventing it from clustering
   with plain "DAEMON Tools"/"DAEMON Tools Ultra". Now strips every
   matching token; the *first* match found is still kept as the single
   `edition` field value.
7. **DAEMON Tools not showing up** (investigated against the user's actual
   scanner log): root-caused to a combination of the above -- glued
   release/build codes (`DAEMONToolsPro4400312-0224`) left undigested
   numeric junk stuck to the name and to the edition word, both preventing
   the family from normalizing to the same clustering key, compounded by
   only the first of two edition words being stripped. Additional targeted
   fixes:
   - A longer digit run (4+ digits) glued directly to a word with no
     separator ("Pro4400312") now gets a space inserted before it, the
     same way the existing year-glue fix works, so "Pro" becomes an
     isolated, matchable edition token. Deliberately requires 4+ digits so
     real glued brand suffixes (Windows7, GTA5, Office365) are untouched.
   - Leftover bare numeric tokens folded into the version were widened
     from 2-4 digits to 2-8 digits (catches longer build codes like the
     7-digit "4400312"), and a hyphenated-but-otherwise-all-digit token
     ("4400312-0224") is now also recognized and folded in, instead of
     being left stuck in the name.
   - Minimum valid name length raised from 2 to 3 characters: a 2-char
     leftover after edition/build-code stripping ("DT" from
     "DTLite4413-0173.rar" once "Lite" and the build code were removed) is
     almost never a real product name on its own -- falling through to the
     next candidate (usually a fuller folder name) is the better default.
   - Reduced the length-based scoring tie-breaker's weight (0.2 -> 0.05,
     cap 30 -> 20 chars). It was previously strong enough that a long,
     noisy filename with an undigested build code stuck to it could
     outscore a short, clean folder name purely by being longer -- now
     that build codes are cleaned up earlier in the pipeline this matters
     much less, but the weight reduction adds a safety margin regardless.
   - **Tried and reverted**: an additional camelCase-split rule to handle
     acronym-glued-to-word names ("DAEMONTools" -> "DAEMON Tools") without
     a space at all. It technically fixed that one case, but it also
     broke real, already-correct brand names elsewhere in this exact
     dataset -- "ACDSee" -> "ACD See", "CCleaner" -> "C Cleaner", "IObit"
     -> "I Obit". Splitting an acronym-run from a following word is
     fundamentally ambiguous without a real dictionary (there's no way to
     tell "DAEMON"+"Tools" from "ACD"+"See" by shape alone), and breaking
     recognizable brand names for many apps is a worse trade than one
     glued filename staying imperfectly named. Reverted; the fixes above
     already cover the clustering for that install tree except for one
     residual case (see Verified below).
   - Also verified but NOT changed: three sibling subfolders under DAEMON
     Tools Ultra's repack (`Other.languages`, `Setup`, `SPTD`) legitimately
     contain a `.rar` directly and so are correctly classified as their
     own install_units by existing logic -- they surface as small,
     low-value extra "apps" (language pack / setup stub / driver). This is
     a real but narrow edge case (an archive sitting in a component
     subfolder of an already-detected app) that would need a materially
     different rule (e.g. "an archive in a folder whose name matches a
     known component/driver keyword list is folded into its parent's
     variant rather than becoming its own install_unit") -- left as a
     known limitation rather than risking a rule broad enough to start
     swallowing genuinely separate archives sitting in innocuously-named
     folders.
8. **Multi-version files sitting flat in one shared folder** (no per-
   version subfolder) -- confirmed already handled correctly by the
   per-file grouping fixed in checkpoint 8 (`_group_installer_files`):
   each distinct installer file becomes its own `raw_candidates` row/
   variant regardless of whether it has its own subfolder, and version
   extraction happens from the file name directly, so this needed no
   further change. Re-verified against the WinGlobe 1.1/2.1 case (item 2
   above), which is exactly this pattern.
9. **Existing databases pick up the new keyword defaults automatically.**
   `_ensure_defaults()` only seeds a setting the first time a key is ever
   seen, so an existing `catalog.db` already has an old
   `ignore_filename_words`/etc list saved and would never see "isdel" or
   the other additions above without manual editing. Added
   `_merge_new_keyword_defaults()`, run once after `_ensure_defaults()` on
   every connect: unions newly-introduced default entries into the four
   affected list-settings (case-insensitive, preserves the user's own
   custom entries/ordering, never removes anything).

### Verified
- Reconstructed the exact WinGlobe/isdel case from the screenshot: now
  resolves to a WinGlobe-derived name with version 1.1/2.1 correctly
  picked up from the leaf folder (previously named "Isdel", no version).
- Reconstructed all 15 log lines from the user's DAEMON Tools report as a
  cluster-simulation test: 5 of 6 distinct-name variants tested now
  cluster into one "DAEMON Tools" app (up from fragmenting into several
  differently-named apps). One residual case (`DTLite4413-0173.rar`, an
  abbreviated filename with no space and no separator at all between the
  acronym and "Lite") still resolves to its own small "DTLite" app rather
  than joining the cluster -- mergeable manually via the GUI's existing
  merge action; not attempting a further automatic fix given the acronym-
  split trade-off discovered above.
- Full regression re-run of every named case from checkpoints 7-9
  (ACDSee build clustering, Able2Extract editions, xplorer2 32/64-bit
  merge, FreeFileSync Free/Portable, CubeDesktop.Pro/FileMaker dot-glued
  tokenization, Airtable/NexusFont setup-glued-to-version, K-Lite,
  long keygen-mention folder naming) -- all still produce the same
  expected output after this round's changes.
- `_merge_new_keyword_defaults()` smoke-tested against a simulated
  pre-checkpoint-11 DB (seeded with only the old short keyword list):
  confirms new words get merged in while existing custom entries survive.
- **Follow-up catch**: the two new settings added this round
  (`noise_short_only_keywords`, `bracket_content_patterns`) plus the
  pre-existing but never-exposed `noise_short_only_max_len` were only in
  `config.py`/the DB, not in the Settings dialog -- broke the project's
  own "every filter is editable from Settings" principle. Added a
  "short-only" keyword box + max-length spinbox to the Keywords tab, and
  a "1. Bracket-content patterns" box at the top of the Filters tab
  (renumbering the rest). `_save()` wired up for all three; smoke-tested
  end-to-end (open dialog, edit each new field, save, reload settings,
  values match) and a full `MainWindow` construction with a seeded app/
  variant confirms the apps table sorts, the variants table shows the new
  Scanned column with real data, and the scanned-date aggregate query
  works against the real schema (not just the resolver in isolation).

## Checkpoint 12: Scraper Phase 1 — Winget manifest (svrooij) + Chocolatey search

Implemented per the attached Scraper Implementation Report, scoped down to
what was actually requested ("winget index_v2 local manifest, then choco
scraping") rather than the report's full v2.5 design -- see the
clarification message sent before this round for the explicit scope split
and the reasoning behind each schema/UI decision below.

### Schema (additive migration, safe on existing DBs)
Added to `apps`: `winget_id`, `choco_id`, `manifest_name`, `alt_source_name`,
`latest_version`, `last_scraped`. Reused existing columns for the report's
differently-named equivalents rather than duplicating them: `publisher`
("company"), `homepage_url` ("url"), `scrape_status` ("scrap_status").
Scraped tags feed into the *existing* `tags`/`app_tags` many-to-many tables
(same ones the resolver already populates with catalog/subcatalog/
"Portable") rather than a new flat string column, so they show up
consistently wherever tags are already used in the GUI.

### `appcatalog/scraper/manifest.py`
Downloads/caches `svrooij/winget-pkgs-index`'s `index.v2.json`. Verified the
real file's structure directly (not just the report's description) before
writing the parser: a flat JSON array of ~14,000 `{Name, PackageId, Version,
Tags, LastUpdate}` objects, no description/company/homepage fields at all
-- confirms the report's own "Limitations" section. Synthesizes a generic
description and a heuristic company (first PackageId segment) exactly as
documented there.

Cache lives next to `catalog.db` (`winget_manifest_cache.json`), not a
hidden profile folder, matching this app's "everything lives with the .db
file" design. Re-download only happens if the cache is older than
`scraper_manifest_staleness_hours` (default 4, configurable) or the user
forces a refresh; falls back to a stale cache on network failure rather
than leaving background enrichment entirely unable to run.

Lookup keys use `resolver.normalize_key()` directly -- the SAME normalizer
the scanner/resolver already use for app-name clustering -- rather than
re-implementing the report's own separate `normalize_name()`, per the
report's own "Consistency" goal.

### `appcatalog/scraper/enrich.py`
`run_scrape()` -- background, manifest-only (no per-app network call,
matching the report's explicit statement that Chocolatey is "used
exclusively in the Manual Match Dialog", i.e. never in the background
batch path). `app_ids=None` restricts to `scraper_auto_enrich_statuses`
(default `resolved`/`verified` -- deliberately excludes `needs_review`/
`ignored`, since a manifest hit on a name that might still change risks
attaching metadata to the wrong app); an explicit app_id list (from
"Scrape selected") bypasses that filter since the user chose them directly.

`apply_choco_candidate()` -- one specific, user-picked Chocolatey result
applied to one app. Unlike the manifest path, always overwrites
description/company/website/version with the chosen candidate (a user pick
is a stronger signal than an automatic name-normalized match), and only
renames if the caller explicitly asks (`choose_name=True`).

Both respect `name_locked` regardless of the `scraper_auto_rename` setting,
and both write through one shared `_apply_updates()` so the merge rules
live in exactly one place.

### GUI
- "Scrape selected" (previously a placeholder message) → `ScrapeWorker`
  (QThread, mirrors `ResolveWorker`/`ScanWorker`'s shape) → re-loads the
  detail panel and refreshes the table on completion.
- New toolbar button "Scrape all (Winget)" → same worker with
  `app_ids=None`, for the report's background-batch use case.
- New "Search Chocolatey…" context-menu item → `ChocoSearchDialog`: search
  box (pre-filled with the app's current name) + max-results spinner +
  results table + Apply, with a yes/no prompt for whether to also rename.
  This is a deliberately simpler stand-in for the report's full
  `ManualMatchDialog` (side-by-side Winget+Choco tables, three-way
  Original/Winget/Choco name-choice dialog) -- deferred, see below.
- **Added missing display fields**: the detail panel previously had
  nowhere to actually show `publisher`/`homepage_url`/`latest_version`/
  `winget_id`/`choco_id`/`last_scraped` even before this round -- only
  `description` was wired to a visible field. Caught this while smoke-
  testing (a scrape that succeeds but is invisible in the GUI isn't a
  usable feature) and added read-only Publisher/Homepage/Latest
  version/Scrape-source fields.
- New Settings > Scraper tab: manifest URL, staleness window, auto-rename
  toggle (off by default -- see reasoning in config.py), eligible-status
  list for "Scrape all", default Chocolatey result count. None of these
  bump `settings_version` (they don't affect resolver naming logic, so
  bumping would incorrectly flag every app as needing re-resolution).

### Verified
- Manifest fetch/cache/build_lookup tested against the REAL live file (not
  a mock) -- confirmed real structure, confirmed cache-hit / force-refresh
  / stale-fallback-on-network-failure all work, confirmed real lookups
  (7-Zip, VLC, Google Chrome) resolve correctly via the shared
  `normalize_key()`.
- `run_scrape()`: status filtering (`needs_review` correctly skipped),
  `name_locked` respected under `scraper_auto_rename=True`, tag merging
  idempotent across repeated runs (no duplicate `app_tags` rows), publisher/
  description/version fields populate correctly.
- `apply_choco_candidate()`: tested with a synthetic candidate shaped
  exactly like `choco_search.search_chocolatey()`'s real return format;
  rename-vs-no-rename both verified.
- Full headless `MainWindow` + `SettingsDialog` + `ChocoSearchDialog`
  construction against a real seeded DB: Scraper settings tab saves and
  reloads correctly, a real scrape via `run_scrape()` populates the new
  detail-panel fields (publisher/latest version/scrape source all showed
  the real Winget data end-to-end), a follow-up `apply_choco_candidate()`
  correctly overlays the Chocolatey values on top.
- **Bug caught and fixed during testing**: my first pass at the new
  detail-panel fields used `app.get(...)`, but `apps` rows come back as
  `sqlite3.Row` (no `.get()` method) everywhere else in this file --
  would have crashed on every app load. Fixed to plain `app["..."]`
  indexing before shipping.

### Testing limitation (disclosed up front, not discovered as a surprise)
`community.chocolatey.org` isn't on this sandbox's network allowlist, so
`choco_search.py` itself (the live HTML scraping/parsing logic, copied
in as-is from the attachment) could not be network-tested here -- only the
DB-write half (`apply_choco_candidate`) was verified, using a synthetic
result shaped like its documented output. Needs one real run on a machine
with normal internet access to confirm the live search path end-to-end.

### Deferred (Phase 2, not started)
- `ChrisTitusTech/winutil` fallback manifest (richer descriptions/
  categories, ~700 apps).
- `winget show` CLI fallback (Windows-only; can't test in this sandbox
  regardless).
- Full `ManualMatchDialog` (side-by-side Winget+Choco candidate tables,
  three-way name-choice dialog, `_merge_candidates()` multi-source
  aggregation with comma-separated multi-value fields). The simpler
  single-source `ChocoSearchDialog` built this round covers the "choco
  scraping" request on its own; the fuller aggregation UI is a bigger,
  separate piece of work.
- Thread-safety wrapping (`threading.Lock`) called out in the report's
  section 7.2 -- not yet needed since nothing here runs the scraper and
  another DB-writing job concurrently (`_active_worker` already prevents
  overlapping jobs from the GUI), but would matter if that changes.

### Unrelated bug noticed, not fixed
`AppPickerDialog` is referenced (merge-apps / move-variant-to-app menu
actions) but is not defined anywhere in the codebase -- calling either
action would raise `NameError`. Pre-existing, unrelated to this round;
flagged to the user rather than silently fixed or left unmentioned.

### Still pending (carried over from earlier checkpoints, untouched this round)
- Three sibling archive-in-component-subfolder "apps" under DAEMON Tools
  Ultra's repack (`Other.languages`, `Setup`, `SPTD`) -- known limitation,
  see checkpoint 11 item 7.
- Collection folders (Rainmeter skins, screensavers) still one "app" per
  file rather than grouped (carried over from checkpoint 9).
- Catalog/Subcatalog/Tag filter dropdown in the left panel (carried over
  from checkpoint 9).


## Checkpoint 13: making scraping actually usable/discoverable in the GUI

Prompted by real usage feedback after checkpoint 12: "Scrape all" ran fine
(25/350 matched) but there was no way to see *which* 325 didn't match, and
"Search Chocolatey…" was only reachable by opening an app then right-
clicking one of its variant rows -- not where anyone would look for an
app-level action.

### Changes
- **New "Scraped" column** on the main apps table (`scrape_status`, shown
  as `not_scraped`/`scraped`/`pending`/`failed` -- NULL rows from before
  this column existed also display as `not_scraped` rather than blank).
- **New "Scraped:" filter dropdown** next to the existing resolver-status
  filter -- lets you isolate exactly the unmatched apps to work through
  manually, which is the actual answer to "where do I find the ones that
  didn't scrape".
- **Apps table now has a real context menu** (right-click a row, or
  multi-select with Ctrl/Shift-click first): "Scrape selected from
  Winget" (batch, any number of rows) and "Search Chocolatey…" (single
  row only -- opens `ChocoSearchDialog` directly, no detour through the
  variants table). The variants-table versions from checkpoint 12 still
  work too, just no longer the only path.
- Apps table selection mode changed from Single to Extended to support
  multi-row "Scrape selected" from the new context menu.

### Verified
Headless smoke test: seeded 4 apps (`7-Zip`, `VLC`, two made-up names),
ran `run_scrape()`, confirmed the "Scraped:" filter correctly isolates
matched vs. unmatched, confirmed the "Scraped" column displays the right
label per row, confirmed `_selected_app_ids()` correctly reads back a
multi-row selection for the new context menu's batch scrape action.

**Real finding surfaced by this test, worth knowing when using it**: `VLC`
(bare) did NOT match the manifest, even though `VLC media player` did in
checkpoint 12's test. The lookup is an EXACT normalized-name match against
the manifest's own `Name` field, not fuzzy -- an app resolved down to a
short/abbreviated name (`VLC`, `Chrome`, `Office`) will very often miss a
manifest entry that's stored under its fuller marketing name (`VLC media
player`, `Google Chrome`, `Microsoft 365`), not because the app isn't in
Winget at all. This is exactly the gap the Chocolatey manual-search path
covers (it searches by term/fuzzy match against the live site rather than
requiring an exact key match) -- expect a meaningful chunk of the
"not_scraped" apps after "Scrape all" to be this, not real absence from
Winget's catalog. A future improvement could try a few fallback lookup
strategies (e.g. prefix/substring match against manifest keys) before
giving up, but that risks false-positive matches and wasn't attempted
here without being asked for it specifically.

## Checkpoint 14: manual Winget search, real descriptions via winget show, full metadata visibility, scan-root re-scan UI

Direct follow-up to five specific asks after using checkpoint 13.

### 1. Context menu improvements
Renamed for clarity now that there are two distinct scrape paths:
"Auto-scrape selected from Winget" (exact-key batch match, unchanged
behavior) vs. "Search & match…" (manual, fuzzy, single source at a time --
see #2). Both still on the apps table AND the variants-table context menu.

### 2. "Search selected from Winget" should work like Chocolatey -- manual search + match
Added `manifest.search_manifest()`: fuzzy/substring search against the
already-cached LOCAL manifest (no network call -- the manifest is already
in memory), returning results in the exact same dict shape as
`choco_search.search_chocolatey()`. Merged what was `ChocoSearchDialog`
into one `SearchMatchDialog` with a "Source:" dropdown (Winget local /
Chocolatey live) instead of two near-duplicate dialogs -- same search box,
results table, and Apply flow either way, just a different worker/apply
function underneath. `WingetSearchWorker` added alongside the existing
`ChocoSearchWorker`.

Search ranking iterated on based on live testing against the real
manifest: a flat substring/fuzzy score put irrelevant mid-string matches
("Jellyfin VLC Bridge") ahead of the obviously-correct answer ("VLC media
player") for a "VLC" search. Fixed with a tiered scorer (exact key match >
starts-with > whole-word-substring > raw substring > rapidfuzz WRatio
fallback), verified against VLC/Chrome/7zip/Office/Reader -- the intended
match now lands in the top few results every time, even where it's not
literally #1 for a genuinely ambiguous term like "Chrome" (many
Chrome-branded utilities exist in the manifest independent of Google
Chrome itself).

### 3. Auto-scrape description quality ("Appname - Windows application" is wrong)
This generic placeholder is literally the best the bare manifest can do on
its own (confirmed -- it has no description/publisher/license fields at
all, see manifest.py's docstring). Implemented the report's `winget show`
fallback (section 2.4), deferred from checkpoint 12:
- New `scraper/winget_show.py`: shells out to `winget show --id <id>
  --exact`, parses Publisher/Description/Homepage/License/Version/Tags
  from the text output.
- **Caveat, stated plainly**: this cannot be tested against a real `winget
  show` invocation from this sandbox (Linux, no winget CLI at all). Built
  and tested the PARSER against a realistic sample output shaped like
  Microsoft's documented format, and caught a real bug this way before it
  shipped -- an early version let a "Publisher Url:" line win over a later
  real "Homepage:" line for the homepage field, because both share a
  fallback relationship in the label list and the first version resolved
  labels in document order rather than priority order. Fixed and
  reverified against both the direct-match and the fallback-when-missing
  case. Full CLI subprocess behavior (timeouts, exit codes, whatever
  wording variance exists across winget CLI versions) is unverified beyond
  this and needs a first real Windows run to confirm.
- Wired into BOTH paths: `run_scrape()` (batch, OFF by default via
  `scraper_winget_show_for_auto_scrape` -- one extra subprocess call per
  matched app could add real time to a 350-app batch) and
  `apply_manifest_candidate()` (manual "Search & match…" apply, ALWAYS
  attempted since it's only ever one call for a user-confirmed pick).
  Verified end-to-end with the CLI call itself mocked (can't invoke the
  real binary here) but the full merge/override logic exercised for real:
  a matched app's description/publisher/license/homepage/version correctly
  come from the winget-show result when available, falling back to the
  manifest placeholder when winget isn't installed or the call fails.

### 4 & 5. Detail panel + table should show all available metadata
- Detail panel: new "Scraper metadata" group box with its own labeled
  field per item (Publisher, Homepage, License, Latest version, Winget ID,
  Chocolatey ID, Manifest name, Name before auto-rename, Tags, Scrape
  status) -- previously winget_id/choco_id were folded parenthetically
  into the version line and license/manifest_name/alt_source_name/tags had
  no UI at all.
- Apps table: added Publisher/Latest Version/Winget ID/Choco ID/License
  columns alongside the existing Scraped/Status/Tags ones. Column-drag-
  reorder (already supported) lets a wide table be tidied per user
  preference; no explicit column show/hide control was added given the
  scope of this round.
- New `license` column on `apps` (additive migration).

### 6. Added/Last-scanned columns + easy re-scan
- Renamed the apps table's "Scanned" column to "Added" (`MIN(first_seen_at)`,
  unchanged meaning, clearer label) and added a new "Last Scanned"
  column (`MAX(last_seen_at)`) -- these can now differ meaningfully once a
  root's been re-scanned more than once.
- Confirmed the underlying incremental-rescan-skip-unchanged/pick-up-new
  behavior was ALREADY implemened (`scanner.py`'s fingerprint check +
  upsert into raw_candidates, since early checkpoints) -- the missing
  piece was just a convenient way to TRIGGER it repeatedly without
  re-browsing to the same folder from scratch each time. Added a new
  "Scan roots…" toolbar button opening `ScanRootsDialog`: lists every
  previously-added root with its last-scan timestamps/status and a
  "Re-scan now" button per row (reuses the existing
  `_run_scan_and_resolve()` worker path, so it's the identical scan+resolve
  flow, just without re-picking the folder).

### Verified (real, not asserted)
- `search_manifest()` tested against the live 14k-entry manifest for 5
  different real-world query terms, iterated twice on ranking quality
  based on actual results.
- `winget_show` output parser tested against a realistic sample, caught
  and fixed a real label-priority bug before it shipped.
- Full `run_scrape()` + `apply_manifest_candidate()` merge logic tested
  with the winget-show call mocked to return success, confirming the
  override/fallback chain (winget_show > manifest > existing stored value)
  works correctly at every level.
- Full GUI-level test: constructed `SearchMatchDialog` for real, ran an
  actual `WingetSearchWorker` QThread against the live network manifest,
  waited for completion, verified the results table populated and the
  description preview updated on selection -- not just the underlying
  functions in isolation.
- `ScanRootsDialog` constructed against a seeded `scan_roots` row, confirmed
  the table populates with the right path/timestamps.
- Apps table with all new columns (added_at/last_scanned_at distinct
  values) constructed and read back correctly via a seeded DB with two
  different raw_candidates timestamps.
- Full resolver regression (WinGlobe/isdel case) re-confirmed unaffected.
- Cold-start (brand new empty DB) `MainWindow` construction re-confirmed
  working after all of the above.

## Checkpoint 15: flattened module structure + features adopted from a parallel DeepSeek build

### Restructuring (prerequisite for everything else this round)
Flattened from a package (`appcatalog/` folder + `appcatalog/scraper/`
subpackage, relative imports) to 8 top-level `.py` files with plain flat
imports, no subfolders, no `__init__.py`. Reason: zip files can't be
uploaded to some AI chat interfaces (including DeepSeek's), and a nested
package structure makes it harder for a model working from a partial
upload to build a correct mental map. Specifically:
- `appcatalog/scraper/{manifest.py, enrich.py, winget_show.py}` merged
  into one `scraper.py` (kept internally organized into 3 clearly marked
  parts). `choco_search.py` kept as its own file deliberately (see
  AI_MODULE_REFERENCE.md section 6 for why).
- All `from .x import y` / `from ..x import y` changed to `from x import y`.
- `run_gui.py`'s `from appcatalog.database import Database` etc. changed
  to flat imports.

**Verified nothing broke**: re-ran the full existing regression suite
against the flattened structure before building anything new on top of
it -- WinGlobe/isdel resolver case, full `MainWindow` construction against
a seeded DB, a real `run_scrape()` call against the live Winget manifest,
a real `SearchMatchDialog` search through an actual `WingetSearchWorker`
QThread. All passed identically to the pre-flatten structure.

### Comparison against a parallel DeepSeek build of this app
User provided a separate, independently-built version of this app
(PyQt5-based, ~6,200 lines across 11 flat files) for a side-by-side
review before any coding, per explicit request. Full comparison:

**Not adopted** (this build's own scanner/resolver/config approach is
meaningfully more accurate and was kept as-is): the DeepSeek scanner has
no fingerprinting/incremental-rescan-skip, no archive content inspection,
no PE metadata reading, no cascading multi-candidate name resolution, no
confidence scoring, none of the bracket-stripping/build-number-folding/
multi-edition-stripping accuracy work from checkpoints 7-11. Its config is
a plain YAML file rather than DB-backed/versioned/GUI-editable settings.
Its `winget show` output parser only captures a single line of
Description (would truncate any wrapped multi-line description) and
appears to expect tags space-separated on the same line as the `Tags:`
label (the real format lists them one-per-line below it) -- this build's
own parser (already multi-line-aware for both) was judged more correct
and kept as-is, though still only validated against a realistic sample,
not a live run (see scraper.py Part 2 caveat).

**Adopted, described below**: `app_name_synonyms`, `category_rules`/
`subcategory_rules`, duplicate-app detection, category/subcategory
rename-merge as first-class actions, a structural health report, column
visibility toggle, and (rebuilt more conservatively, not ported as-is)
physical file reorganization.

### 1. `app_name_synonyms` (resolver.py)
Regex -> canonical-name rewrite rules, applied as the LAST step of name
cleaning (`_apply_name_synonyms()`), after all other cleaning but before
clustering. Direct fix for the checkpoint 11 case that fuzzy clustering
alone couldn't fully guarantee: a real folder-name typo ("Deamon Tools"
vs. the actual product "DAEMON Tools") where the fuzzy ratio between the
two spellings could land on either side of `fuzzy_match_threshold`
depending on exact wording. Default rule ships covering exactly this
case. Verified: both the typo'd folder-based candidate and the correctly-
spelled candidates now normalize to the identical `normalize_key()`,
guaranteeing they cluster as one app regardless of fuzzy-match luck.

### 2. `category_rules` / `subcategory_rules` (scanner.py)
Regex-against-full-relative-path rules (first-match-wins, case-
insensitive), checked in `_derive_catalog_subcatalog()` BEFORE the
existing "first path segment = catalog, second = subcatalog" fallback.
Lets a messy real collection where the same logical category is spelled a
dozen different ways across different backup sources get normalized to a
clean taxonomy via config, without renaming anything on disk. Empty by
default -- zero behavior change until rules are added. Verified against
both a matching-rule case and a no-match-falls-through case.

### 3. `app_manager.py` (new file) -- four feature groups, all reachable
from a new "Organize…" toolbar button opening `OrganizeDialog` (4 tabs)
in `gui.py`:

- **Duplicate detection** (`find_duplicate_groups`): exact-key +
  fuzzy-near-miss passes over all apps, surfaced for manual review/merge
  (calls the existing `resolver.merge_apps()` once confirmed -- nothing
  merges automatically). Verified against a synthetic exact-dup pair and
  the real DAEMON Tools typo fuzzy-dup case (correctly caught at 90.9%,
  above the 88 default threshold).
- **Category/subcategory rename+merge** (`rename_catalog`,
  `rename_subcatalog`): bulk `UPDATE` across affected apps; renaming to
  an already-existing name IS the merge (no FK table to reconcile in this
  schema). Verified.
- **Structural health report** (`generate_structural_report`): apps
  spread across multiple scan roots, subcategory names duplicated across
  different catalogs, and unused `folder_name_aliases` entries (this
  schema's plain-text-column equivalent of DeepSeek's FK-based "orphaned
  category" concept -- adapted rather than copied since there's no
  categories table here to be orphaned from). Read-only. Verified against
  seeded data producing all three finding types correctly.
- **Physical file reorganization** (`preview_reorganize` /
  `execute_reorganize`): moves each variant's folder into
  `dest/Catalog/Subcatalog/AppName/Version/`. **Deliberately rebuilt more
  conservatively than the DeepSeek version this was adopted from**, which
  used plain `shutil.move` with no collision check and no move log --
  judged too risky to port as-is for something that runs against a real
  personal backup drive. This version: preview is a pure dry run (moves
  nothing); execute takes EXACTLY the previewed plan rather than
  recomputing it; a destination that already exists is ALWAYS skipped,
  rechecked at execute time even if the preview said otherwise; a JSON
  move log is written incrementally (survives a mid-run crash) next to
  `catalog.db`; `variants.source_path` is updated on success.
  **Verified against a real filesystem, not just mocked**: created real
  folders/files in a temp dir, ran preview (confirmed nothing moved,
  destinations correctly computed), ran execute (confirmed file content
  preserved after the real move, DB `source_path` updated, move log
  written and readable), then ran the exact same preview+execute AGAIN on
  the already-moved data specifically to test the collision path
  (confirmed it skips rather than overwrites or crashing), and separately
  verified a missing/already-gone source folder is recorded as a failure
  without aborting the rest of the batch.

### 4. Column visibility toggle (gui.py)
`visible_columns` setting (list of column keys), a "Columns…" toolbar
button (checkable menu, one column per click), applied via
`QTableView.setColumnHidden()` in `MainWindow._apply_column_visibility()`,
called on every `refresh_all()` and at startup. "App Name" can't be
hidden (there'd be no way to identify a row). Verified: setting a
restricted visible-columns list and confirming the right columns report
as hidden via `isColumnHidden()`.

### 5. `AI_MODULE_REFERENCE.md` (new file)
A living architecture/API-index document, explicitly requested: file map,
per-module purpose and key-function index, DB schema summary, dependency
direction, and a documented testing-conventions section (including the
QMessageBox-mock-hangs-headless-tests gotcha discovered while testing
`OrganizeDialog` this round, so it doesn't cost another round of confused
debugging next time). Meant to be kept current going forward by
whichever AI (this one or DeepSeek) makes the next change -- update it in
the SAME change that adds/removes/renames something, not as an
afterthought.

### Known test-harness gotcha discovered this round (documented, not a
code bug): `unittest.mock.patch` against `QMessageBox.question` (a
PySide6 static method) did not reliably intercept the call in this
sandbox's headless (`QT_QPA_PLATFORM=offscreen`) environment -- multiple
attempts hung rather than returning the mocked value. Worked around by
testing the surrounding logic directly (calling the same underlying
`merge_apps`/`find_duplicate_groups` functions the confirm-branch would
have called) rather than forcing the modal dialog through headlessly.
Confirmed via a clean, isolated, freshly-started Python process that
`find_duplicate_groups` + `merge_apps` + `OrganizeDialog` construction
(including with a real `MainWindow` as parent) all work correctly in
isolation -- the hang was specific to the mock-patching approach, not the
underlying code. Documented in AI_MODULE_REFERENCE.md's testing-
conventions section so this doesn't cost debugging time again.

### Full regression re-confirmed after all of the above
WinGlobe/isdel resolver case, DAEMON Tools typo now resolving via the new
synonym rule (previously only partially addressed by fuzzy clustering),
cold-start `MainWindow` construction, all via a single clean end-to-end
script run after every module was in place.

## Checkpoint 16: Detail panel UI polish — catalog/subcatalog dropdowns, spacing, font size, homepage stretch

Prompted by visual feedback after using the updated Detail Panel (larger font, better spacing, and dropdowns for catalog/subcatalog fields). The previous implementation still used plain `QLineEdit`s for catalog/subcatalog; these were replaced with **editable `QComboBox`es** that list all currently used catalog/subcatalog names from the database, making it easy to select an existing value or type a new one.

### Changes
- **Catalog and Subcatalog now use `QComboBox`** with `setEditable(True)` so users can both select from the list or type a new value. The combos are populated via `_populate_catalog_combo()` / `_populate_subcatalog_combo()`, which query `DISTINCT catalog` / `subcatalog` from the `apps` table.
- **Spacing increased** in both the App `QFormLayout` and the Scraper metadata `QGridLayout` (from 4 to 8), making the panel less cramped.
- **App name font size increased** to 12pt (was default ~9pt) for better readability.
- **Homepage field now stretches** across the full width of the metadata group (spans 3 columns) so long URLs are fully visible.
- Fixed an `AttributeError` (`QComboBox` has no `setText`) by replacing `.setText()` calls with `.setCurrentText()` and blocking signals while setting to avoid unnecessary `currentTextChanged` triggers.

### Verified
- Headless smoke test: `MainWindow` construction, app selection, and `load_app()` now correctly populate the combos and set the current values without crashing.
- Manual visual inspection confirmed the combos show the list of existing categories, allow typing new values, and the changes commit correctly when a new value is selected or typed.
- Full regression re-run of the WinGlobe/isdel resolver case and the DAEMON Tools clustering case to confirm no unintended side effects from the UI changes.


---

## 11. Recent updates (Checkpoint 17)

### `app_manager.py` – duplicate detection enhancements

- **`_dup_normalize()`** – a standalone normalizer used *only* for duplicate detection. It is **not** influenced by resolver settings (no edition stripping, synonyms, etc.), ensuring stable detection independent of configuration changes. It strips trailing versions, parentheticals, and non‑alphanumeric characters.
- **`find_duplicate_groups()`** now returns `DuplicateGroup` objects with a `members` list containing full app details: `id`, `name`, `catalog`, `subcatalog`, `variant_count`, and `sample_paths` (first variant folder path). This richer data powers the new UI preview.
- Detection passes: exact fixed‑key (using `_dup_normalize`), exact base (version stripped), exact normalized, fuzzy base, token set, and partial ratio.

### `gui.py` – OrganizeDialog Duplicates tab

- Replaced the old `QTableWidget` with a **`QTreeWidget`**.
- Each duplicate group is a top‑level item with header showing reason, score, and app count.
- Child items per app display **Catalog**, **Subcatalog**, **Variants**, and a **Sample Path** (truncated to 80 chars; full path available as a tooltip).
- **Checkboxes** on each app (and group headers for future use) allow fine‑grained selection.
- Columns are resizable and movable by dragging headers.
- Two merge actions:
  - "Merge selected in group" – merges all checked apps in the currently selected group into the first checked app.
  - "Merge all groups (auto)" – merges all groups that have at least two checked apps after a single confirmation.
- Both use `resolver.merge_apps()` and refresh the list automatically.

> **Note:** Group‑level checkbox toggling of all children is not yet implemented but can be added later.

---

## For `PROGRESS.md` – append this new checkpoint at the end:

```markdown

## Checkpoint 17: Improved duplicate detection and merging UI

Following user feedback from the "Organize" dialog, we enhanced both the detection logic and the visual interface for managing duplicate apps.

### Detection improvements
- **Standalone normalizer** `_dup_normalize()` was added to `app_manager.py`. It is **not** influenced by resolver settings (e.g., edition stripping, synonyms). It strips trailing versions, parentheticals, and non‑alphanumeric characters, providing a stable key that always identifies the same core app name regardless of configuration changes.
- `find_duplicate_groups()` now returns richer data: each `DuplicateGroup` contains a `members` list with app details (`id`, `name`, `catalog`, `subcatalog`, `variant_count`, `sample_paths`). This enables the UI to show full context before merging.
- The detection still runs multiple passes (exact fixed-key, exact base, exact norm, fuzzy base, token set, partial ratio) to catch both exact duplicates and near‑misses.

### UI overhaul in OrganizeDialog
- Replaced the simple `QTableWidget` with a **`QTreeWidget`** in the Duplicates tab.
- Each duplicate group becomes a top‑level item with a header showing the reason, match score, and number of apps.
- Each app in the group is a child row with **Catalog**, **Subcatalog**, **Variants**, and a **Sample Path** (truncated to 80 chars, full path shown on hover).
- **Checkboxes** on both group and app items allow fine‑grained selection; merging only affects checked apps.
- Columns are resizable and movable (drag headers).
- Two merge actions:
  - "Merge selected in group" – merges all checked apps in the currently selected group into the first checked app.
  - "Merge all groups (auto)" – merges all groups that have at least two checked apps, after a single confirmation.
- Both actions call `resolver.merge_apps()` and refresh the duplicate list automatically.

### Verification
- Tested with a seeded database containing exact and fuzzy duplicate groups. The tree populated correctly, checkboxes worked, and merges completed without errors.
- Columns were resized and reordered interactively; the full path tooltip appeared on hover.
- Full regression of resolver and scanner logic (WinGlobe/isdel, DAEMON Tools, ACDSee, etc.) showed no side effects.
- The new detection correctly groups apps that previously required manual merging (e.g., typo‑spelled "Deamon Tools" now appears as a fuzzy match group with the correctly spelled apps).

### Known remaining work
- The group‑level checkbox currently does not toggle children automatically; this can be added as a future enhancement if desired.
- The duplicate detection could be further improved by adding a path‑similarity pass (variants in the same parent folder), but the current multi‑pass approach already covers most real‑world cases seen so far.


## Checkpoint 18: Monitor module — manual "watch folder" job

New `monitor.py` module. Not a background watcher: a two-phase job the
user triggers with the new "Monitor…" toolbar button. It walks one or
more configured watch folders (typically Downloads), proposes a match
for each new candidate installer/archive against the existing apps
table, lets the user review/edit the full plan in a dialog, then (only
after that explicit confirmation) compresses and moves each file into
the organized structure under its app's folder.

Shares the same `_active_worker` lock as Scan / Resolve / Scrape, so
only one job runs at a time.

### Design discussion before coding
Explicit Q&A round with the user established:
- Manual job (toolbar button), not a watcher.
- Two-phase: scan -> dry-run plan -> user reviews/edits -> execute.
- "Archive the installer" = compress it (7z default), not just move it.
  Already-compressed inputs (`.zip/.rar/.7z/.iso/.tar/.gz/.tgz/.cab`)
  move as-is; only real installers get compressed.
- Report at end of run; quick dialog + optional CSV export.
- Reuse the existing organized-apps destination root (the same one
  Organize > File Structure uses); ask which root if more than one.
- Extensions list, already-compressed list, partial-download list, and
  move-vs-copy mode are all new `monitor_*` settings, editable from a
  new Settings > Monitor tab.
- Monitor folders are a separate list from `scan_roots` (they're sources,
  not the organized destination).
- New apps start at `status='needs_review'`.

### Module structure
Deliberately self-contained per the checkpoint-15 flattening goal.
`gui_main.py` imports exactly ONE name from `monitor.py`:

    from monitor import MonitorJob

and then only ever touches its public API:

    job = MonitorJob(parent_window, db, db_path)
    job.progress.connect(slot)      # status text
    job.job_finished.connect(slot)  # MonitorResult | None
    job.job_failed.connect(slot)    # error message
    if job.start():                 # False if user cancelled the start dialog
        self._active_worker = job
    job.cancel()

Everything else -- the two-phase worker thread, the `MonitorPlanItem` /
`MonitorResultItem` / `MonitorResult` dataclasses, the three private
dialogs (`_MonitorStartDialog`, `_MonitorPlanDialog`,
`_MonitorReportDialog`), all compression backends -- is internal to
`monitor.py`. A dialog change here cannot break `gui_main.py`, and vice
versa.

### Two phases
**PLAN** (`scan_monitor_folders`): reads only. Walks each configured
folder, filters candidates by extension / size / partial-download
marker / size-settled-for-N-seconds, runs each surviving filename
through `resolver.extract_fields()` (the same cascade the scanner uses,
including the synonyms / bracket-strip / build-number-folding work from
checkpoints 7-15), and proposes a match against the `apps` table:
exact `normalize_key` hit -> "exact", fuzzy candidates above
`fuzzy_match_threshold` -> "fuzzy", otherwise -> "none".

**EXECUTE** (`execute_monitor_plan`): takes the user-reviewed plan
verbatim, never recomputes anything. Per item:
- `skip` -> recorded, nothing moves
- `attach` -> uses the pre-existing `matched_app_id`
- `create_new` -> inserts a new app at `status='needs_review'` with
  `name_locked=1`, then syncs catalog/subcatalog as tags
- Computes destination `dest_root/Catalog/Subcatalog/AppName/Version/`
- Already-compressed input -> move-or-copy as-is; otherwise compress to
  the chosen format, then (move mode) delete the source
- Records a `variants` row with `raw_candidate_id = NULL` (monitored
  files never entered `raw_candidates`) and `name_source = 'monitor'`,
  so monitor-originated variants are distinguishable in the variants
  table from resolver-produced ones
- Post-loop (batched once, not per item) fires one `scraper.run_scrape()`
  call for every app that was attached/created, if
  `monitor_auto_scrape_on_attach` is on

### Safety
- Destination collisions are ALWAYS skipped, never overwritten, rechecked
  at execute time.
- Each item is wrapped individually: one failure is recorded and skipped
  rather than aborting the batch.
- JSON move log written incrementally next to `catalog.db`
  (`monitor_log_<timestamp>.json`).
- Two `create_new` rows with the same normalized name in one run collapse
  to one app (the second file attaches to the first's just-created app).

### Compression backends (tiered, matching scanner.py's pattern)
- 7z -> py7zr, else external 7z/7za on PATH, else falls back to zip
- zip -> stdlib zipfile (always available)
- rar -> external rar binary only (rarely present; falls back through
  7z then zip, noting the fallback in the per-item report)

### Settings added to config.py
`monitor_folders`, `monitor_extensions`,
`monitor_already_compressed_extensions`, `monitor_skip_partial_extensions`,
`monitor_min_size_mb`, `monitor_settle_seconds`, `monitor_archive_format`,
`monitor_move_mode`, `monitor_auto_scrape_on_attach`. All exposed in a new
Settings > Monitor tab.

### GUI
- New "Monitor…" toolbar button (between Organize and Export CSV),
  tooltip describing the flow.
- Start dialog: folders in a QTableWidget with Add folder… / Delete
  selected buttons (rather than a textarea, per user feedback -- the
  first version was a plain textbox, and the user asked for a proper
  table with add/delete buttons). Destination root dropdown (seeded
  from `scan_roots`) with a Browse… button. Mode (move/copy) and archive
  format dropdowns.
- Plan dialog: 8-column editable table (File / Extracted / Status /
  Action / Target App / New Name / Catalog / Subcatalog). Every row is
  editable, including auto-matched ones. Warns (but doesn't block) if
  a "create new" row is missing a catalog -- the most common reason a
  monitor-created app ends up looking blank in the main table.
- Report dialog: Moved/Copied / Skipped / Failed summary, per-row table,
  and a CSV export button.

### Fixes and iterations during development
1. `extract_fields` call signature -- the first version passed wrong
   kwargs (a single `pe_metadata` blob instead of the real
   `pe_product_name`/`pe_product_version`/`pe_file_version`), and read
   `.name` instead of `.clean_name` on the result. Fixed both after
   cross-checking against the actual `resolver.py`.
2. `settings_version` -- the first `_record_variant` tried to read a
   `settings_version` setting via `db.get_setting("settings_version", 0)`,
   which doesn't exist as a settings key (it's a separate table). Removed
   the column from the variant insert entirely -- the resolver's own
   `_upsert_variant` doesn't write `resolved_with_settings_version` either,
   so the monitor now matches it.
3. Redundant `MonitorWorker` in `gui_backend.py` -- an early draft put
   the worker there; it was already duplicated inside `monitor.py` as a
   private `_MonitorWorker`. Removed the `gui_backend` copy to keep
   `monitor.py` self-contained.
4. Report-dialog ordering -- `MonitorJob._on_finished` originally showed
   the report dialog BEFORE emitting `job_finished`, which meant the
   status bar, table refresh, and `_active_worker` release all waited on
   the user closing the modal report. Swapped so the signal fires first;
   the report is now a follow-up summary, not a gate.
5. Slow "stuck on one file" symptom on first real run -- the post-attach
   `run_scrape()` call ran inline per item, blocking the execute loop
   for 5-30 s whenever the Winget manifest cache needed a refresh.
   **Fixed this checkpoint**: the loop now collects every touched
   `app_id` into a `touched_app_ids` set, and a single
   `run_scrape(db, app_ids=sorted(touched_app_ids))` call runs once
   after the loop. The loop runs at full speed; the scrape is one
   final, visible step. (An earlier draft of this checkpoint claimed the
   fix was already applied -- it was not; corrected here and in
   `monitor.py`.)
6. Duplicate apps from multi-file create-new -- a single run that
   contained two files both needing "create new" with the same name
   produced two separate apps. Added a `created_in_this_run` dict keyed
   by `normalize_key(new_name)` so the second file attaches to the
   first's just-created app.
7. Blank Catalog/Subcatalog on monitor-created apps -- the fallback
   names `"Uncategorized"` / `"Misc"` were used for the on-disk folder
   structure but not written back to the `apps` row, so the app looked
   empty in the main table while its folder had a real (if generic)
   name. Now `_create_new_app` writes the same fallbacks, and
   `_sync_monitor_app_tags` populates the Tags column from them.
8. Missing catalog warning in plan dialog -- the plan dialog's
   `_execute` now checks every "create new" row for a blank catalog and
   warns (but doesn't block) before handing the plan off. This is the
   most common reason a monitor-created app looks blank.

### Verified
- Full run against a real watch folder: 6 candidates, 1 exact match, 5
  skipped by the user in the plan dialog, 1 moved + compressed +
  scraped (Kodi 21.3, destination folder created correctly under
  `.../Uncategorized/Misc/kodi/21.3/`, scrape populated publisher /
  latest version / winget ID / tags in the detail panel).
- Duplicate-name collapse: two files needing "create new" with the same
  name correctly merged into one app in the same run (Re Shade case).
- Collision skip: re-running with the same file present skipped rather
  than overwrote.
- Catalog fallback: monitor-created apps now show `Uncategorized` /
  `Misc` instead of empty in the main table.
- `needs_review` highlight: monitor-created apps correctly show the
  yellow background (same as resolver-flagged `needs_review` apps),
  which is the intended visual cue that a monitor-created app deserves
  a glance before being marked verified.

### Known caveats
- `py7zr` may not be installed; the first 7z run on such a machine will
  fall back to zip and note the fallback in the report.
- `rar` output requires an external rar binary; falls back to 7z then
  zip if absent.
- `winget show` is still unverified against a live Windows CLI
  invocation (same caveat as checkpoint 14) -- the batch scrape uses the
  manifest-only path by default, which is fine here.
- The main table's "Added" / "Last Scanned" columns show blank for apps
  whose only variants came from the monitor, since those columns are
  derived from `raw_candidates.first_seen_at` / `.last_seen_at` via a
  LEFT JOIN and monitor variants have `raw_candidate_id = NULL`. Not a
  bug -- just a consequence of the "monitored files never entered
  raw_candidates" design choice. `variants.source_path` and the
  `monitor_log_<timestamp>.json` move log both have the real history.

## Checkpoint 19: Post-checkpoint-18 bug sweep

Full read-through of every module after checkpoint 18 shipped, in
response to "check code for errors and conflicts". Seven issues found,
all fixed:

### Critical
1. **`AppPickerDialog._populate()` crashed with `AttributeError`** --
   it called `self.db.cursor.execute(...)`, but `Database` has no
   `.cursor` property. This fired the moment a user clicked
   **Move to different app…** on any variant. Fixed to
   `self.db.connect().execute(...)`, matching every other call site in
   the project.
2. **`monitor.py` still had the per-item inline `run_scrape()` call**
   that checkpoint 18 fix #5 claimed was already batched. Re-applied the
   batched version: the execute loop now collects `touched_app_ids` and
   does one `run_scrape(db, app_ids=sorted(touched_app_ids))` after the
   loop. `PROGRESS.md`'s checkpoint 18 fix #5 entry corrected to note the
   discrepancy.

### Minor
3. Removed unused imports: `QApplication` and `QColor` from
   `gui_main.py`; `QWaitCondition` and `QMutex` from `gui_backend.py`
   (leftovers from when a `MonitorWorker` briefly lived in that file).
4. `AppPickerDialog`'s docstring was stale ("stub – implement if
   needed") -- it's a fully implemented dialog now. Replaced with an
   accurate description.
5. **`DetailPanel` catalog/subcatalog combos committed on every
   keystroke** -- `currentTextChanged` was connected directly, so typing
   "Graphics" wrote ~8 UPDATE statements and ~8 audit_log rows.
   Rewired to `editingFinished` (focus-out / Enter) + `activated`
   (explicit dropdown pick), so only confirmed edits persist. Programmatic
   `setCurrentText` calls were already signal-blocked, but this makes the
   intent explicit rather than relying on the caller.
6. `_commit_field` silently ignored empty values (couldn't clear a
   Catalog/Subcatalog from the detail panel). Changed to allow clearing
   catalog/subcatalog (a valid "unfiled" state) while still refusing to
   clear `name` (a nameless app is unusable in the table and every
   picker).

### Verified
- Full headless smoke test of `MainWindow` against a seeded DB after all
  fixes: construction, selection, detail-panel population,
  catalog/subcatalog combo commit-on-focus-out, `AppPickerDialog`
  population, and a full `MonitorJob` planning pass all complete without
  error.
- Regression re-run of the WinGlobe/isdel resolver case and the DAEMON
  Tools clustering case: unchanged.
- `monitor.py` batch-scrape path re-tested end-to-end against a seeded
  DB with 3 planned items, all 3 touched apps were scraped in a single
  `run_scrape()` call (confirmed via a lightweight logger shim).
## Checkpoint 20: Claude review pass — full codebase audit, two real bugs found & fixed

Requested review: "check the reports and code base ... check for errors,
issues, cross working... make sure everything is perfect." Full pass over
all 12 modules (pyflakes, cross-module import verification, settings-key
audit, and live scan+resolve smoke tests against real-shaped synthetic
trees), cross-referenced against every prior checkpoint above.

### Verified clean (no action needed)
- All 12 modules import without error, headless, no circular imports.
- Every cross-module `from X import name` resolves to a real top-level
  definition (scripted AST check) -- no stale references to
  renamed/removed functions.
- The specific bugs earlier checkpoints (esp. 18-19) claimed fixed
  (`AppPickerDialog` cursor crash, monitor's batched `run_scrape()`,
  `.get()` on `sqlite3.Row`) are genuinely fixed in this delivery.
- `AppsTableModel.COLUMNS` and the SQL in `.refresh()` are consistent --
  no orphaned display columns.

### Bug 1 (fixed) — `portable_indicator_words` missing from `DEFAULT_SETTINGS`
Used in `resolver.py` (portable-build detection, hardcoded fallback
`["portable", "paf"]`) and editable in Settings > Advanced, but never
added to `config.py`'s `DEFAULT_SETTINGS`. Effect: the Settings dialog
showed this field as *empty* (not the real default), and clicking Save
-- even without touching that field -- unconditionally persisted an
empty list, silently and permanently disabling portable-app detection
catalog-wide the first time anyone opened and saved Settings. Fixed by
adding `"portable_indicator_words": ["portable", "paf"]` to
`config.py`. (`visible_columns` has the same absence but every call site
already supplies the correct fallback inline, so it's cosmetic only --
left as-is with a note; see AI_MODULE_REFERENCE.md.)

### Bug 2 (fixed) — naming cascade collapsed to the CATALOG name for a very common real-world layout
Root cause, isolated with a direct `extract_fields()` reproduction: for
an install unit that is (a) exactly 2 folder levels deep (a plain
`Catalog/AppName/installer.exe` layout with no separate subcatalog
folder -- very common, not an edge case) AND (b) has a generic installer
filename already in `ignore_filename_words` (`setup.exe`, `install.exe`,
etc. -- also extremely common), BOTH the folder candidate (disqualified
for sitting at depth<=2, per the checkpoint-6 "Burners" fix) and the
file candidate (invalid, ignore-listed word) get thrown out, leaving
`parent_folder` (the CATALOG folder, one level further up) as the only
scored candidate. `parent_folder` is meant to be the weakest possible
fallback (`source_bonus=1`) but nothing stopped it from winning outright
in this case -- reproduced concretely:
`Collection/GRAPHICS/Adobe Photoshop CS6/setup.exe` resolved to an app
named **"Graphics"** (the catalog) instead of "Adobe Photoshop", with
the real name silently demoted to `alt_name_candidate` (visible via
"Use this instead" in the GUI, but not the default -- easy to miss
across a large catalog).

Fixed in `extract_fields()`: when the scored winner's source is
`parent_folder`, check whether the depth-disqualified `folder` candidate
at the install unit's own level has a genuinely valid name of its own
(not empty, not ignore-listed) -- if so, use that instead, since a real
(if structurally shallow) folder name is almost always better than the
bare catalog/subcatalog folder one level up. Deliberately scoped as a
no-op for the original "Burners" case (installer sitting directly IN
the catalog folder itself, no dedicated app subfolder at all) since
there the shallow folder's cleaned text is identical to the parent's
and no swap happens.

**Verified**:
- The exact failing case now resolves to "Adobe Photoshop" / version
  "CS6", `name_source="folder"`, alt candidate correctly shows "Setup".
- Regression: the original "Burners" case (`dvd_fab_v6.2.0.24_multilingual.zip`
  directly in the `BURNERS` folder) still resolves to "Dvd Fab", not
  "Burners" -- byte-identical to pre-fix behavior.
- Regression: a normal 3-level `Catalog/Subcatalog/AppName/installer`
  layout is byte-identical to pre-fix output (confirmed against an
  unpatched copy of `resolver.py` run side-by-side).
- New case added: a 2-level `BURNERS/Nero/setup.exe` layout (a real app
  folder one level under a catalog that's itself named after a
  category) now correctly resolves to "Nero" instead of "Burners".
- Full synthetic scan+resolve run across three trees simultaneously
  (the bug case, a clean 3-level case, and the original Burners case)
  in one DB: all three named correctly, no cross-contamination.

### Minor fixes, same pass
- `app_organizer.py`: `_existing_catalogs()` type-hinted `Optional[str]`
  without importing `Optional` -- harmless today only because
  `from __future__ import annotations` defers evaluation, but a latent
  footgun for any future runtime introspection (e.g. `typing.get_type_hints`).
  Added the import.

### Not changed this round (flagged, not fixed -- see report to user)
- Several unused imports (`csv` in `app_manager.py`, a handful of unused
  `PySide6.QtWidgets` symbols in `gui_main.py`/`monitor.py`, two dead
  `nonlocal` statements in `app_manager.py`) -- cosmetic, zero runtime
  effect, left for a dedicated cleanup pass rather than risking
  incidental edits across large files in the same change as the two
  real fixes above.
- `AI_MODULE_REFERENCE.md`'s dependency-direction line said
  `monitor <- gui_backend`; the actual (and correct, non-circular)
  direction is `monitor.py` importing `AppPickerDialog` FROM
  `gui_backend.py`. Doc arrow was backwards -- corrected in that file.
- All previously-documented open items (collection-folder grouping,
  catalog/subcatalog/tag combined filter dropdown, the DAEMON-Tools-style
  archive-in-component-subfolder edge case, untested live `winget show`
  CLI and Chocolatey scraping) remain open -- not attempted this round,
  out of scope for a review/audit pass.

## Checkpoint 21: HTML reports + PyInstaller-safe paths (logs/, manifest/)

User request: (1) the reorganize/monitor end-of-run summary showed only
counts ("Failed: 3") with no way to see WHAT failed or WHY without
opening the raw JSON log -- wanted an HTML report instead, auto-opened
when created; (2) the app will be packaged with PyInstaller, so it must
not rely on the current working directory for the database, logs, or
caches, or it'll misplace them depending on how the .exe is launched;
(3) logs of any kind into a `logs/` subfolder, external metadata caches
into a `manifest/` subfolder, database left bare next to the app.

### New module: `app_paths.py`
Single source of truth for where things live. `get_app_base_dir()`
resolves to `sys.executable`'s folder when frozen (PyInstaller), or this
file's own folder otherwise -- NEVER the current working directory,
which is what silently broke this before (a double-clicked .exe, a
shortcut with an unexpected "Start in" folder, "Run as administrator",
etc. can all launch with a CWD that isn't the .exe's own folder).
`resolve_db_path()` anchors a relative db path (including the bare
default `"catalog.db"`) to that folder instead of CWD.
`get_logs_dir()`/`get_manifest_dir()` return (creating if needed)
`<catalog folder>/logs` and `<catalog folder>/manifest` -- anchored to
wherever the ACTUAL catalog.db in use lives (not necessarily the .exe's
folder, so a poweruser running two catalogs from two different .db
files on a data drive gets two independent logs/manifest pairs, not one
shared pair that collides).

Wired in:
- `run_gui.py`: rewritten. Default db path now
  `get_default_db_path()` (script/exe folder), any path given on argv is
  passed through `resolve_db_path()`. Logging now also goes to a
  rotating file `logs/app.log` (5 x 2MB) via `logging.handlers
  .RotatingFileHandler` -- console logging is now conditional on a
  usable `sys.stdout` existing, since attaching a `StreamHandler` to
  `None` (which is what a `--windowed`/`--noconsole` PyInstaller build
  gives you) would make every single log call raise inside logging's
  own error handling. Previously there was NO general application log
  file at all, only the per-run reorganize/monitor JSON logs.
- `scraper.py`: `cache_path_for_db()` / `cache_path_for_winutil()` now
  return `manifest/winget_manifest_cache.json` and
  `manifest/winutil_apps_cache.json` (were bare next to catalog.db).
- `app_manager.py` (`execute_reorganize`) and `monitor.py`
  (`execute_monitor_plan`): the JSON move-log now defaults into
  `logs/` (was bare next to catalog.db).

Regression-tested: constructed a `.db` at a nested path
(`/tmp/x/MyCatalog/catalog.db`), ran a full scan+resolve+reorganize from
a DIFFERENT current working directory, confirmed `logs/` and the
manifest cache both land next to the catalog, not CWD; also confirmed
the frozen-exe branch resolves against a faked `sys.executable`.

### HTML reports (`html_report.py`, new module)
Single self-contained (no CDN, no external font/JS, fully offline)
dark-themed HTML renderer shared by both `app_manager
.generate_reorganize_html_report()` and `monitor
.generate_monitor_html_report()`. Layout: summary cards (counts) up top,
a "Needs attention" table FIRST listing every failed/skipped item with
its actual reason in plain text (this is the part that was missing
before -- the JSON log always had the reason, the message box never
showed it), then a searchable/filterable "Everything" table below
covering every item including successes (togglable). Written next to
the JSON log it's generated from, in `logs/`, with a matching
timestamp; the JSON log's path is also linked at the bottom for anyone
who does want the raw machine-readable version.

`ReorganizeResult` gained `entries` (the full per-item outcome list,
previously only ever written to the JSON file and discarded, not
returned to the caller) and `html_report_path`. `PlannedMove` gained
display-only `app_name`/`version` fields (populated in
`preview_reorganize()`) purely so the report can show a real name
instead of a bare variant ID -- not used anywhere in path-building
logic, that's untouched. `MonitorResult` gained `html_report_path`.

**Found and fixed while wiring this up**: `MonitorResultItem.app_name`
was declared on the dataclass but never actually SET at any of
`execute_monitor_plan()`'s four `MonitorResultItem(...)` construction
sites -- every row, success or failure, would have rendered with a
blank/"(new app)" name in both the brand new HTML report AND the
pre-existing in-app `_MonitorReportDialog` table (that dialog's "App"
column has had this exact same blank-name bug all along; the JSON log
also never carried an app_name field). Fixed by resolving the name
once per item up front (from `new_name`/`matched_app_name`/
`extracted_name`/the raw filename, whichever's available first) and
passing it into all four construction sites and into the JSON log entry.

GUI wiring:
- `app_organizer.py`'s `_on_reorg_finished`: now opens
  `result.html_report_path` in the system browser automatically
  (`webbrowser.open(Path(...).as_uri())`, wrapped in try/except -- never
  blocks completion if no browser is available) instead of only showing
  a bare-counts message box; the message box is kept (shorter) as a
  fallback/confirmation, and mentions the report was opened.
- `monitor.py`'s `_MonitorReportDialog`: auto-opens the same way when
  the dialog appears (the in-app table already showed per-row notes, so
  this is mainly for a shareable/printable copy), plus an explicit "Open
  HTML Report" button next to the existing "Export to CSV…" for
  re-opening it later without re-running anything.

Verified end-to-end for both features: a real failure (deleted a source
folder / deleted a monitor-inbox file out from under the run) shows up
in the "Needs attention" section with its actual error text, a real
success shows up in "Everything" with the correct app name, and the
generated file opens as valid, readable HTML (checked structurally, not
just "did it write bytes").

### Not done this round (call out before active use, per user's ask)
- `_MonitorReportDialog`'s in-app table's "App" column had the same
  blank-name bug the HTML report would have inherited -- fixed as part
  of the same change (see above), but flagging explicitly since it
  predates this checkpoint and was never reported/noticed before.
- The reorganize dialog does not (yet) have its own explicit
  "Open HTML Report" re-open button the way the monitor dialog now
  does -- it only auto-opens once, immediately, plus shows the path in
  the completion message box. Low-risk gap, not fixed, since the
  auto-open covers the actual complaint; a manual re-open button is a
  five-minute follow-up if wanted.
- No cap on how many timestamped `*.json`/`*.html` files accumulate in
  `logs/` over time -- on a very actively reorganized/monitored catalog
  over months this could grow into hundreds of small files. Not
  addressed; worth a "keep last N runs" setting if this becomes a
  real-world annoyance.
- PyInstaller `.spec` file / actual build+package step was not
  attempted (out of scope for a source-level review -- there's no
  Windows machine available in this environment to build or run the
  resulting .exe on). The path-handling code is written and tested
  against the documented PyInstaller `frozen`/`sys.executable`
  contract, but hasn't been verified against a REAL frozen build.

## Checkpoint 22: Clean Library, readable status colors, catalog/subcatalog tree

User request: (1) a "Clean library" action next to "Re-scan now" in the
Scan Roots dialog -- checks whether apps ALREADY in the catalog still
exist on disk (never looks for new ones) since apps get deleted/moved/
replaced by hand outside this app; (2) the needs-review yellow row color
was too bright and made text unreadable; (3) apps should be color-coded
by state (new/unscraped, needs review, etc.) with dull colors so text
stays readable, and a fully "done" app (scraped or manually verified)
should look normal/uncolored; (4) the left-panel catalog list should
also show subcatalogs.

### Clean Library (new feature)
`app_manager.py` gained a new section: `MissingItem`, `CleanLibraryResult`,
`scan_for_missing_sources()` (read-only, `os.path.exists()` per variant,
scoped to one scan root when given so an unplugged external drive
doesn't wrongly flag every OTHER root's apps as missing too), and
`execute_clean_library()`. Deletion removes BOTH the variant row and its
`raw_candidates` row -- the raw_candidates row has to go too, or a
future Resolve-without-a-fresh-Scan could silently re-create the exact
variant just removed, since Resolve reads raw_candidates from the DB,
not the live filesystem. Any app left with zero variants afterward is
deleted outright (FK cascades handle its tags etc.). Same
dry-run-then-confirm shape as Reorganize/Monitor, and reuses
`html_report.py` for the same kind of report those two already produce.

GUI: `gui_backend.CleanLibraryScanWorker` (QThread -- `os.path.exists()`
on a network share or sleeping drive can block noticeably per call, and
a catalog can have thousands of variants) runs the scan phase;
`gui_main.CleanLibraryReviewDialog` shows the results with a checkbox
per row (all checked by default, Select all/none) and a
"Remove N selected from catalog" button that calls
`execute_clean_library()` and auto-opens the HTML report exactly like
Reorganize does. Wired into `ScanRootsDialog` as a new "Clean library"
button next to each root's existing "Re-scan now" button.

Verified end-to-end: deleted a real folder out from under a resolved
app, confirmed the scan found exactly that one missing item and left an
unrelated scan root's apps alone, confirmed removal deleted both the
variant and the now-empty app while leaving an unrelated app untouched,
confirmed the review dialog's table and remove button work correctly.

### Row status colors -- readability fix + full state coverage
The old code only ever colored `needs_review` rows, with a single
hardcoded pale-yellow BACKGROUND and no explicit foreground/text color.
That's the actual bug behind "yellow is too bright, text isn't
visible": on a system running in dark mode, default cell text renders
light/white (inherited from the system palette), and white text on a
pale yellow background is nearly unreadable -- the background color
itself wasn't really the problem, the missing paired foreground was.

`gui_backend.AppsTableModel` now has `_row_status_key()` covering every
meaningful state, each with an EXPLICIT (background, foreground) pair
so it's readable on any system theme, not just a retuned shade:
- `needs_review` -- dull amber (was the too-bright pale yellow)
- `scrape_status == "failed"` -- dull rose
- `not_yet_enriched` (resolved, but `scrape_status` is
  `not_scraped`/`pending`) -- dull blue, doubles as the "new app" marker
  the user asked for, since every freshly-resolved app starts here
- `status == "ignored"` -- dull gray, plus italic app name
- `status == "verified"` OR `scrape_status == "scraped"` -- **no
  override at all** (returns `None`), so a fully "done" app just uses
  the normal system row color like the user asked
Matching tooltips added for each state (shown on the Status/Scraped
columns). Verified all 7 status/scrape_status combinations resolve to
the intended color key (or `None` for done).

### Left panel: catalog + subcatalog tree
`catalog_list` (a flat `QListWidget`) is now `catalog_tree`, a real
`QTreeWidget`: catalogs as top-level nodes, subcatalogs as expandable
children, "(all catalogs)" pinned at the top. Selecting a subcatalog
filters to just that subcatalog; selecting its parent catalog shows
everything under it (all subcatalogs combined) -- same as before for
catalog-level selection, new for subcatalog-level. Selection is tracked
by an explicit `{catalog, subcatalog}` dict in each node's `UserRole`
data rather than by node text, so a subcatalog name can never collide
with an unrelated catalog's name. `AppsTableModel` gained a
`subcatalog_filter` (mirrors `catalog_filter`) and a
`catalog_subcatalog_tree()` helper that returns `{catalog: [subcatalog,
...]}` for building the tree. Tree rebuilds (on every `refresh_all()`)
preserve the current selection by identity, not text, and re-expand
every catalog node by default.

Verified end-to-end: built a 3-catalog collection, confirmed the tree
shows the right catalog/subcatalog nesting, confirmed selecting a
subcatalog node filters to exactly that subcatalog, and selecting the
parent catalog node shows all of its subcatalogs combined.

### Not done this round
- No "select all under this catalog" bulk action in the new tree beyond
  the filtering itself -- out of scope for this request.
- The dull status colors are fixed values, not settings-driven/
  user-customizable -- if a user wants their own palette, that's a
  follow-up (would need new DEFAULT_SETTINGS entries + a small color
  picker in SettingsDialog).
- Clean Library's scan step batches progress updates every 25 items;
  fine for realistic catalog sizes, untuned/untested at extreme scale
  (tens of thousands of variants on a slow network share).

## Checkpoint 23: real crash report from Windows -- subprocess text-decoding bug

User ran the actual packaged-from-source app on Windows and hit a real
crash during scraping:

```
UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d in position 3973
```

thrown from `subprocess.py`'s internal `_readerthread`, right after
"Fetched winget manifest" / "Loaded winutil apps from cache" in the log
-- i.e. during the `winget show` enrichment step.

### Root cause
`scraper.py`'s `fetch_winget_show()` called
`subprocess.run(..., capture_output=True, text=True, timeout=timeout)`
with NO explicit `encoding=`. With `text=True` and no encoding, Python
decodes the child process's stdout/stderr using the PLATFORM's default
locale encoding -- on a typical Windows install that's `cp1252`
("charmap"), NOT UTF-8. `winget show`'s own output is UTF-8 and
regularly contains characters outside cp1252's range (curly quotes,
em-dashes, trademark symbols, any non-English publisher/app name) --
the very first one of those crashes the decode in a background reader
thread, which kills the entire scrape run outright (not just that one
lookup), stopping the app cold mid-batch.

`app_manager.py`'s RAR-archiving path (`_archive_single_file`, the
`archive_format == "rar"` branch) had the exact same pattern
(`subprocess.run([...], capture_output=True, text=True)`, no encoding)
-- same latent bug, not yet reported by the user but caught while
auditing every `text=True` subprocess call in the codebase for the same
class of issue once the first one was found. (Checked every other
`subprocess.run`/`Popen` call, every `open()`/`write_text()`/`read_text()`,
and every `csv.writer`/`json.dump` file handle across all 16 files for the
same gap -- everything else already specified `encoding="utf-8"`
explicitly. Only these two were missing it.)

### Fix
Both calls now pass `encoding="utf-8", errors="replace"` explicitly --
matches what winget/rar actually emit, and any genuinely malformed byte
degrades to a single replacement character (`�`) instead of crashing
the whole run.

### Verified
Reproduced the EXACT failure class (not a guess) by writing a fake
`winget`/`rar` executable on `PATH` that emits a raw invalid byte
(`\x9d`, the literal byte from the user's traceback) in otherwise
normal-looking output:
- Before the fix's pattern (`text=True`, no encoding, run in an
  environment where that byte is invalid): crashes exactly as reported.
- After the fix: `fetch_winget_show()` returns a normal result with the
  publisher field showing `Caf�Software` instead of crashing; RAR
  archiving completes normally instead of crashing.

### Why this matters beyond the one crash
This is the same underlying class of bug as checkpoint 21's PyInstaller
path-handling work -- code that behaves fine in whatever environment it
was originally written/tested in (Linux, or a Windows box with a
UTF-8-friendly locale) but breaks on a plain, unmodified Windows install
using its default codepage. Worth remembering for any FUTURE
`subprocess.run(..., text=True)` call added to this codebase: always
pass `encoding="utf-8", errors="replace"` explicitly, never rely on the
platform default.

## Checkpoint 24: PyInstaller .spec + requirements.txt

New files: `app_manager.spec`, `requirements.txt`. No source-code
changes this round.

### `app_manager.spec`
Onedir build by default (a commented-out onefile alternative is included
at the bottom) -- entry point `run_gui.py`, output name `AppCatalog`.
Explicitly `collect_all()`s `rapidfuzz`, `bs4`, and `certifi` (PyInstaller's
default import-scanning only sees `.py` source, not compiled C
extensions or dynamically-registered plugins, which all three have in
some form). `certifi` matters more than it looks: `requests` verifies
HTTPS certificates against certifi's bundled `cacert.pem` data file --
missing it doesn't fail at build time, it fails at runtime the first
time the app tries to reach the network, with a much more confusing SSL
error. Also bundles `py7zr`, `pefile`, `pycdlib`, and `rarfile` --
scanner.py/app_manager.py/monitor.py's four genuinely-optional
dependencies (all already wrapped in `try/except ImportError` in the
source) -- IF they're installed in the build environment; if not, the
app still works, those specific features just fall back to their
documented non-bundled path (an external `7z`/`unrar` binary on PATH, or
plain zip) exactly like they do when run from source without them
installed. PySide6 deliberately does NOT use `collect_all` (would drag
in QtWebEngine/QtMultimedia/QML and bloat the build by hundreds of MB
for modules this app never imports) -- relies on PyInstaller's own
built-in PySide6 hook instead, with a comment explaining the fallback
(`collect_all('PySide6')`) if that assumption ever proves wrong for a
future PySide6/PyInstaller version pairing.

**Verified for real, not just written**: installed PyInstaller plus
every dependency (including all four optional ones) in this sandbox and
actually ran `pyinstaller app_manager.spec` twice (once without the
optional deps, once with all of them installed) -- both builds
completed with zero errors. The only "missing module" warnings in
either build were for `pycdlib`/`rarfile` in the run WITHOUT them
installed (expected -- exactly mirrors their `try/except ImportError`
fallback in `scanner.py`) plus unrelated `setuptools` internals noise
that has nothing to do with this app. Then actually launched the
resulting compiled executable (a Linux ELF in this sandbox, since
there's no Windows machine available here, but built by the exact same
`app_manager.spec` a Windows PyInstaller run would use) and confirmed:
it started without crashing, and -- the real point of this test --
`catalog.db` and `logs/app.log` were created exactly next to the
executable itself, not in some CWD-dependent location, which is the
first real end-to-end confirmation of checkpoint 21's entire
`app_paths.py` design working correctly against an actual compiled
binary rather than just `python run_gui.py`.

### `requirements.txt`
Four required packages (`PySide6`, `requests`, `rapidfuzz`,
`beautifulsoup4`) plus the same four optional ones the spec handles,
each commented with what it enables and what happens without it.
Parses and resolves cleanly via `pip install --dry-run -r
requirements.txt` (checked in this sandbox).

### Not verified (no Windows machine available in this environment)
- The actual Windows `.exe` this spec produces has NOT been run on a
  real Windows machine. Everything above was validated as thoroughly as
  a Linux sandbox allows: spec syntax, PyInstaller's full Analysis/
  dependency-collection phase, and an actual compiled-and-launched
  binary proving the path-handling design works end to end -- but the
  Windows-specific bootloader, DLL bundling, and Qt platform plugin
  loading (`platforms/qwindows.dll` etc., PyInstaller's PySide6 hook is
  expected to handle this automatically, per its own hook logic, but
  this specific claim is unverified on real Windows) are still a "should
  work, not yet confirmed" until the first real Windows build.
- No `.ico` icon was provided, so the build uses PyInstaller's default
  icon. `icon=None` in the spec has a comment showing where to point it
  at a real file once one exists.
- UPX compression is deliberately left off (`upx=False`) -- a common
  source of "this program can't start" failures and false antivirus
  flags specifically with PySide6's Qt DLLs. Worth reconsidering only
  after a plain (non-UPX) build is confirmed working on the target
  Windows machine.

## Checkpoint 25: Scan root repath (Feature A) + per-root folder layout config (Feature B)

Two independent features requested together, discussed and designed
before any code was written (see the conversation's own back-and-forth
for the full design negotiation -- summarized here is the final shape
and what was actually verified).

### Feature A: `update_scan_root_path()`

For when an external/backup drive holding a scan root changes mount
point (`D:\` -> `E:\`, or a UNC path gets remounted) -- rewrites the
scan root's `path` plus every already-catalogued path derived from it
(`raw_candidates.folder_path`, `variants.source_path` via a join since
variants don't store `scan_root_id` directly, `scan_errors.path`) in one
transaction. Prefix-match only, via `_starts_with_root()`/
`_rewrite_prefix()` -- never a naive substring replace, so a sibling
path that merely shares characters with the old root (`D:\PROGRAMS2\`
vs root `D:\PROGRAMS`) is never touched. Does NOT re-scan or re-resolve;
the folder structure under the root is assumed unchanged. Logged to
`audit_log` (`entity_type="scan_root"`, `action="path_change"`).
GUI: "Change path…" button per row in `ScanRootsDialog`.

**Verified**: ran a real scan+resolve against a synthetic tree, called
`update_scan_root_path()` to move it to a different path, and confirmed
every `raw_candidates`/`variants` row's path was rewritten correctly
with a matching `audit_log` entry -- not just read the code, actually
executed it against a live DB.

### Feature B: per-scan-root folder layout (`resolve_scan_root_layout()`)

The scanner's old `_derive_catalog_subcatalog()` only ever supported a
FIXED two-tier structure: `catalog = parts[0]`, `subcatalog = parts[1]`,
always, regardless of how deep the real folder nesting went. This broke
down for the user's actual drive layout in three ways: (1) some catalogs
have NO subcategory tier at all (`ADOBE\Photoshop\` -- Photoshop
shouldn't be mistaken for a category), (2) some catalogs MIX direct apps
and subcategory folders side by side (`GRAPHICS\FastStone\` next to
`GRAPHICS\Converters\`), and (3) some subcategories nest an arbitrary
number of levels deep (`GRAPHICS\Converters\Video\AcmeConvert\` --
`Video` should be the subcatalog for apps below it, not `Converters`).

New DB shape (`scan_roots` table, additive migrations): `root_is_catalog`
(this root's own folder IS a single catalog, no top-level catalog
folder), `root_catalog_name`, `folder_layouts_json` -- a flat map keyed
by lowercased relative path, values either a bare int (`-1` skip / `0`
no-subcatalog / `2` has-subcatalog) or `{"mode": ..., "name": "..."}`
when the folder's also been renamed for display.

`scanner.resolve_scan_root_layout(root, folder_path, layout,
root_is_catalog, root_catalog_name, settings) -> (catalog, subcatalog,
depth, skip)` replaces the old function as the real code path (the old
`_derive_catalog_subcatalog` is kept only as what an empty/unconfigured
layout degrades to). Core algorithm, arrived at after working through
several wrong drafts (see below):

- **Skip**: any ancestor (or the folder itself) marked `-1` in the
  layout excludes the whole subtree -- checked shallow-to-deep, no
  override possible below an ancestor's skip (matches "do not scan/
  import this folder or anything under it" literally).
- **Deepest-match-wins ancestor lookup** (`_layout_nearest_ancestor`):
  walks from the folder's own key up to the shallowest, returns the
  first configured `(depth, mode)` found; unconfigured anywhere
  defaults to `(depth=1, mode=2)` -- "has subcatalog" starting just past
  the catalog boundary.
- **`mode == 0` (no subcatalog)**: two genuinely different index
  arithmetics depending on WHERE the match happened, both needed to get
  the nested case right:
  - **Ancestor-match** (`matched_depth < len(parts)`, e.g. `"graphics/
    converters"` configured `0` while deriving for a descendant of
    Converters): `subcatalog = parts[matched_depth - 1]` -- the
    ancestor's own name.
  - **Self-match** (`matched_depth == len(parts)`, e.g. `"graphics/
    faststone"` configured `0` while deriving for FastStone itself):
    `subcatalog = parts[matched_depth - 2]` -- one level shallower,
    since here the CURRENT folder is the app, not an ancestor of it.
    Getting this wrong (using the ancestor formula for a self-match)
    was the first real bug caught while testing: it produced
    `subcatalog="FastStone"` (i.e. the app naming itself) instead of
    `None`.
  - Either way, `None` if the resulting boundary index is at or before
    the catalog folder itself (matches the original `ADOBE` example).
- **`mode == 2` (has subcatalog, incl. default)**: `subcatalog =
  parts[matched_depth]` -- the very next folder past wherever the chain
  was last explicitly anchored (or past the catalog boundary, if never
  anchored at all).
- **Depth**: `+1` when `mode == 0`, unchanged otherwise -- same
  correction the original design used, just now applied relative to
  the ACTUAL matched depth rather than assumed to always be 1, so it
  keeps working correctly no matter how many real tiers exist above it.

This means the user's exact nested case needs only ONE override
(`"graphics/converters/video": 0`) -- `GRAPHICS\Converters\appname1`
(no override in its chain) still correctly resolves to
`subcatalog="Converters"` via the plain default-mode-2 path, while
`GRAPHICS\Converters\Video\AcmeConvert` resolves to
`subcatalog="Video"` via the self/ancestor-match logic above. Renaming
(`{"mode":.., "name":..}`) is a pure label substitution at whichever
position the algorithm lands on -- no separate propagation logic, every
descendant just inherits the label automatically since it's written
straight into the string the scanner hands to `raw_candidates`.

`gui_main.FolderLayoutDialog`: lazy-expanding `QTreeWidget` (levels 1-2
populated immediately since "some folder might be mixed" is the common
case; level 3+ resolves on-demand the moment a level-2 row is expanded,
via `list_child_folders()`), per-row mode combo + rename field + live
preview column. Two real UI-layer bugs caught and fixed while testing
against a real widget tree (not just the underlying function):
- **Preview self-match bug**: the preview always appended a synthetic
  child segment to compute "what would an app here look like", which
  turned a genuine self-match (FastStone) into a false ancestor-match,
  showing a non-`None` subcatalog. Fixed by branching on the ROW's own
  combo selection: `mode == 0` previews the row's own path directly
  (self-match), everything else appends a synthetic child.
- **Placeholder leak into the computed value**: for a `mode == 2` row
  exactly one level above where its subcatalog would land (every
  top-level row with no deeper override), the synthetic placeholder
  segment's literal text ("App") leaked into the COMPUTED subcatalog,
  not just the displayed name, because it landed at the exact index
  `resolve_scan_root_layout` reads. Fixed by appending two synthetic
  segments (a real known child name when available, else a generic
  "Category", then "App") so the computed subcatalog always lands on a
  real-ish name.
- **Case-sensitivity bug in `_prune_orphaned_layout_keys`**: joining
  the LOWERCASED layout keys directly into a path and calling
  `os.path.isdir()` silently reports every folder "missing" on a
  case-sensitive filesystem (this sandbox; would also be fragile
  cross-platform in general), which would have wiped the ENTIRE saved
  layout on every save. Fixed with `_folder_exists_case_insensitive()`,
  walking one segment at a time via `os.scandir()` with case-insensitive
  name matching.

Auto-trigger: `MainWindow._maybe_show_folder_layout_dialog()` runs
before every scan -- opens the full dialog (all top-level folders shown,
new ones pre-filled at default and marked "(NEW)") on the very first
scan of a root, or whenever a re-scan finds top-level folders not yet
present as an explicit key in `folder_layouts_json`. Top-level rows are
always written explicitly on OK (even at the default value) specifically
so an unchanged, already-seen folder never re-triggers the dialog on a
later re-scan.

**Verified**: `resolve_scan_root_layout()` tested against 9 cases
(ADOBE no-subcatalog, default 2-tier, mixed direct-app with explicit
override, skip, the nested case with only the deepest folder overridden,
rename via dict entry, `root_is_catalog` default, `root_is_catalog` with
an override, and a version-subfolder-stays-put case) -- all correct,
including the exact nested scenario from the design discussion. Then a
FULL end-to-end run: built a synthetic tree matching the real reported
layout, ran `run_scan()` + `run_resolve()` against it for real, and
confirmed the final `apps` rows (name, catalog, subcatalog) come out
right for all four structural cases at once in a single scan.

## Checkpoint 26: ScanRootsDialog consolidation + global batch operations

`ScanRootsDialog` is now the single entry point for scan root management
-- the main toolbar's standalone "Add scan root…" `QAction` was removed
(`_pick_and_scan_root()` deleted along with it); the dialog's own "Add
new scan root…" button was already equivalent and is simpler to reason
about with one entry point instead of two paths to the same job.

Four new/changed buttons in `ScanRootsDialog`:
- **"Delete selected root…"** -> `app_manager.delete_scan_root(db,
  scan_root_id)`. Deliberately non-destructive to the catalog: deletes
  the `scan_roots` row (cascading `raw_candidates`/`scan_errors` via the
  existing `ON DELETE CASCADE` FKs), but `variants.raw_candidate_id` is
  `ON DELETE SET NULL` (already the schema's design, not new), so
  already-resolved apps/variants are left in place, just unlinked from
  their (now gone) raw scan data. Confirmed via a real delete against a
  live DB: app row survived, its variant's `raw_candidate_id` went to
  `NULL` rather than the variant disappearing.
- **"Re-scan all roots"** -> `MainWindow._run_rescan_all_roots()` /
  `_advance_batch_rescan()`. Re-scans every known root sequentially
  (never in parallel -- the DB connection/worker pattern isn't set up
  for concurrent jobs), reusing the exact same single-root path per
  root (including `FolderLayoutDialog` if that root has new folders); a
  root that fails or gets skipped (dialog cancelled) doesn't stop the
  rest of the batch, and a summary lists anything that had a problem.
- **"Clean library (all roots)"** -> `MainWindow._run_clean_library(None)`.
  `app_manager.scan_for_missing_sources()` already supported
  `root_path=None` meaning "check the whole catalog" -- it just wasn't
  wired to any button; this was a pure GUI wiring task, not a backend
  change.

**Bug caught and fixed while building the batch re-scan, before it ever
shipped**: the first draft connected to each worker's `finished_ok`
signal to advance to the next root AFTER calling
`_run_scan_and_resolve()` (i.e., after `.start()` had already been
called on that worker). For a fast/trivial scan, the worker could finish
and emit `finished_ok` before that `.connect()` line even ran -- Qt
signals only notify connections that existed AT emit time, so the
connection was silently missed and the batch stalled after the first
root. Confirmed with a traced test: `_advance_batch_rescan` was only
ever called once despite two roots being queued, yet the SECOND root's
app still appeared in the final `apps` table -- misleadingly, because
`ScanAndResolveWorker` calls `run_resolve(db)` with no `scan_root_id`
scope, so the FIRST root's resolve pass re-derived the second root's
already-scanned (but not yet re-scanned) `raw_candidates` too, masking
the stall. Rebuilt to piggyback on the handlers ALREADY connected before
`.start()` (`_on_scan_resolve_finished`, `_on_job_failed`) via a
`_batch_rescan_current_path` instance flag rather than a fresh
post-start connection -- re-verified with the DB wiped between steps
(including `raw_candidates`, not just `apps`) so a stalled batch
couldn't hide behind resolve's global scope a second time; both roots
were confirmed freshly re-scanned in sequence. Also verified the
mid-batch-failure and mid-batch-cancel-and-skip paths independently.

## Checkpoint 27: Fixed intermittent QThread crash + clean library scan-root-awareness

Two issues reported after checkpoint 26 shipped, from real use.

### `QThread: Destroyed while thread '' is still running` (intermittent crash)

**Reported**: crash running Clean library right after Re-resolve all,
"happens some times" -- i.e. timing-dependent, which is the signature of
this exact class of bug.

**Root cause**: every job's completion/failure handler in `MainWindow`
did `self._active_worker = None` directly inside the slot fired by that
worker's own custom `finished_ok`/`failed` signal. That signal is
emitted from INSIDE the worker thread's `run()`, which does not
guarantee the underlying OS thread has fully wound down and reached a
state Qt considers `finished()` by the time the (queued, cross-thread)
slot actually executes on the main thread. Dropping the last Python
reference to a `QThread` object before Qt considers it finished is
exactly what produces this message, and can escalate to a real crash
rather than just a warning depending on timing -- which matches "happens
some times" precisely. (`AI_MODULE_REFERENCE.md`'s `app_organizer.py`
section already documented awareness of this general class of risk for
a different worker -- `OrganizeDialog.closeEvent` blocking close during
an active reorganize -- but the fix hadn't been generalized to every
`_active_worker` site in `MainWindow`.)

**Fix**: one helper, used everywhere `_active_worker` gets cleared on
completion (7 call sites -- scan+resolve, resolve-only, scrape,
monitor, clean-library, and the generic job-failed handler):
```python
def _release_worker(self):
    worker = self._active_worker
    if worker is not None:
        worker.wait()
    self._active_worker = None
```
`wait()` blocks until the thread has genuinely finished; in the
overwhelming common case (it already has, by the time the slot runs)
this returns immediately and costs nothing. Closes the race entirely
rather than narrowing the window.

**Verified**: ran scan+resolve -> resolve-all -> clean-library(None)
back to back against a real `MainWindow` instance repeatedly; also
re-ran the full checkpoint 26 batch-rescan test suite (sequential
processing, mid-batch failure, mid-batch skip) against the changed
code to confirm `_release_worker()` didn't introduce a stall of its own
(it doesn't -- `wait()` on an already-finished thread returns
instantly, confirmed by timing the full 3-step sequence at 0.04s wall
time in the same headless harness).

### Clean library now understands *why* a variant might be gone

**Reported**: after using "Delete selected root…" (checkpoint 26) to
forget a scan root, running Clean library said "Everything already in
the catalog in the whole catalog still exists on disk. Nothing to clean
up." -- not a bug, but a real mismatch with what the user wanted: Delete
scan root is deliberately non-destructive (see checkpoint 26), so the
files it used to track are usually still sitting on disk wherever they
were, and Clean library's ONLY job was ever "does this exact file still
exist" -- it had no concept of scan roots at all.

Asked whether "delete scan root" should become destructive instead
(cascade-delete its apps); declined in favor of keeping deletion
non-destructive and extending Clean library's own rules instead:

`scan_for_missing_sources()` now checks, per variant, in order:
1. **`raw_candidate_id IS NULL`** (its scan root was deleted via
   "Delete selected root", per checkpoint 26's `ON DELETE SET NULL`
   design) -> flagged missing, `reason="scan root no longer tracked"`.
   This is what makes deleting a scan root and then running Clean
   library actually remove its apps, as requested.
2. **Its scan root IS still registered, but `os.path.isdir(root.path)`
   is `False`** (external/network drive unplugged, letter remounted
   elsewhere) -> flagged missing, `reason="scan root path unreachable"`,
   WITHOUT checking the individual file -- one `os.path.isdir()` per
   scan root (cached in a dict, not per-variant) rather than every one
   of that root's files individually failing the same underlying check.
3. Otherwise, the original per-file `os.path.exists()` check,
   `reason="file not found"`.

`MissingItem` gained a `reason: str = "file not found"` field (default
preserves the old behavior for any caller not yet aware of it).
`CleanLibraryReviewDialog` shows a new "Reason" column so the user can
see which of the three rules caught each item before confirming
removal. `execute_clean_library()` needed no changes -- it already
handles a `None` `raw_candidate_id` gracefully (just skips the
`raw_candidates` delete for that item), which covers case 1 for free.

**Verified**: built a 3-root synthetic scenario exercising all three
paths at once (one root deleted via `delete_scan_root()`, one root's
folder removed from disk with `shutil.rmtree()` while its `scan_roots`
row stayed, one root left completely untouched) and confirmed
`scan_for_missing_sources(db, root_path=None)` returned exactly the two
expected items with the correct `reason` on each, the untouched root's
app was correctly left alone, and `execute_clean_library()` removed
both flagged apps cleanly.

## Checkpoint 28: Folder layout redesign -- explicit self-describing roles + "-1 level" single-catalog promotion

Reported after using checkpoint 25's mode system in practice: the
`Has subcatalogs / No subcatalogs / Skip / ⤷ Inherit` labels weren't
informative, and the "root is itself a single catalog" checkbox didn't
match how the user actually wanted to add a single-catalog folder (they
wanted it to fold into a SHARED scan root with other single-catalog
folders next to it, not become its own separate root). Discussed fully
before coding (per the user's own request) -- two real design forks
resolved by explicit Q&A before any code changed, both confirmed correct
against the final implementation below.

### Terminology + cascading defaults (supersedes checkpoint 25's mode system)

Every folder is now one of four SELF-describing roles -- **Catalog /
Subcatalog / App / Skip** -- picked from the identical dropdown at any
depth (no more `Has subcatalogs` describing a folder's CHILDREN, no more
a separate "⤷ Inherit" pseudo-state). An unconfigured folder's default
cascades from its parent's role: Catalog's children default to
Subcatalog, Subcatalog's children default to App, and an App's children
stay App (they're just internal/version folders by that point, never
re-categorized). A brand-new top-level folder with no parent in the tree
defaults to a NEW per-root setting, `unconfigured_toplevel_role`
(`scan_roots` column, `'catalog'` normally, `'skip'` for a root created
via the promotion below) -- exposed as a small dropdown in
`FolderLayoutDialog`'s top strip, replacing the old "This root is itself
a single catalog" checkbox + catalog-name field entirely.

`scanner.resolve_scan_root_layout()` was rewritten around this (the old
`_layout_mode_of`/`_layout_find_skip`/`_layout_nearest_ancestor` int-mode
helpers are gone, replaced by `_layout_role_of()` and
`_resolve_role_chain()`): walks the chain TOP-DOWN computing one role per
folder (forward, cascading), rather than the old backward deepest-match
search. `catalog` = the label of the DEEPEST folder in the chain with
role `"catalog"` (normally exactly one, at the top; marking a folder
deeper in the chain `"catalog"` too is allowed and intentionally
"restarts" categorization from there, discarding anything shallower --
an escape hatch for an unusual nested structure, confirmed working via
`_add_new_root`'s test below, not expected to be common). `subcatalog` =
the deepest `"subcatalog"`-role folder below that catalog, or `None`.
Depth still gets boosted to `max(raw_depth, 3)` whenever the folder
ITSELF has role `"app"` (same load-bearing idea as before -- keep the
resolver's "depth<=2 = don't trust this name" rule from misfiring on a
direct catalog child -- just computed off the folder's own resolved role
now instead of a matched-ancestor's mode).

Because every role is now self-describing, most of checkpoint 25's
self-match-vs-ancestor-match complexity (and its associated bugs) simply
disappears: a folder marked `"app"` unambiguously means "I am the app,"
never "my children are," so `FolderLayoutDialog`'s preview no longer
needs any synthetic child-segment trick at all -- it just resolves the
row's own path directly and formats by role (`"Catalog: {name}"` /
`"{catalog} / {subcatalog} (subcategory)"` / `"{catalog} / {subcatalog}
/ {this folder's name}"` for App / `"(not imported — skipped)"`).

**One real trade-off surfaced during testing, flagged but not asked
about further given how explicitly the cascade rule was described
twice**: since a Catalog's children now default to Subcatalog (not
App), a catalog with NO real subcategory tier at all (a flat catalog
where every child folder directly holds an installer, e.g. the original
design doc's `ADOBE\Photoshop\` example) needs each such child
EXPLICITLY marked `"app"` to flatten it -- marking the catalog folder
itself `"app"` doesn't work (there'd be no folder left with role
`"catalog"` in the chain, so nothing could be named at all; confirmed
via `catalog_idx = max(...)` returning `None` when tested this way).
Verified this isn't silently broken, just requires the explicit
override: the promotion test below (`Desktop/SomeApp`, no subcategory
tier, `SomeApp` left at its cascaded-default `"subcatalog"` role rather
than explicitly flattened to `"app"`) still resolved to a real app row
(`catalog="Desktop"`, `subcatalog="SomeApp"`) because
`resolver.extract_fields()` fell back to naming it from the installer
file itself rather than the folder -- not silently lost, just not the
ideal `subcatalog=None` shape it would have gotten with an explicit
`"app"` override on `SomeApp`.

### "-1 level" single-catalog promotion (supersedes checkpoint 25's `root_is_catalog` checkbox)

Picking a folder as "itself a single catalog" no longer stores IT as the
scan root. `ScanRootsDialog._add_new_root()` now asks (`QMessageBox.
question`) whether the picked folder is itself a single catalog; if yes,
prompts for a display name (`QInputDialog.getText`, defaulting to the
folder's basename), then walks up one level (`os.path.dirname`) and
`db.ensure_scan_root()`s THAT as the actual scan root, writing an
explicit `{"role": "catalog", "name": ...}` (or bare `"catalog"` when
the name wasn't changed) entry for the originally-picked folder, and
setting that root's `unconfigured_toplevel_role` to `"skip"`.

The requested dedup ("adding `C:\Program\Graphic` and `C:\Program\
Desktop` both as single catalogs should collapse into ONE scan root at
`C:\Program\`, editing either affects the same shared config") needed
NO new dedup logic at all -- it falls straight out of `scan_roots.path`
already being unique and `ensure_scan_root()` already being
insert-if-missing: the second "add as single catalog" call just reuses
the same row and merges its own catalog entry into the same
`folder_layouts_json`.

**Verified** end-to-end against a real `MainWindow`/`ScanRootsDialog`,
not just the underlying functions: added `/tmp/Program/Graphic` and
`/tmp/Program/Desktop` as single catalogs in two separate calls
(monkeypatching `QFileDialog.getExistingDirectory`, `QMessageBox.
question`, and `QInputDialog.getText` to simulate the two prompts) and
confirmed (a) exactly ONE `scan_roots` row exists, at `/tmp/Program`,
with `unconfigured_toplevel_role="skip"` and a `folder_layouts_json`
containing both `"graphic": "catalog"` and `"desktop": "catalog"`, (b)
a THIRD sibling folder (`/tmp/Program/Junk/randomstuff`, never
explicitly added) produced no app at all -- confirming the skip-by-
default behavior -- and (c) both `Graphic\Converters\AcmeConvert` and
`Desktop\SomeApp` still resolved to real, correctly-catalogued apps.

Also re-verified the full four-scenario walkthrough from checkpoint 25
(flat catalog, default 2-tier, mixed direct-app, nested subcategory with
rename) against the new role vocabulary end-to-end (`run_scan()` +
`run_resolve()` against a synthetic tree, not just `resolve_scan_root_
layout()` in isolation) -- all four still produce the correct final
`apps` rows.

## Checkpoint 29: Layout-change propagation + two follow-up fixes

Three related changes, motivated by the "editing a scan root's folder
layout after an initial scan leaves stale apps behind" bug the user hit
on a real ADOBE/PHOTOSHOP/CS6 subtree. Full reproduction from the user's
own log: `PHOTOSHOP\CS6 v13.0` initially mapped as role `App`, scan 1
produced 148 raw_candidates and 9 apps (most of them payload subfolders
that were never intended as standalone apps). User then edited the
layout, changing CS6 v13.0 to `Single App/Variant`, and re-scanned.
Scan 2 correctly emitted only ONE install unit for the CS6 subtree (the
role change worked as designed at scan time), but the 9 old apps
remained in the catalog -- total went from 9 to 10 apps, not 9 to 1.
Root cause: the scanner upserts `raw_candidates` and the resolver only
creates/updates apps; nothing in the pipeline removes a raw_candidate
that's no longer walked under the current layout. Result: the layout
editor was only usable on a fresh database.

### A. New `app_manager.apply_layout_change()` -- the propagation itself

New function in `app_manager.py` plus a new `LayoutChangeResult`
dataclass, placed under a new "0b. Layout-change propagation" heading
between the scan-root-lifecycle group and the duplicate-detection
group. Called by `FolderLayoutDialog._on_ok()` right after
`db.save_folder_layout()` writes the new JSON; `save_folder_layout()`
itself stays dumb (just writes the JSON), propagation is a separate
concern the caller orchestrates. Two classes of change, classified
per-key from the diff of old vs. new layout:

- **Label-only** (role unchanged, only the optional `name` differs):
  `UPDATE raw_candidates.catalog/subcatalog` in place across the
  affected subtree, computed via `scanner.resolve_scan_root_layout()`
  (the SAME function the scanner uses, so labels can't drift between
  the two). No deletion, no re-scan needed.
- **Role change** (effective role differs, or an entry was added/
  removed): delete `variants` + `raw_candidates` under the affected
  subtree, then delete any app left with zero variants. Because the
  fingerprints go with the deleted `raw_candidates`, the next
  incremental scan can't skip these folders even if their mtime is
  unchanged -- a natural re-scan of just the affected subtree is
  automatic, no new "force re-scan" flag needed.

All steps run in one transaction, no filesystem operations, logged to
`audit_log` as `entity_type="scan_root", action="layout_change"` with
the affected folder list and removal counts. Never auto-triggers a
re-scan; per the plan, re-scanning is the user's choice (the existing
"Re-scan now" button, or the natural next scan).

Reuses existing helpers throughout: `_starts_with_root()` for the path
prefix check (same case-insensitive separator-boundary-aware ancestor
check `update_scan_root_path()` uses), `_delete_zero_variant_apps()`
(extracted from `execute_clean_library()` so both call sites use
identical logic), `resolve_scan_root_layout()` for label computation
(imported lazily so `app_manager` stays importable without pulling in
`scanner`'s optional PE/archive deps).

GUI wiring: `FolderLayoutDialog.__init__` gains a
`self.layout_change_result = None` slot, populated by `_on_ok()` after
`save_folder_layout()`; `ScanRootsDialog._edit_layout()` reads it back
and sets the main window's status bar to
"Layout saved. Removed N app(s) / M variant(s) (K folder(s) affected).
Re-scan to rebuild." or "Layout saved. No catalog changes needed." on a
no-op. The previous modal "Layout saved" info box is removed -- save is
already a deliberate action, the status-bar message is the one-second
signal to inspect before re-scanning (per the plan's section 3.5).

### B. Fix 1 -- `_effective_role()` for classification

First live test log from the user after the initial implementation
showed:

    Layout change propagated for scan_root_id=24: 7 role change(s),
    0 label-only change(s) -> removed 0 raw_candidate(s) / 0 variant(s) /
    0 app(s); 0 label update(s)

"7 role changes" on a root with no prior scan and no prior saved
layout. Root cause: `FolderLayoutDialog._on_ok()` deliberately writes
every visible top-level row explicitly on every save (so a future
re-scan doesn't treat an unchanged folder as "new"), and the initial
`apply_layout_change()` compared raw explicit-entry values -- `None`
(no entry in the old empty layout) vs. `"catalog"` (the explicit value
in the new layout). Every top-level folder looked like an added entry,
i.e. a role change, even though "implicit default" and "explicit value
equal to the default" mean the same thing to the scanner.

Harmless in that specific case (nothing to delete yet), but a real
landmine the moment this root ever gets scanned and the user re-opens
the dialog and clicks OK without changing anything -- 7 subtrees would
get deleted for no reason.

Fixed by adding `_effective_role(layout, key, unconfigured_toplevel_
role)` -- a small recursive helper that mirrors `scanner._resolve_role_
chain()`'s forward cascade exactly (catalog -> subcatalog -> app -> app;
a top-level key with no entry gets the root's own
`unconfigured_toplevel_role`). Classification now compares effective
roles rather than raw entries:

    old_eff = _effective_role(old_layout, key, old_top_default)
    new_eff = _effective_role(new_layout, key, new_top_default)
    if old_eff != new_eff:
        role_change_keys.append(key)

Verified: after the fix, the same open-OK-on-a-fresh-root sequence logs
"0 role change(s), 0 label-only change(s)". The "save without changes
is a no-op" case (open on an already-scanned root, click OK without
touching anything) also correctly logs zero role changes now, which was
the primary load-bearing case the initial version silently got wrong.

### C. Fix 2 -- `unconfigured_toplevel_role` default is `'catalog'`,
### explicitly

Second live test log, from the SAME root 24 session:

    SKIPPED (folder layout: skip mode) ...\DESKTOP UTILITIES
    SKIPPED (folder layout: skip mode) ...\GRAPHICS
    SKIPPED (folder layout: skip mode) ...\SYSTEM UTILITIES
    SKIPPED (folder layout: skip mode) ...\tryout
    Scan walk finished. 4 folders visited.
    SCAN FINISHED: status=completed folders_seen=0 install_units_found=0
    errors=0

Four folders visited, zero folders seen -- every one skipped by layout,
on a brand-new root that had never been configured. Root cause traced
by querying the DB directly: `scan_roots.id=24` had
`unconfigured_toplevel_role = 'skip'`. `FolderLayoutDialog.__init__`
read that value and set the top-strip combo to Skip, and then
`_populate_top_level()` defaulted every top-level row's role combo to
Skip too (via `_default_role_for((name,))` returning
`default_toplevel_combo.currentData()`). The user clicked OK on the
apparently-default state, `_on_ok()` force-wrote every row explicitly,
and the scanner then correctly skipped all four subtrees.

Why was a fresh root's `unconfigured_toplevel_role` set to `'skip'`?
`database.py`'s current migration list already declares `DEFAULT
'catalog'`, but SQLite's `ALTER TABLE ... ADD COLUMN` cannot change a
column default once the column exists. An older build had shipped
`DEFAULT 'skip'` in that migration; every fresh DB created by that
build has `'skip'` baked into the column's schema, and every subsequent
`INSERT INTO scan_roots (path) VALUES (?)` -- including
`db.ensure_scan_root()`, which is the exact path taken when the "Add
new scan root…" flow answers "No" to the single-catalog prompt --
silently inherits `'skip'`. The current code's migration attempts to
`ALTER TABLE ... DEFAULT 'catalog'`, hits "duplicate column", catches
it, and moves on, leaving the bad default in place.

Two fixes applied:

1. **Explicit values in every `scan_roots` INSERT**, rather than
   relying on the table default at all. `Database.ensure_scan_root()`
   in `database.py` now writes
   `INSERT INTO scan_roots (path, unconfigured_toplevel_role,
   folder_layouts_json) VALUES (?, 'catalog', '{}')` explicitly, with
   a comment explaining why (the ALTER TABLE default can't be trusted
   on an older DB). Same treatment for `scanner._upsert_scan_root()`.
   This closes the gap permanently regardless of what default the
   table happens to carry.

2. **One-time data migration for existing rows**:
   `Database._fix_bad_skip_roots()`, called from `init_schema()` right
   after `_run_migrations()`. Fingerprint: a root whose
   `unconfigured_toplevel_role` is `'skip'` AND whose layout contains
   either no entries at all or only `'skip'` entries with no name
   overrides -- i.e. a root nobody has deliberately configured. Any
   such root is reset to `'catalog'` with its auto-written `'skip'`
   entries dropped, so the next dialog open shows the correct default.
   A root where the user made a real choice (any non-skip role, any
   rename) is left alone. If a user genuinely wants everything
   skipped they can re-set it from the dialog in one click; the
   migration cannot distinguish "user wanted this skipped" from "user
   accepted the buggy default" beyond that heuristic, which is why it
   only fires on the exact poisoning signature.

### Verified end-to-end

- Same ADOBE/PHOTOSHOP/CS6 scenario from the original report: role
  change `App` -> `Single App/Variant`, `apply_layout_change()` deletes
  148 raw_candidates / 9 variants / 9 apps under the affected subtree,
  re-scan produces 1 app. ADOBE case closed.
- "Open dialog, click OK without changes" on an already-scanned root:
  logs `0 role change(s), 0 label-only change(s)`, status bar shows
  "Layout saved. No catalog changes needed."
- "Open dialog, click OK without changes" on a FRESH root: same
  zero-change log; next scan no longer skips every top-level folder.
- Label-only rename of a catalog display name: no deletions,
  `raw_candidates.catalog` updated in place across the affected
  subtree, apps present. (Label updates do NOT touch `apps.catalog`/
  `apps.subcatalog` -- those are left for the next re-resolve, per the
  plan's "or leave that to the next resolve" allowance.)
- Skip transition (`App` -> `Skip`): whole subtree's raw_candidates
  deleted, all apps gone.
- Partial preservation: an app whose variants span both in-scope and
  out-of-scope folders survives with its out-of-scope variants intact;
  only the in-scope variants are removed.
- Cold start (fresh empty DB, first-ever layout save): no-op cleanly,
  no errors, zero counts, "no catalog changes needed" message.
- Root 24 in the user's live DB: after the migration runs on next
  startup (or the manual `UPDATE`), the dialog opens with
  `unconfigured_toplevel_role` = Catalog, all four top-level rows
  showing Catalog role, saving writes them as explicit catalog
  entries, and the next scan walks into all four subtrees.

### Out of scope (per the plan, restated here so it's not
### rediscovered as a surprise later)

- Auto-triggering a re-scan after save. Manual re-scan only; the
  status-bar message states the next action. One line to change later
  if it turns out to be wanted.
- Confirmation dialog on save. Rejected -- save is a deliberate action,
  the post-save status-bar message conveys the consequence.
- Preservation of locked / verified / scraped apps during role
  changes. Rejected -- an app with no remaining valid variants does
  not exist, regardless of its flags.
- Auto-merge of new single-app into old payload apps. Rejected --
  removal + re-scan produces the correct result without heuristics.
- Naming-cascade fix for the `Single App/Variant` role. The user's own
  log shows the new single app is currently named from the grandparent
  catalog (`Adobe`) rather than from the anchor folder (`CS6 v13.0`).
  This is a real bug in `resolver.extract_fields()` for that role, but
  it is orthogonal to propagation -- even perfect propagation leaves
  that naming wrong. Flagged separately, not addressed by this
  checkpoint.
  
  
## Checkpoint 30: Variant installer-file override (per-variant lock) + open-location path fix

Feature request: allow a user to manually pick which file in a variant's
source folder is the "real" installer -- overriding whatever the scanner
auto-picked -- and have that choice survive re-scans and re-resolves.
Kept deliberately minimal after an earlier over-engineered design (a
separate `variant_file_overrides` table keyed on scan root + folder,
with its own picker dialog) was scrapped in favor of a single boolean
lock column on `variants` plus a plain file dialog.

### New column

`variants.file_locked INTEGER DEFAULT 0` (additive migration in
`database.py::_run_migrations()`). Follows the exact convention already
used by `version_locked` / `name_locked` / `catalog_locked` /
`subcatalog_locked`: `0` = don't touch, `1` = user owns this field.

### Scanner -- honor the lock on re-scan

`scanner.run_scan()` now builds a `locked_files` dict scoped to the scan
root before walking:

    SELECT rc.folder_path, v.file_name
    FROM variants v
    JOIN raw_candidates rc ON rc.id = v.raw_candidate_id
    WHERE rc.scan_root_id = ? AND v.file_locked = 1 AND v.file_name IS NOT NULL

Keyed by `folder_path`, valued by the user's file name for that folder.
Passed into `walk_scan_root(..., locked_files=...)`.

`scanner.walk_scan_root()` now:
- Accepts `locked_files: Optional[dict] = None`.
- Looks up `locked = (locked_files or {}).get(display_dirpath)` **before**
  the `is_single_app` branch (not after -- the previous draft's placement
  inside the post-classification block was unreachable for single_app
  folders, since that branch `continue`s earlier).
- Passes `forced_file=locked` into `_build_single_app_candidate()`.
- For install_unit folders, does a case-insensitive basename match of
  `locked` against `file_names`; on hit, collapses the whole folder's
  candidates to just `[[locked_match]]`; on miss, logs
  `"Locked file %r not found in %s -- falling back to auto-pick"` and
  falls through to `_group_installer_files()` as before.

`scanner._build_single_app_candidate()` gained a `forced_file` kwarg:
when set, it tries to match `forced_file` against the collected relpaths
(exact-then-basename), and if matched, restricts `group_files` to that
one entry before `_enrich_install_candidate()` runs. Miss -> log warning
+ auto-pick.

Both paths keep the same DB row on disk (`variants.file_name`) as the
canonical source of the override -- no separate storage, no new table,
no scan-root-level config, no folder_layouts interaction.

### Resolver -- never clobber a locked file_name

`resolver._upsert_variant()` was writing `variants.file_name` on every
update unconditionally. That silently reverted the user's pick the first
time they ran Re-resolve without an intervening scan (raw_candidate still
held the old auto-pick). Now guarded:

    if not existing["file_locked"]:
        updates["file_name"] = raw_row["primary_file_name"]

Same pattern as the existing `version_locked` guard directly above it.

### GUI

**Context menu** (`DetailPanel._show_variant_context_menu`) gained two
actions between the existing "Run / open file" and "Re-evaluate selected"
entries:
- **Change installer file…** -- opens a plain `QFileDialog.getOpenFileName`
  rooted at the variant's current file location (or its source folder if
  the stored file no longer exists). Writes `variants.file_name = <relpath>`
  and `variants.file_locked = 1`, reloads the app.
- **Clear installer file override** -- writes `file_locked = 0`. Only
  enabled when the selected variant is currently locked. Auto-pick takes
  over on the next scan; `file_name` stays as-is until then.

Both menu items are disabled when `raw_candidate_id IS NULL` (monitor-
created variants have no scan-root folder to key the override against --
the scanner's `locked_files` query joins on `raw_candidates` and would
never see them).

**Variants table** (`load_app`) prepends a 🔒 to the File Name column
when `file_locked` is set, with a tooltip naming the locked file and
pointing at the Clear action.

**Stored value is `relpath(chosen, source_path)`** -- not
`os.path.basename()`, and not `.replace(os.sep, "/")`. This was the
single most-bug-prone part of the whole feature; two distinct real bugs
came out of getting it wrong, both fixed:

1. **Basename-only store broke single_app subtrees.** For a `single_app`
   folder whose installer lives one level down
   (`source_path="...\cs13"`, actual file `"...\cs13\Set\setup.exe"`),
   storing just `"setup.exe"` made `_variant_full_path` join to
   `"...\cs13\setup.exe"` -- one folder too shallow -- and Open Location
   popped "file no longer exists" for a file that was sitting right
   there. Fixed by storing the relpath; `Set/setup.exe` joins correctly.
2. **Forward-slash conversion broke Explorer `/select,`.** An
   intermediate fix stored `"Set/setup.exe"` with `.replace(os.sep, "/")`
   for "portability" -- but this is a Windows-only app (uses
   `explorer`, `os.startfile`, `\\?\` prefixing, none of which are
   cross-platform anyway), and Explorer's `/select,` argument silently
   ignores forward slashes and mixed separators, falling back to opening
   its default location (Desktop). The scanner's own relpaths
   (`_collect_single_app_files` -> `os.path.relpath`) already store
   native separators; the GUI was the odd one out for no real reason.
   Fixed by dropping the replacement and storing `os.path.relpath()`
   verbatim -- same convention as the scanner, same convention as the
   DB, same convention as the OS.

A refusal guard was added: if the chosen file's relpath starts with `..`
(wrong folder) or raises `ValueError` (different Windows drive letter),
warn and abort rather than silently store something the scanner will
never find.

**`_variant_full_path`** and **`_open_file_location`** now `normpath()`
their input as belt-and-braces (a no-op on already-native input, cheap
insurance for any historical DB rows still holding a forward-slash
value from the two days the intermediate fix existed). A single
diagnostic `print(f"[open-location] {full_path}")` was added at the top
of `_open_file_location` -- the earlier version had four extra prints
(lengthy `repr()`, `exists`/`isfile`/`isdir` booleans, resolved folder,
the full subprocess command line) that were overkill; one line showing
the resolved path is enough to see whether the normpath landed.

### Bugs found during this feature's own development

1. **`sqlite3.Row` has no `.get()`.** `load_app()`'s first pass at the
   🔒 marker wrote `v.get("file_locked")`; `variants` rows come back as
   raw `sqlite3.Row` objects (the codebase's usual convention is either
   `row["col"]` or `dict(row)` first). Fired on every app click,
   `AttributeError`, red traceback. Fixed to `v["file_locked"]` with a
   `"file_locked" in v.keys()` guard for pre-migration DBs.
2. **Dead code from an earlier draft.** `walk_scan_root` briefly had a
   second `if is_single_app:` block AFTER the fingerprint computation,
   holding the `locked` lookup and a duplicate `_build_single_app_candidate`
   call. Unreachable (the first `is_single_app` branch already
   `continue`d), but visually confusing. Removed; the `locked` lookup now
   lives once, before both branches.
3. **Nested redundant `if` in the install_unit branch.** An early edit
   left `if classification.unit_type == "install_unit":` wrapping another
   identical check inside itself -- always true, always redundant.
   Collapsed.
4. **The `.replace(os.sep, "/")` "fix" was itself the second bug** (see
   above), not the first.

### Verified end-to-end

- Right-click a variant → Change installer file → pick a real file in a
  nested subfolder of a single_app folder. `variants.file_name` becomes
  the relpath (native separators), `file_locked = 1`. Variants table
  shows `🔒 Set\setup.exe`.
- Right-click → Open file location → Explorer opens with the file
  selected. `[open-location] C:\...\cs13\Set\setup.exe` printed once,
  matching the DB value joined with `source_path`.
- Run **Re-resolve all** with no intervening scan. `file_name` unchanged;
  `file_locked` still 1.
- Run **Re-scan now**. `raw_candidates.primary_file_name` for that folder
  becomes the locked file's relpath; the candidate is emitted with that
  primary file; resolve writes `variants.file_name` = same value
  (guarded write is a no-op because it already matches). App unchanged.
- Right-click → Clear installer file override → `file_locked = 0`. Next
  scan reverts `raw_candidates.primary_file_name` to the auto-pick and
  `variants.file_name` follows.
- Delete the locked file from disk, re-scan. Scanner logs
  `Locked file 'Set\setup.exe' not found in ... -- falling back to
  auto-pick`; `file_locked` stays 1 so the override re-applies if the
  file comes back.
- Try to pick a file from outside the app folder. Warned and refused,
  no DB write.
- Monitor-created variants (`raw_candidate_id IS NULL`): both menu items
  disabled with explanatory tooltips.

### Files touched

- `database.py` -- one migration line.
- `scanner.py` -- `run_scan` locked_files query + `walk_scan_root`
  signature + both branches; `_build_single_app_candidate` gained
  `forced_file`.
- `resolver.py` -- one `if not existing["file_locked"]:` guard around
  the `file_name` update in `_upsert_variant`.
- `gui_main.py` -- context menu, two new DetailPanel methods, 🔒 marker,
  `_variant_full_path` normpath, `_open_file_location` normpath + one-line
  print.

Nothing in `app_manager.py`, `monitor.py`, `scraper.py`, `app_organizer.py`,
`gui_backend.py`, or `config.py` needed to change. No new settings, no
new dialogs, no new tables.

---

## Checkpoint 31: Variant manifests (`*.appcatalog.json`) + resizable Reorganize preview columns

*Date: 2026-10-02.*

### Problem

Every manual correction lived only in `catalog.db`: locked names, confirmed
installer files, verified status, scraped description / Winget / Choco ids.
Lose or reset the DB, update the app, or reorganize the library, and a rescan
had nothing but messy folder names to go on. Worse, dependent folders of a
multi-folder installer (`Redist/`, `crack/`, `data/`) were re-detected as
bogus extra "apps" (reproduced: the old code turned `Redist/vc.exe` and
`crack/keygen.exe` into apps called "Redist" and "Crack").

### Decisions locked in

- **One manifest file per VARIANT (install unit)**, stored next to its
  files, independent of catalog / subcatalog. Category data is deliberately
  never written, so a move between category folders can never leave a stale
  category behind. Tags that merely mirror a catalog/subcatalog name are
  dropped too.
- **Personal file names** (so one manifest can never be mistaken for, or
  overwritten by, another):
  - variant owns its folder -> `<folder>/<App name> <version> [edition] [arch] [language].appcatalog.json`
  - folder shared by several variants -> `<folder>/<entry file name>.appcatalog.json`
  - the writer NEVER overwrites a manifest that describes a different unit
    (different `variant_uid` AND different entry file): it appends ` [<uid6>]`.
  - renamed app -> manifest renamed, the superseded file removed.
  - the first-draft plain name `appcatalog.json` is still READ and is migrated
    (renamed + removed) on the next write.
- **The manifest describes the whole unit**: `unit.entry` (the setup file,
  with a path-independent fingerprint and a `confirmed` flag), `unit.members`
  (every dependent file/folder with a role: `entry` / `required` /
  `companion`), `unit.owned` (whole folder vs one file in a shared folder).
  Paths are relative to the manifest's folder (drive letters never stored).
  `app` carries name + scraped details; `variant` carries version / edition /
  architecture / language / locks / original (pre-cleanup) name.
- **Auto mode is the default** (`manifest_auto_enabled`, on by default,
  toggle in Settings -> Scanning & Noise). Implemented with SQLite triggers +
  a dirty flag + a flusher rather than hooks in each edit function, so every
  code path that edits an app/variant is covered (resolver edits, scraper,
  merge/split, monitor, ...). Category operations are not in the trigger
  column lists, so they never cause a write.
- Auto mode writes only variants that carry human or scrape work
  (`manifest_auto_only_valuable`, on by default); the manual buttons always
  write.
- **Old databases keep working.** Everything is additive: 7 new columns, 4
  triggers, uid backfill done with the dirty-trigger out of the way. Opening
  an old `catalog.db` marks nothing dirty and writes no file.

### Precedence when a manifest meets a database (`app_manifest.apply_manifest`)

| Situation | Result |
|---|---|
| variant created in THIS resolve (fresh DB / new file) | manifest applies fully (name, version, edition, arch, language, locks, details) |
| existing variant, manifest unchanged since we last wrote/applied it | nothing |
| existing variant, manifest changed on disk, DB has no unsaved edits | manifest applies (it is newer) |
| existing variant, manifest changed AND DB has unsaved edits | DB wins; `manifest_conflict` row in `audit_log`; DB version rewritten at next flush |
| existing variant, no baseline (DB predates manifests) | DB wins; only EMPTY app details (description, publisher, ids ...) are filled |

DB locks (name / version / file) always win over manifest values.

### Scan side ("claim first")

- `scanner.walk_scan_root` hides manifest files from the file list,
  classification and the folder fingerprint (writing one does not make a
  folder look changed; an incremental rescan of a freshly manifested tree
  skipped 14/14 folders).
- A **confirmed** manifest pins its entry file as the unit's representative
  (`claim_groups`, and `forced_file` for single_app folders) and its
  dependent sub-folders are not descended into (`claimed_dir_names`), so
  dependencies can no longer become bogus apps and the setup file is never
  re-guessed. Unconfirmed manifests do not change scanning.
- `resolver.run_resolve` looks up the manifest per candidate
  (`ManifestCache.find`, by entry file name or its pre-archive `orig_name`),
  lets a trusted manifest name a brand-new variant, then calls
  `apply_manifest`. `ResolveProgress` gained `manifests_found/applied/
  db_kept`. `_upsert_variant` now returns `(variant_id, created)`.
- Integrity: if a required member is missing or changed, or the entry is
  gone, an `audit_log` row `manifest_integrity` is written and the app is set
  to `needs_review` unless verified. (No dedicated screen yet, see below.)

### Write side

- Triggers: new variant rows (`manifest_dirty = 1`, uid assigned), and a
  REAL change (`OLD.x IS NOT NEW.x`) to any manifest-relevant app/variant
  column. A no-op UPDATE does not dirty anything.
- Flush points: GUI timer every 4 s (`ManifestFlushWorker`, own DB
  connection), end of a scrape batch / manual Winget / Choco apply, after
  each successful reorganize item and monitor attach, and on window close.
  `suspend_auto_flush()` holds the flusher back while files are moving
  (`execute_reorganize` and `execute_monitor_plan` are now thin wrappers
  around `_execute_reorganize_impl` / `_execute_monitor_plan_impl`).
- Content hash (volatile `updated_at` / `written_by` excluded) makes a flush
  of an unchanged variant a no-op.
- Reorganize: manifests are never copied as "sidecars"; the manifest is
  rewritten at the destination after the move commits. Copy mode writes at the
  destination with `record=False` (the DB row stays at the source). Archive
  mode records the original installer name + fingerprint (`orig_name`,
  `orig_fp`) so both forms match later.
- Manual: toolbar **Write manifests...** (all, with confirmation + background
  `ManifestWorker`), app right-click **Create/update manifest** (multi-select),
  variant right-click **Create/update manifest**.

### Reorganize preview table

All 7 columns are user-resizable (`Interactive`), last column takes spare
room, horizontal scroll when wider than the window, middle-elided paths with
full-path tooltips, double-click a header divider to fit a column. Widths are
remembered in the new setting `reorg_table_col_widths` (list of 7 ints,
debounced save).

### Bugs caught while building (worth remembering)

- In the table-width restore loop I first used `for col, w in ...`; `w` is the
  tab's page widget in `_build_file_structure_tab`, so the loop rebound it,
  Python garbage-collected the page and the dialog died with
  "QVBoxLayout already deleted". Found by bisecting, loop variable renamed.
  Lesson: never reuse `w` / `v` as loop names inside the `_build_*_tab` methods.
- The first draft wrote the plain name `appcatalog.json` for owned folders, so
  two variants whose folders were merged would overwrite each other's file.
  Replaced by personal names + the same-unit collision rule above.

### Verified (synthetic library; old-format DBs built with the ORIGINAL code)

- Old DB opened by the new code: columns/triggers added, uids backfilled,
  0 dirty rows, 0 files written.
- Manual work -> manifests -> folders moved into new category folders -> DB
  deleted -> fresh DB: locked name, verified status, scraped details, locked
  version and locked setup file restored; no "Redist" / "Crack" apps.
- Real `execute_reorganize` (move mode) wrote personal-name manifests inside
  the destination folders; no manifests left behind at the source.
- Missing dependency -> `manifest_integrity` audit row. DB-wins and conflict
  cases. Auto OFF writes nothing and keeps the dirty flags; suspend works.
- Legacy `appcatalog.json` migrated; same-name different-variant manifest
  never overwritten; app rename renames the manifest.
- Headless GUI: toolbar action, settings checkbox save/reload, manual worker,
  auto flush after an edit, context-menu path; resizable table widths saved
  and restored.

### Not done / known gaps

- `monitor.py` change (`_record_variant` now returns the new variant id, the
  manifest is written after the attach) is syntax-checked but was NOT run
  against real folders. First real monitor run should be watched.
- Manifest integrity problems are only in `app.log` and `audit_log`; no
  screen shows them yet (candidate: a Report-tab section).
- A pristine old DB that meets manifests written elsewhere keeps its own
  names/locks (by design). An explicit "import manifests over the database"
  action does not exist yet.
- Not tested on Windows; the touched files have mixed CRLF/LF endings (the
  patches preserved each file's own endings).

### Files touched

New: `app_manifest.py`. Changed: `database.py` (calls `ensure_manifest_schema`),
`config.py` (3 settings), `scanner.py`, `resolver.py`, `scraper.py`,
`app_manager.py`, `monitor.py`, `gui_backend.py` (2 workers), `gui_main.py`
(toolbar, 2 menus, settings group, timer, close flush), `app_organizer.py`
(resizable table).

---

## Checkpoint 32: Rescan/Resolve-all no longer destroys manual work; Resolve-all confirm + backups; installer override; Add app directly; Repair empty apps

*Date: 2026-10-02.*

### Reported

1. Rescanning via the Scan-roots dialog made duplicate app names with EMPTY variants, for many (not all) apps.
2. "Resolve all" was clicked by mistake and wiped the manual organising / renaming; it has no confirmation.
3. "Change installer file" picked a wrong bare exe; the real installer was one level up in a sibling sub-folder. The warning was right but there was no override.
4. No way to add an app without scanning a root.

### Root cause of 1 and 2 (reproduced with the ORIGINAL code, see "Verified")

`resolver._upsert_app` matched an existing app ONLY by `apps.normalized_key`, derived from the
folder/file names of the cluster. `resolver._upsert_variant` then overwrote the variant's `app_id` with
whatever app that returned. So:

* any change of an app's `normalized_key` -- manual rename (`edit_app_field`), the **scraper's auto-rename**
  (`scraper_auto_rename`: sets `name` + `normalized_key`, does NOT lock the name), re-resolve acceptance --
  made the next resolve fail to find the app, CREATE a duplicate with the auto-derived name and MOVE the
  variants into it. The renamed/scraped app (with its description, Winget id ...) was left empty.
  This is why it hit "many but not all" apps: those renamed or scraped. (`normalize_key("Notepad++")`
  happens to equal `normalize_key("Notepad")`, so that rename was unaffected.)
* manual merges (`merge_apps`), moves (`move_variant_to_app`) and splits were silently undone by the same
  `app_id` overwrite. "Resolve all" is the same code path, hence the lost organising work.
* Reorganize updates `variants.source_path` but leaves `raw_candidate_id` on the OLD raw row; a rescan of the
  destination then created NEW variants (duplicates) next to the moved ones.

### Fix (new module `app_curation.py`; all additive)

1. **Aliases** `app_aliases(normalized_key PK, app_id)`. Trigger `trg_apps_key_alias` records the OLD key whenever
   `apps.normalized_key` changes (covers the scraper, manual rename, every writer). `merge_apps` aliases the
   absorbed app (`alias_absorbed_app`). `_upsert_app` looks an unknown cluster key up in the aliases before
   creating an app; an app found by alias keeps its current name (rename was deliberate).
2. **Pinning** `load_pin_map`: an existing variant is NOT re-clustered when its app is protected (name /
   catalog / subcatalog locked, or `status='verified'`) or `variants.app_pinned = 1` (set by merge / move /
   split). Pinned variants only get their own fields refreshed and teach an alias. Existing databases:
   `app_pinned` is backfilled once from `audit_log` (variants with move/split, apps that absorbed a merge).
3. **Adoption** `adopt_orphan_variants`: a variant with no raw candidate (monitor attach, manual add) or a stale
   one (Reorganize moved the folder) is re-linked to the fresh raw row with the same folder + file; its old raw
   row is deleted when that folder no longer exists (otherwise resolve made ghost variants from it).
4. `remove_empty_unprotected_apps` at the end of resolve: apps left empty that carry no lock / verified /
   scrape / description are clutter and are deleted.
5. **Repair** for catalogs already damaged: toolbar **Repair empty apps…** (`find_empty_app_repairs`,
   `apply_empty_app_repairs`, dialog `RepairEmptyAppsDialog`). For each empty app: *merge* (a similar-named app
   in the same catalog has the variants -> they move INTO the empty app, which keeps its name + scraped data;
   the duplicate is deleted unless it has work of its own), *delete* (nothing worth keeping), or *review*.
   Fuzzy score >= 80 (merge) / no manual data (delete) are pre-ticked.
6. `monitor._lookup_app_by_key` falls back to aliases.

### Resolve all: confirmation + backups

* "Re-resolve all" now asks first. Default / Escape button is **Cancel** (Enter cannot trigger it). The text says
  what is re-derived and what is protected. Found while testing: my first version set the default via
  `box.buttons()[-1]`, whose order is not guaranteed, and the destructive button was the default -- use the
  button object returned by `addButton`.
* `app_curation.create_backup(db_path, label, keep, min_interval_s)` writes `backups/<db>-<label>-<timestamp>.db`
  next to the database (sqlite backup API). Taken by `ResolveWorker` (`before-resolve`, keep 10),
  `ScanAndResolveWorker` (`before-scan`, throttled to 1 per 15 min, keep 6), Repair (`before-repair`) and the
  installer override (`before-override`). A backup failure only logs.

### Installer override (outside the variant's folder)

`_change_variant_installer_file` still refuses nothing silently: for a file outside `source_path` it now calls
`DetailPanel._offer_installer_override` -> `app_curation.plan_installer_override` / `apply_installer_override`.
The scanner can only see files under a variant's folder, so the variant's unit root is widened to the COMMON
PARENT folder: variant (and its raw row) re-pointed to that folder, `file_name` = relative path of the chosen
file, `file_locked = 1`, and that folder is marked `single_app` in the scan root's folder layout (existing
machinery: whole subtree = one unit, `forced_file` honoured). Variants already catalogued inside that folder
(typically bogus ones made from the unit's own sub-folders) are listed and removed on confirmation; refused if
more than `MAX_OTHER_APPS_SWALLOWED` (5) OTHER apps live there (it is a category folder) or the common parent is
the scan root / a drive root. Cancel is the default button. After applying, re-scan the root once.

### Add app directly

Toolbar **Add app…** -> `AddAppDialog`: installer file, app folder (defaults to the installer's folder; a parent
is allowed, file stored relative), name / catalog / subcatalog (editable combos of existing values) / version /
edition / arch / language prefilled by `suggest_app_fields` (same extractor as the resolver), optional "scrape
after adding". `add_app_manually`: app created verified with name + catalog + subcatalog locked, variant with
`file_locked = 1`, `app_pinned = 1`, `name_source = 'manual'`, no raw candidate (like a Monitor attach). If the
typed name collides with an existing app (key or alias) the user chooses "Add as variant" or "Create separate
app". Duplicate file at the same folder -> refused. If its folder is later scanned, the variant is adopted (no
duplicate). Manifests are written by auto mode as for any variant.

### Manifest changes (follow-up to checkpoint 31)

Found while testing that a fresh DB did not restore manual merges/moves or a scraped app name. Fixed:
`variant.pinned` is written to the manifest, `app_pinned` is a dirty-trigger column and counts as "valuable",
and a manifest is "trusted" (may name a brand-new variant) when it is confirmed / verified / pinned / scraped /
name-locked. Fresh DB over the reorganized test tree now reproduces the organised state exactly.

### Verified (synthetic libraries; old-format DBs built with the ORIGINAL code via `git archive HEAD`)

| Scenario (manual rename, scraper-style rename, merge, manual move, rescan, Resolve all, Reorganize + rescan dest) | original | new |
|---|---|---|
| apps / empty apps after rescan | 8 / `VLC media player` empty | 5 / none |
| merge, move, scraped data survive Resolve all | all lost | all kept |
| variants after Reorganize + rescan of dest (expect 8) | 16 | 8 |

Also: repair tool on a DB damaged by the original code (scraped "VLC media player" got its 2 variants back, the
duplicate "Vlc" removed, rescan keeps it); old catalog.db opens (0 dirty, uids/pins backfilled); manual add
(+ duplicate / name-collision checks, later scan adopts it); installer override on the "wrong bare exe, real one
in a sibling folder" layout (rescan keeps one variant with the chosen file); headless GUI for the Resolve-all
confirm (Cancel does nothing and takes no backup), repair dialog, add dialog, override dialog.

### Not done / known gaps

* Edition / architecture / language typed into a variant are not locked; Resolve all / rescan still recalculates
  them (the app-level protection does not cover those fields).
* Duplicate VARIANTS that earlier rescans already created (Reorganize then rescan, before this fix) are not merged
  automatically; Clean library / Duplicates tools apply.
* `monitor._fuzzy_candidates` still compares only current keys (the exact lookup uses aliases).
* Adoption matches by normalised folder path + file name; not tested on Windows paths / long-path prefixes.
* The installer override needs one rescan of the scan root afterwards (the dialog says so).
* Extras / dependent-folder roles from FUTURE_PLAN_settings_and_extras.md are still only discussed.

### Files touched

New: `app_curation.py`, `curation_dialogs.py`. Changed: `database.py` (calls `ensure_curation_schema`),
`resolver.py` (pin map, adoption, alias lookup, pinned pass, empty-app cleanup, merge/move/split pin),
`monitor.py`, `app_manifest.py`, `gui_backend.py` (backups), `gui_main.py` (toolbar, Resolve-all confirm,
override dialog, add/repair handlers).

---

## Checkpoint 33: Manifest identity (fresh DB no longer merges separately-organised apps) + UI tidy-up

*Date: 2026-10-03.*

### Reported

1. **Repair empty apps…** does not belong on the top bar -> context menu (or Settings).
2. **Add app…** should sit next to the **Scan roots…** button.
3. **Split into new app…** (variant table) should use the Add-app dialog, prefilled from the variant, editable.
4. *Bug report:* a manually edited + Organized library, scanned by a NEW database and resolved from the manifests,
   still ends with variants under the wrong app name / under another app, even across different sub-catalogs.

### Why (4) happened -- reproduced

The manifests DO carry the app name (`app.name`, plus `app_uid`, locks, scraped data). The resolver used them badly:

* `override_fields_from_manifest` only applied the manifest to "trusted" manifests (confirmed / verified / name-locked);
  every other manifest was ignored and the variant fell back to filename/folder heuristics.
* Even a trusted manifest only replaced the variant's NAME. The variant then still went through
  `cluster_candidates`, which fuzzy-merges names across ALL catalogs (token-sort >= 88, plus a rule that merges any
  name that is a strict prefix of another, >= 6 chars). So names the user had deliberately kept apart were glued back
  together, and the cluster took the highest-confidence name -- i.e. the trusted one.
* `app_uid` was in the manifest but never used for grouping; `_upsert_app` matched by name only.

Reproduction (`/tmp`-style harness, 6 apps, then split/rename by hand, Reorganize, brand-new DB):
`Adobe Photoshop` + `Adobe Photoshop Elements` -> ONE app named "Adobe Photoshop Elements" holding BOTH variants;
`Foxit-Reader` + `Foxit Reader Portable` -> ONE app "Foxit Reader Portable". This is exactly the reported symptom.

### Fix

* **Every manifest decides identity for a fresh variant** (not only trusted ones): it was written from the DB's own
  state, so using it reproduces that state. `override_fields_from_manifest` no longer checks `trusted`;
  `apply_manifest` sets the app name for a newly created app unconditionally (locks still come from the manifest).
* **Manifest-bound clusters** (`resolver.cluster_manifest_members`, `Cluster.app_uid`, `Cluster.manifest_bound`):
  candidates whose identity came from a manifest are grouped by `app_uid` (fallback: exact name) and are NEVER
  fuzzy-merged with anything. Same name + same catalog/subcatalog under different uids (a regenerated uid) is still one
  app. The canonical name is the newest manifest's own spelling. Only the remaining candidates use
  `cluster_candidates`.
* **`_upsert_app` matches by `app_uid` first**, then by name -- preferring the app in the SAME catalog/subcatalog
  (a manifest-bound cluster matches by name only inside its own catalog/subcatalog, and never via alias). A new app
  keeps the manifest's `app_uid`, so identity survives a lost DB. An app matched by uid keeps its current name.
* Result on the reproduction: grouping and names identical to before the DB was lost, and every `app_uid` preserved.
  Earlier suites (rename/scrape/merge/move/rescan/Resolve-all/Reorganize, manifest recovery, repair, add, override,
  Resolve-all confirm) unchanged.

### UI

* Toolbar: **Add app…** now directly after **Scan roots…**; **Repair empty apps…** removed from the toolbar and
  added to the app-table context menu ("Repair empty apps… (whole library)").
* **Split into new app…** opens `AddAppDialog` in *split mode* (`split_variant=`, `split_app=`): installer file, app
  folder, name, catalog / subcatalog, version, edition, architecture, language prefilled from the variant and editable;
  button reads "Split". Name equal to the current app is refused; a name that exists elsewhere asks "Move into that
  app / Create separate app / Cancel". Backend: `app_curation.split_variant_with_details`:
  - same folder, other file -> `file_name` changes + `file_locked`;
  - different folder -> variant detached from its raw row (like a manual add), new folder/file locked, old raw row
    dropped if unused (a rescan lists the old folder again if it still exists);
  - changed version -> `version_locked`; new app is verified with name/catalog/subcatalog locked; variant `app_pinned`;
  - the old app is deleted if that was its only variant and it carries no manual/scrape data.
  The old `resolver.split_variant_to_new_app` is kept (no longer used by the GUI).

### Not done / known gaps

* A manifest that is not found at all is still ignored (`ManifestCache.find` matches the scanner's primary file to the
  manifest's `entry.path` / `orig_name`). If an UNCONFIRMED manifest's folder contains several installers and the
  scanner picks a different one, that variant falls back to heuristics. Candidate fix: fall back to the single manifest
  of a folder / match by entry fingerprint.
* An app has ONE catalog/subcatalog (folder majority vote across its variants); manifests intentionally do not store it.
* Two genuinely different apps with the same name in the same catalog/subcatalog are still treated as one.
* Variants pointing at different `app_uid`s but identical name+home are merged (by design, see above).

### Files touched

`resolver.py` (Cluster fields, `cluster_manifest_members`, `run_resolve`, `_upsert_app`), `app_manifest.py`
(`override_fields_from_manifest`, `apply_manifest`), `app_curation.py` (`split_variant_with_details`),
`curation_dialogs.py` (split mode), `gui_main.py` (toolbar, context menu, split handler).
