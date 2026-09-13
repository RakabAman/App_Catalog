"""
Default values for every setting exposed in the GUI's live settings panel.
All of these are stored in the `settings` table (DB-backed, per the
"inside the same SQLite DB" decision) and can be changed without restart.
"""

DEFAULT_SETTINGS = {
    # -- resolver confidence thresholds --------------------------------
    # >= this score: auto-accepted, status='resolved'
    "confidence_auto_accept": 0.85,
    # below this score: status='needs_review' (between the two = resolved
    # but visually flagged as "low confidence" in the table)
    "confidence_needs_review": 0.60,

    # -- fuzzy clustering (grouping variants into one app) --------------
    # 0..100, rapidfuzz token_sort_ratio threshold. Higher = stricter
    # (fewer, more precise merges). Lower = looser (more aggressive merges).
    "fuzzy_match_threshold": 88,

    # -- archive handling -------------------------------------------------
    # If listing an archive's contents doesn't give us a confident single
    # installer name/version match, we escalate to full extraction.
    "archive_ambiguity_escalates_to_extraction": True,
    # Scratch folder for temporary full extraction (auto-purged after use)
    "archive_scratch_dir": None,  # None = use system temp dir
    "archive_purge_scratch_after_use": True,
    # Max archive size (MB) we will fully extract even when ambiguous --
    # safety valve so a single 40GB ISO doesn't stall the whole scan.
    "archive_max_full_extract_mb": 2048,

    # -- deep inspection (PE metadata + archive content) -------------------
    # Split into two independent switches -- "going through archive
    # content/metadata" covers two genuinely different operations with
    # different cost/risk profiles, so they're controlled separately.
    # Both OFF by default: the scanner then only uses file/folder names for
    # identification, which is much faster over 800GB and has zero risk of
    # archive-extraction issues. Turn either on for better accuracy at the
    # cost of scan speed.
    "read_exe_metadata_enabled": False,      # read PE version resource from .exe files
    "inspect_archive_contents_enabled": False,  # list/extract .zip/.rar/.7z/.iso contents

    # -- version resolution priority -------------------------------------
    # If both PE version resource and folder/file-name-parsed version are
    # found and disagree, prefer the PE resource (usually more reliable).
    "prefer_pe_version_over_parsed": True,

    # -- noise / container folder detection --------------------------------
    # Folder name keywords (case-insensitive substring match) that mark a
    # folder as a "container" (part of a larger install) rather than a
    # standalone install unit -- e.g. Dreamweaver's payloads/redist/resources.
    "container_folder_keywords": [
        "payloads", "payload", "redist", "resources", "resource",
        "common", "commonfiles", "drivers", "runtime", "runtimes",
        "_files", "support", "docs", "documentation", "samples",
        "sample", "helpcenter", "help center",
    ],
    # Folder names that indicate junk / non-installer content entirely
    # (tutorials, scraped web pages, etc.) -- excluded from scanning results
    # but still recorded with unit_type='noise' for transparency.
    "noise_folder_keywords": [
        "_files", "tutorial", "tutorials",
        # crack/patch/keygen/update/skin/theme folders: these must be
        # excluded ENTIRELY (not scanned as install candidates at all), not
        # just deprioritized for naming -- a patch/crack/skin/theme for an
        # app one level up is not a separate app nor a separate version of
        # it. Only excluded when the folder name is SHORT (see
        # noise_short_only_keywords/noise_short_only_max_len below) -- a
        # long release name that merely mentions "Keygen" as one descriptor
        # among many ("Adobe.Captivate.v2.0.1177.WinALL.Keygen.Only-ViRiLiTY")
        # is the real install folder, not a bare keygen dump.
        "crack", "cracks", "cracked", "patch", "patches", "patched",
        "keygen", "keygens", "serial", "serials", "update", "updates",
        "hotfix", "license", "skin", "skins", "theme", "themes",
        # SPTD is a specific anti-piracy driver bundled with DAEMON Tools
        # and similar disc-emulation tools -- a folder that IS just
        # "SPTD" (containing SPTD.rar/exe) is always this driver
        # component, never a standalone app in its own right, so it's
        # safe to always exclude (not depth/length-gated the way
        # "languages" below is, since there's no plausible legitimate app
        # that's just named "SPTD").
        "sptd",
        # "Other languages"/"Language pack" subfolders under a real app's
        # release folder are supplementary translation files, not a
        # separate app -- short-only gated (see below) since a real
        # language-learning app could plausibly be named just "Languages".
        "languages", "language pack",
    ],
    # Which noise_folder_keywords only count as "exclude this folder" when
    # the folder's name is short (<= noise_short_only_max_len characters) --
    # i.e. the folder basically IS just this word, not a substantive release
    # name that happens to mention it.
    "noise_short_only_keywords": [
        "crack", "cracks", "cracked", "patch", "patches", "patched",
        "keygen", "keygens", "serial", "serials", "update", "updates",
        "hotfix", "license", "skin", "skins", "theme", "themes",
        "languages", "language pack",
    ],
    "noise_short_only_max_len": 25,

    # -- release-group / scene tag stripping (regex fragments) -------------
    # ORDER OF THE NAME-CLEANING PIPELINE (applied in this exact sequence
    # to the chosen source text before version/edition/arch/language are
    # extracted and the name is finalized):
    #   0. SOURCE SELECTION: try FILE name first, then FOLDER name, then
    #      one level up from folder, then two levels up -- at each step,
    #      the ignore lists/patterns below are applied and the result is
    #      checked for validity (non-empty, not purely generic/numeric).
    #      First valid result wins. This replaces a fixed "prefer file
    #      unless depth<=2" rule with an actual cascade, so e.g. a file
    #      named "setup.exe" correctly falls through to the folder name,
    #      and a folder that's just "32-bit" correctly falls through to
    #      its parent.
    #   1. bracket_content_patterns  (strip {...}/[...]/(...) content entirely)
    #   2. website_tag_patterns      (site names/domains stamped into the name)
    #   3. release_tag_patterns      (scene-release/keygen/crack flags)
    #   4. ignore_filename_words / ignore_folder_names (+ pattern variants)
    #   5. build-number extraction (folded into version, not left in name)
    #   6. version extraction & removal
    #   7. architecture keyword removal
    #   8. edition keyword removal (whole-token match only, ALL matches
    #      stripped -- "Pro Advanced" both leave, not just the first)
    #   9. language keyword removal (whole-token match only)
    #   10. tidy (dots/underscores -> spaces, collapse whitespace)
    #   11. smart-case normalization
    # All of these are editable here/in Settings so the pipeline can be
    # tuned without touching code.

    # Step 1: content inside {}, [], or () is release-uploader/tag noise
    # (site names, uploader handles, "with SPTD", crack/keygen mentions,
    # etc.) and should never survive into the app name -- strip the
    # brackets AND everything inside them. Applied first, before any other
    # cleaning, since these wrappers can contain arbitrary junk that would
    # otherwise confuse later steps (version/edition/website matching).
    "bracket_content_patterns": [
        r"\{[^}]*\}",
        r"\[[^\]]*\]",
        r"\([^)]*\)",
    ],

    # Step 2: website/domain tags stamped into folder or file names, e.g.
    # "www islamdigit blogspot com", "HaxPC.net-AppName". Applied first
    # since site tags often wrap around everything else.
    "website_tag_patterns": [
        r"\bwww\.?\s*(?:[\w\-]+[\.\s]+)*(com|net|org|info|biz|me|tv|cc|ir|ru)\b",
        r"\b[\w\-]{3,}\.(com|net|org|info|biz|me|tv|cc)\b",
        r"\[\s*www[\.\s][\w\.\-\s]+(com|net|org)\s*\]",
    ],

    # Step 2: regex-based release/scene tags.
    "release_tag_patterns": [
        r"-[A-Za-z0-9]+$",              # trailing -GROUPNAME
        r"\[[A-Za-z0-9.\- ]+\]",        # [ChingLiu]-style brackets
        r"\{[^}]+\}",                    # {H33T}{projectmyskills}-style curly-brace tags
        r"@[A-Za-z0-9_]+",               # @IGI-style uploader handles
        # "by UploaderName" -- NOT anchored to end-of-string, since a
        # version/other suffix often follows it ("...by Jiri Mahel-v2.2");
        # capped at a few name-words so it can't run away and eat unrelated
        # trailing text.
        r"\bby\s+[\w'\u00ae\u2122.]+(\s+[\w'\u00ae\u2122.]+){0,3}",
        # multi-part archive segment markers left in the stem after grouping
        # (e.g. "BigApp.part1.rar" -> stem "BigApp.part1" -- the ".part1"
        # must not leak into the app name)
        r"\bpart\s*\d+\b",
        r"\b(keygen|crack|cracked|patch|patched|portable|multilanguage|winall|"
        r"incl\.?\s*keygen|full|only|incl|unlocked|retail|preactivated|repack|"
        r"license\s*key|serial\s*key|serial\s*number)\b",
        # dual-architecture mentions embedded in a longer name -- can't map
        # to a single architecture value, so stripped rather than parsed:
        # "32+64 Bits", "32 & 64 bit", "x86+x64", "x86 and x64"
        r"\b32\s*(\+|&|and)\s*64\s*bits?\b",
        r"\bx86\s*(\+|&|and)\s*x64\b",
        # standalone "+" used as a concatenator ("Software + Key") -- not
        # part of the app name once the things it joins are stripped
        r"\s\+\s",
        r"\+",
    ],

    # NOTE: "Setup"/"Install" glued directly to a version number with no
    # separator ("AirtableSetup1.3.2") are deliberately NOT handled here as
    # a "\bsetup\d*\b"-style regex -- that greedily eats the leading digit
    # of the version too (turning "Setup1.3.2" into "Setup1" + leftover
    # ".3.2", misparsed as version "3.2"). Instead resolver/extract.py
    # handles this case with a lookahead that strips only the word, never
    # the digits, reusing the ignore_filename_words list below.

    # Step 3: plain word list (no regex needed) -- simpler for day-to-day
    # tuning than editing regex. Whole-token match only (same compound-word
    # safety as edition/language matching -- see resolver/extract.py).
    "ignore_filename_words": [
        "setup", "install", "installer", "patch", "serial", "beta", "trial",
        "demo", "final", "repack", "rip", "iso", "update", "upd", "fix",
        "nulled", "activated", "activator", "loader", "license",
        "key", "keys", "keyfile", "licensed", "sptd", "with",
        # generic installer-stub filenames that carry no real product name
        # of their own -- "isdel.exe" (a common InstallShield uninstall/
        # self-delete stub) was wrongly winning the naming cascade over the
        # real app folder name ("Isdel" instead of "Winglobe").
        "isdel",
        # release-group "contact pack" / "contactpack" tag, same idea as
        # the other release-tag noise words above.
        "contactpack",
    ],

    # Folder names that should NEVER be used as a naming source (they're
    # structural/generic, not app names) -- e.g. a folder literally named
    # "32" containing a 32-bit build was becoming the app name "32".
    # Used both when climbing for parent-folder context and when deciding
    # whether to prefer the file name over the folder name outright.
    "ignore_folder_names": [
        "setup", "install", "installer", "bin", "x86", "x64", "win32", "win64",
        "32", "64", "new", "old", "temp", "tmp", "files", "common", "data",
        "output", "release", "debug", "misc", "other",
    ],

    # PATTERN (regex) version of the above -- covers variants that a plain
    # word list can't, e.g. "32 - Bit", "32-bit", "64 Bit" (a folder that's
    # really just a bitness label for the app one level up, not the app
    # name itself). Checked in addition to ignore_folder_names; either
    # matching marks the folder as unusable for naming.
    "ignore_folder_name_patterns": [
        r"^\d{2}\s*-?\s*bits?$",          # "32 Bit", "32-bit", "64 - Bit"
        r"^(x86|x64|win32|win64|arm64)$",
        r"^v?\d+(\.\d+)*$",               # bare version-only folder, e.g. "1.1", "2.0"
        r"^cs\d$",                         # bare product-line marker, e.g. "CS3", "CS6"
        r"^new\s*folder(\s*\(\d+\))?$",   # Windows default "New folder", "New folder (2)"
    ],

    # Same idea for FILE names: a plain word list plus a pattern list, so a
    # generic filename like "setup(1).exe", "install_x64.exe", or a bare
    # "32-bit.rar" is recognized as non-descriptive and the resolver falls
    # back to the folder name (or one level further up) instead of using
    # the generic filename as the app name.
    "ignore_filename_patterns": [
        r"^setup\s*(\(\d+\))?$",
        r"^install(er)?\s*(\(\d+\))?$",
        r"^\d{2}\s*-?\s*bits?$",
        r"^(x86|x64|win32|win64)$",
    ],

    # Folder/catalog/subcatalog name -> preferred display name conversions,
    # e.g. {"burners": "CD/DVD Burner"}. Keys are matched case-insensitively
    # against the raw folder name. Applied to catalog/subcatalog (and would
    # apply to an app name if it happens to exactly match a key).
    "folder_name_aliases": {},

    # App-name synonyms: regex -> canonical replacement, checked AFTER all
    # other name cleaning (bracket/noise/version/edition stripping) but
    # BEFORE clustering. Idea borrowed from a parallel build of this app
    # (DeepSeek's "app_name_synonyms") after a side-by-side review -- it's a
    # direct, explicit fix for exactly the case fuzzy clustering handles
    # unreliably: a KNOWN misspelling or rebrand of a specific app, e.g. the
    # real-world "Deamon Tools" (typo'd folder name, actual product is
    # "DAEMON Tools") from checkpoint 11, which normalize_key()+fuzzy match
    # alone could not be trusted to always merge (a typo can push the fuzzy
    # ratio below fuzzy_match_threshold depending on exact spelling). Each
    # entry is applied with re.sub(pattern, replacement, name, flags=I) --
    # keep patterns narrow (anchored/specific) so this can't accidentally
    # rewrite an unrelated app name that happens to share a substring.
    "app_name_synonyms": [
        {"pattern": r"(?i)^(demon|deamon)\s+tools", "replacement": "DAEMON Tools"},
    ],

    # -- build-number recognition ---------------------------------------------
    # "ACDSee Pro 6.2 Build 212 Final" -- "Build 212" is part of the VERSION,
    # not the app name. This pattern is checked in addition to the main
    # VERSION_PATTERNS cascade (in resolver/extract.py) specifically so the
    # matched "Build NNN" text gets stripped from the name and appended to
    # the version instead of leaking into the app name as free text.
    "build_number_pattern": r"\bbuild\s*#?\s*(\d+)\b",

    # Bare trailing version numbers with no separator context, e.g.
    # "Able2Extract Professional 10" -- the "10" IS the version, but a
    # bare 1-3 digit number is ambiguous enough elsewhere that this is
    # opt-in and only ever applied to the LAST number in the cleaned name,
    # never a number in the middle (too likely to grab an unrelated digit).
    "allow_bare_trailing_number_as_version": True,

    # Words that mark an install unit as a portable build (no installer,
    # runs standalone) rather than a normal setup -- checked against
    # folder/file name tokens, sets ExtractedFields.is_portable and the
    # "(Portable)" name suffix. Editable from Settings > Advanced.
    # NOTE: this key must stay in DEFAULT_SETTINGS -- it used to be
    # missing, which meant the Settings dialog showed an empty list (not
    # this real default) and saving the dialog even without touching this
    # field would silently persist an empty list, permanently disabling
    # portable detection for the whole catalog. Fixed; keep it defined.
    "portable_indicator_words": ["portable", "paf"],

    # -- architecture / edition / language keyword maps -----------------------
    "architecture_keywords": {
        "x64": ["x64", "64bit", "64-bit", "64 bit", "win64", "amd64"],
        "x86": ["x86", "32bit", "32-bit", "32 bit", "win32"],
        "arm64": ["arm64"],
    },
    # Whole-token match (see resolver/extract.py's compound-word-safety
    # comment) -- "professional", "pro", "platinum" etc. get pulled out of
    # the name into a separate `edition` field so "Able2Extract" and
    # "Able2Extract Professional" can cluster as one app with two editions
    # rather than becoming two separate apps.
    # Catalog/subcategory regex rules -- first-match-wins, checked against
    # the FULL relative folder path (case-insensitive), evaluated BEFORE
    # the plain "first path segment = catalog, second = subcatalog"
    # fallback. Idea from a parallel build of this app (DeepSeek's
    # category_rules/subcategory_rules) after a side-by-side review: lets
    # a messy real collection where the same category is spelled a dozen
    # different ways across different backup sources ("GRAPHICS", "Graphic
    # Design", "IMG TOOLS"...) get normalized to one clean taxonomy via
    # config, without renaming anything on disk. Empty by default --
    # existing behavior is completely unchanged until rules are added.
    # Example: [{"pattern": ".*ADOBE.*", "value": "Graphics"}]
    "category_rules": [],
    "subcategory_rules": [],

    # -- per-scan-root folder layout (see FolderLayoutDialog / scanner
    # resolve_scan_root_layout()) -- label used when a folder ends up with
    # no catalog or no subcatalog at all. User-editable so it isn't stuck
    # as a hardcoded string.
    "fallback_catalog_name": "MISC",
    "fallback_subcatalog_name": "MISC",

    "edition_keywords": [
        "professional", "pro", "enterprise", "ultimate", "premium", "premiere",
        "home", "standard", "business", "server", "community", "ce",
        "lite", "academic", "student", "edu",
        "platinum", "advanced", "expert", "elite", "essentials", "plus",
        "ultra", "aio", "suite", "hd",
        # NOTE: deliberately NOT including "free" -- it's extremely common as
        # the literal first word of a real app name (FreeFileSync, FreeArc,
        # Free Download Manager), so treating it as a generic "free edition"
        # marker caused real false positives. Add it back here if your own
        # collection doesn't have that overlap.
    ],
    # word -> display label, whole-token match, same reasoning as edition.
    "language_keywords": {
        "multilanguage": "Multilanguage",
        "multi-language": "Multilanguage",
        "multilingual": "Multilanguage",
        "english": "English",
        "french": "French",
        "german": "German",
        "spanish": "Spanish",
        "italian": "Italian",
        "japanese": "Japanese",
        "chinese": "Chinese",
        "russian": "Russian",
    },

    # -- scan behavior ----------------------------------------------------
    "scan_follow_symlinks": False,
    "incremental_scan_by_default": True,  # skip unchanged folders via fingerprint

    # -- scraper (checkpoint 12: Winget manifest + Chocolatey enrichment) ---
    # "sources enabled" flag kept from the earlier placeholder for anything
    # still to come (e.g. a future AlternativeTo source); the two sources
    # below are now real and independently toggleable.
    "scraper_sources_enabled": [],

    # svrooij/winget-pkgs-index: a large (~5,000+ app), frequently-updated
    # manifest used for automatic background enrichment. Cached locally next
    # to catalog.db; only re-downloaded when older than the staleness window
    # below (this app has no persistent background process, so "every 4
    # hours" from the upstream repo becomes "re-check if stale, next time
    # a scrape runs" rather than a literal poller).
    "scraper_winget_manifest_url":
        "https://github.com/svrooij/winget-pkgs-index/raw/main/index.v2.json",
    "scraper_manifest_staleness_hours": 4,
    # In DEFAULT_SETTINGS, after "scraper_winget_show_timeout_seconds":
    "scraper_winutil_apps_url": "https://raw.githubusercontent.com/ChrisTitusTech/winutil/main/config/applications.json",
    # If OFF (default), a manifest match never changes an app's `name` --
    # only publisher/description/homepage_url/tags/winget_id/latest_version
    # are filled in. If ON, a manifest display name that differs from the
    # current name replaces it (old name kept in `alt_source_name`), UNLESS
    # the app's name is locked (name_locked=1), which is never overridden
    # either way. Left off by default because the resolver's naming pipeline
    # (see checkpoints 7-11) is deliberately tuned per this app's own
    # collection, and a generic manifest marketing name isn't necessarily an
    # improvement over an already-correctly-resolved name.
    "scraper_auto_rename": False,

    # Only these statuses are attempted for automatic background enrichment
    # -- deliberately excludes "needs_review" (name may still change, so a
    # manifest hit could easily attach to the wrong app) and "ignored".
    # Manual Chocolatey lookup (a user picking one specific app) isn't
    # restricted by this list -- it's a deliberate, targeted action.
    "scraper_auto_enrich_statuses": ["resolved", "verified"],

    "scraper_choco_default_max_results": 5,

    # checkpoint 14: winget show fallback for real description/license
    # (the bare manifest only has Name/PackageId/Version/Tags -- see
    # scraper/manifest.py's module docstring). OFF by default for the
    # BATCH "Scrape all" path specifically -- it's one extra subprocess
    # call per matched app (a few hundred ms to a couple seconds each,
    # depending on network conditions winget itself experiences), so 350
    # apps could add minutes to a run. The manual "Search & match…" dialog
    # always fetches it for the single candidate the user picks regardless
    # of this setting, since that's only ever one call.
    "scraper_winget_show_for_auto_scrape": False,
    "scraper_winget_show_timeout_seconds": 10,

    # -- GUI appearance -----------------------------------------------------
    # Global multiplier applied to the whole UI (fonts, widget sizes,
    # spacing) via Qt's QT_SCALE_FACTOR mechanism. Must be read and applied
    # BEFORE QApplication is constructed (see run_gui.py), so changing this
    # takes effect on next launch, not live within the current session.
    "ui_scale_multiplier": 1.0,
    
    # -- monitor (checkpoint 18: manual "Monitor" job) ---------------------
    # Manual job: walks each monitor folder, plans what to do with every
    # new candidate file, lets the user review/edit the plan, then
    # move-or-copies each file into the organized structure (optionally
    # compressing it first). Not a background watcher -- see monitor.py's
    # module docstring for the full design.

    # Folders walked by the Monitor job. Separate from scan_roots (which
    # are the organized-app ROOTS and stay where they are). One per line
    # in Settings > Monitor.
    "monitor_folders": [],

    # Extensions the Monitor job considers. Defaults to the same set the
    # scanner already treats as installer/archive payloads.
    "monitor_extensions": [
        ".exe", ".msi",
        ".zip", ".rar", ".7z", ".iso", ".tar", ".gz", ".tgz", ".cab",
    ],

    # Extensions that are ALREADY compressed -- these are moved as-is
    # (double-compressing an ISO or a .zip gains ~nothing and doubles the
    # I/O). Everything not in this list gets compressed to the chosen
    # archive format before being placed.
    "monitor_already_compressed_extensions": [
        ".zip", ".rar", ".7z", ".iso", ".tar", ".gz", ".tgz", ".cab",
        ".bz2", ".xz",
    ],

    # Partial-download markers -- anything ending in one of these is
    # ignored entirely (a mid-download .exe.crdownload is not a real file).
    "monitor_skip_partial_extensions": [
        ".crdownload", ".part", ".tmp", ".partial", ".!ut", ".aria2", ".downloading",
    ],

    # Minimum file size (MB) to process. Stops a stray empty .exe from
    # cluttering the plan.
    "monitor_min_size_mb": 1,

    # A file must have been quiet (unchanged mtime) for this many seconds
    # before it's considered settled enough to process -- avoids grabbing
    # a file mid-copy on Windows where .crdownload isn't always used.
    "monitor_settle_seconds": 3,

    # Default archive format for the compression step. "7z" is the
    # project's default; "zip" is always available (stdlib), "rar" needs
    # an external `rar` binary and will fall back to 7z/zip with a note
    # in the report if it isn't present.
    "monitor_archive_format": "7z",

    # Move (default) vs copy -- chosen per-run in the start dialog, this
    # is just the default the dialog pre-selects.
    "monitor_move_mode": "move",

    # After a file is attached to an app, fire a one-app Winget-manifest
    # scrape so the app's metadata stays current. Cheap (cached manifest).
    "monitor_auto_scrape_on_attach": True,

}
