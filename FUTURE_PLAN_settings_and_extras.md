# Future Improvement Plan: Settings Dialog Reorganization + Special-Category "Extras"

Status: **PLANNED, not implemented.** No code has changed as a result of
this report. Written after a discussion session; captures the finalized
design so a future coding session can start straight from here.

---

## 1. Settings dialog reorganization

### Problem
The Filters tab crams 9 numbered, sparsely-explained items into one
scroll area. Two mechanisms that both affect a catalog/subcatalog's
final label live in different tabs and look like duplicates.

### The three naming mechanisms (not duplicates, but confusing overlap)

| Mechanism | Setting key | Runs | Matches on | Effect |
|---|---|---|---|---|
| `FolderLayoutDialog` rename field | `scan_roots.folder_layouts_json` | scan time, per scan root | one specific folder | most specific; overrides everything below it |
| Category/subcategory rules | `category_rules` / `subcategory_rules` | scan time, global | regex against the full relative path | can change WHICH folder is picked as catalog/subcategory, not just relabel it |
| Folder name aliases | `folder_name_aliases` | resolve time, global (`resolver._apply_alias()`) | exact case-insensitive match on the already-decided catalog/subcatalog string | pure relabeling; also the fallback smart-case title-caser when nothing matches |

All three stack in the order above. Not currently obvious from the UI.

### Proposed regrouping (shape only, not final widget layout)

1. **"Scanning & Noise" tab** -- everything deciding what gets scanned
   at all: `container_folder_keywords`, `ignore_folder_names`/
   `ignore_folder_name_patterns`/`ignore_filename_patterns`, and (see
   part 2 below) the new `special_categories` editor replacing
   `noise_folder_keywords`/`noise_short_only_keywords`. One clear
   sentence per group on what it does to results (excluded entirely vs.
   deprioritized vs. recategorized).
2. **"Naming & Renaming" tab** -- all three naming mechanisms from the
   table above, grouped together and explicitly labeled by when/how
   they match ("Rename by exact folder name" / "Rename by path
   pattern"), plus a note cross-referencing `FolderLayoutDialog` for the
   per-scan-root version. Also candidate home for `app_name_synonyms`,
   `edition_keywords`, `language_keywords`, `build_number_pattern`.
3. Existing Resolver / Archives / Scan / Scraper / Appearance / Monitor
   tabs stay as-is, audited later for anything that belongs in the two
   new groups.

---

## 2. Special-category "Extras" (crack/keygen/tutorial/plugin/theme, user-definable)

### Problem
Folders matching `noise_folder_keywords` are discarded outright
(`unit_type='noise'`, no trace beyond the raw row). The user has many
apps with bundled or adjacent addons, plugins, tutorials, and
crack/keygen/serial content that should be tracked, categorized, and
optionally linked to their parent app -- not silently thrown away, and
not mixed into the main Apps list either.

### Definable categories (replaces `noise_folder_keywords` entirely)

New setting, `special_categories`, a dict of user-defined category name
-> keyword list (category names themselves are user-definable, not
fixed to a hardcoded set):

```json
{
  "crack":    ["crack", "cracks", "cracked", "keygen", "keygens", "patch", "patches", "serial", "hotfix"],
  "tutorial": ["tutorial", "tutorials"],
  "plugin":   ["plugin", "plugins", "addon", "addons", "extension", "extensions"],
  "theme":    ["theme", "themes", "skin", "skins"]
}
```

A folder matching no configured category behaves exactly like today's
plain noise (discarded, no change for anyone who defines nothing new).
Same whole-word matching semantics as today's keyword lists -- singular
and plural still need separate entries (confirmed via testing:
`\btutorial\b` does not match `"tutorials"`).

### Storage: new `extras` table, separate from `apps`/`variants`

Roughly: `id, category, name, catalog, subcatalog, source_path,
raw_candidate_id, linked_app_id (nullable), link_method
('nested'|'exact_name'|None), first_seen_at`. Kept structurally separate
from `apps` so extras are browsable as their OWN section, never mixed
into the main Apps table/tree (per the explicit requirement that they
show up separately, not "along all other apps").

### Linking -- finalized scope after discussion

**Automatic linking only for two clear-cut cases** (no auto-linking at
any fuzzy-confidence level -- everything fuzzy goes to manual mapping,
per the user's explicit call):
1. **Nested** -- the extra folder sits literally inside a resolved
   app's own install-unit folder (structural path-prefix check, no
   ambiguity).
2. **Exact resolved-name match** -- the extra's cleaned name (keyword
   stripped) exactly matches an already-resolved app's name within the
   same catalog/subcatalog.

**Everything else is manual.** A new "Map Extras" tab/dialog, modeled
directly on `OrganizeDialog`'s existing "Duplicates" tab pattern (`app_
organizer.py`, `_build_duplicates_tab()`/`_scan_duplicates()`/`_merge_
selected_duplicate_group()`): groups of extras alongside their best
fuzzy-matched candidate app(s), user checks off and assigns manually --
an extra can be mapped to one or more apps, left unmapped, or deferred.
Reuses `app_manager.find_duplicate_groups()`'s fuzzy matcher rather than
building new matching logic.

**Fuzzy search scope**: defaults to the extra's own catalog/subcatalog
(matches the common nested/sibling case), with an optional wider
whole-library search specifically for extras that found no candidate
nearby -- e.g. a "search whole library" button per unmatched item or
group in the Map Extras tab, rather than always paying the cost of a
library-wide fuzzy pass for every extra.

### UI surfacing

- New "Extras" node in the left tree, browsable by category (Cracks &
  Keygens / Tutorials / Plugins / Themes / ...), separate from the main
  Apps view.
- App detail panel gains an "Extras" section listing anything linked
  (via nested, exact-name, or manual mapping) to the selected app.
- Settings: category editor (add/rename/remove category, edit its
  keyword list) living in the reorganized "Scanning & Noise" tab from
  part 1.
- "Map Extras" tab/dialog for manual fuzzy-match resolution, as above.

### Tutorials -- open question, not resolved

Tutorials may not need app-linking at all -- a `TUTORIALS` catalog can
already be scanned as a normal Catalog via `FolderLayoutDialog` today
(mark it Catalog, don't add it to `special_categories`) and browsed like
any other catalog. Whether tutorials specifically need per-app linking,
or are fine as their own standalone browsable catalog, still needs a
decision before building the tutorial-specific piece of this.

---

## 3. Suggested build order (not committed, just a reasonable sequence)

1. `special_categories` setting + migrate `noise_folder_keywords` editor
   into the new grouped UI (part 1's "Scanning & Noise" tab).
2. `extras` table + scanner/resolver changes to populate it instead of
   discarding matched folders.
3. Nested + exact-name auto-linking.
4. Extras tree section + app detail panel "Extras" block (read-only).
5. "Map Extras" manual tab, reusing the duplicate-matcher.
6. Settings dialog naming-mechanisms regrouping (part 1's "Naming &
   Renaming" tab) -- independent of the extras work, can happen anytime.
