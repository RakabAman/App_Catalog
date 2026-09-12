"""
Database module: schema (DDL) + connection/settings accessor, consolidated
from what were previously db/schema.py and db/connection.py.
"""

"""
Database schema for the App Catalog tool.

Design principles (per project discussion):
- Scan output (raw_candidates) and Resolve output (apps/variants) are separate
  layers. Re-resolving never re-touches the filesystem.
- Every resolved field that a user manually edits gets locked so re-resolve
  never silently overwrites a human correction.
- Settings live in this same DB (single-file simplicity) and are versioned,
  so every resolved record can record which settings version produced it.
"""

SCHEMA_VERSION = 1

DDL = """
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Settings: single-row-per-key live config, editable from the GUI with
-- no restart required. resolver reads this table at the start of every run.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,       -- stored as JSON-encoded string
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Bumped every time settings that affect resolution logic are saved.
-- apps/variants store the version that produced them so "resolved before
-- rule change X" can be filtered and selectively re-run.
CREATE TABLE IF NOT EXISTS settings_version (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    note            TEXT
);

-- ---------------------------------------------------------------------
-- Scan roots: the folder(s) being scanned, so re-scans know their base
-- and can do incremental/resumable walks.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_roots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL UNIQUE,
    last_scan_started_at   TEXT,
    last_scan_finished_at  TEXT,
    last_scan_status        TEXT      -- running | completed | failed | cancelled
);

-- ---------------------------------------------------------------------
-- Raw candidates: pure structural output of the Scanner. No naming
-- intelligence applied here -- just "what did we find on disk".
-- This table is permanent history; Resolver reads from it and never
-- mutates it. Re-resolving = re-reading this table with new logic.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_root_id        INTEGER NOT NULL REFERENCES scan_roots(id) ON DELETE CASCADE,

    folder_path         TEXT NOT NULL,          -- full path of the install-unit folder
    catalog              TEXT,                   -- level-1 folder under scan root (e.g. ADOBE)
    subcatalog           TEXT,                   -- level-2 folder (e.g. CONVERTERS)
    depth                INTEGER,

    -- what we found inside this folder
    primary_file_name    TEXT,                   -- best-guess main installer file
    primary_file_type    TEXT,                   -- exe | msi | msix | zip | rar | 7z | iso | unknown
    primary_file_size    INTEGER,
    all_files_json        TEXT,                   -- JSON list of files considered (name, size, type)

    -- PE / installer metadata, when extractable (from exe directly, or from
    -- an exe found inside an extracted archive)
    pe_product_name       TEXT,
    pe_product_version    TEXT,
    pe_file_version       TEXT,
    pe_company_name       TEXT,
    pe_original_filename  TEXT,
    pe_source              TEXT,                  -- direct | extracted_from_archive

    -- archive inspection bookkeeping
    archive_inspected      INTEGER DEFAULT 0,      -- 0/1
    archive_extraction_level TEXT,                 -- listed_only | extracted_ambiguous
    archive_extract_reason  TEXT,                  -- why we escalated to full extraction

    -- classification of the folder itself
    unit_type              TEXT,                   -- install_unit | container | noise | unresolved
    unit_type_reason        TEXT,

    -- fingerprint for incremental re-scan (skip unchanged folders)
    fingerprint             TEXT,                  -- hash of (path, mtime, size, file list)

    first_seen_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at             TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(scan_root_id, folder_path, primary_file_name)
);

CREATE INDEX IF NOT EXISTS idx_raw_candidates_root ON raw_candidates(scan_root_id);
CREATE INDEX IF NOT EXISTS idx_raw_candidates_unit_type ON raw_candidates(unit_type);
CREATE INDEX IF NOT EXISTS idx_raw_candidates_fingerprint ON raw_candidates(fingerprint);

-- ---------------------------------------------------------------------
-- Scan errors: folders the scanner could NOT read at all (permission
-- denied, path-too-long on Windows, etc). Previously these were silently
-- skipped -- that's exactly the kind of failure that can make a whole
-- scan quietly come back empty with no visible cause. Every skip is now
-- recorded here and surfaced in the GUI after a scan.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_root_id    INTEGER NOT NULL REFERENCES scan_roots(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    error_message   TEXT NOT NULL,
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scan_errors_root ON scan_errors(scan_root_id);

-- ---------------------------------------------------------------------
-- Apps: the clean, canonical, user-facing entity. One app can have many
-- variants (versions/editions/languages/architectures).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS apps (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    name                 TEXT NOT NULL,
    name_locked           INTEGER DEFAULT 0,     -- user manually edited -> resolver won't overwrite

    catalog                TEXT,
    catalog_locked          INTEGER DEFAULT 0,
    subcatalog              TEXT,
    subcatalog_locked        INTEGER DEFAULT 0,

    publisher                TEXT,               -- filled by scraper later; placeholder for now
    description                TEXT,
    homepage_url                TEXT,
    icon_path                    TEXT,

    normalized_key                TEXT,           -- lowercase/stripped key used for fuzzy clustering

    -- when the resolver's name/folder-vs-file naming choice is uncertain,
    -- the alternate candidate is kept here so the user can see both and
    -- decide which is better (see resolver/extract.py for how this is chosen)
    alt_name_candidate               TEXT,
    alt_name_source                   TEXT,        -- 'file' | 'folder' -- which source the alt came from


    confidence                     REAL,           -- 0..1, resolver's confidence in this grouping/name
    status                          TEXT DEFAULT 'needs_review',  -- resolved | needs_review | verified | ignored

    resolved_with_settings_version INTEGER REFERENCES settings_version(id),

    scrape_status                   TEXT DEFAULT 'not_scraped',   -- not_scraped | pending | scraped | failed

    created_at                       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_apps_normalized_key ON apps(normalized_key);
CREATE INDEX IF NOT EXISTS idx_apps_status ON apps(status);
CREATE INDEX IF NOT EXISTS idx_apps_catalog ON apps(catalog, subcatalog);

-- ---------------------------------------------------------------------
-- Variants: one per discovered install unit, linked to a canonical app.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS variants (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id               INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    raw_candidate_id       INTEGER REFERENCES raw_candidates(id) ON DELETE SET NULL,

    version                  TEXT,
    version_locked            INTEGER DEFAULT 0,
    edition                    TEXT,               -- Pro, Home, Enterprise, CE, ...
    architecture                 TEXT,             -- x86 | x64 | arm64 | unknown
    language                      TEXT,

    source_path                    TEXT NOT NULL,
    file_type                       TEXT,
    file_size                        INTEGER,

    is_ignored                        INTEGER DEFAULT 0,  -- marked as junk/false-positive by user

    file_name                          TEXT,       -- exact original installer/archive file name
    alt_name_candidate                   TEXT,     -- the other naming source's candidate, for transparency
    name_source                           TEXT,    -- 'file' | 'folder' | 'pe' -- which source won


    confidence                         REAL,
    created_at                          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_variants_app ON variants(app_id);

-- ---------------------------------------------------------------------
-- Tags: free-form, many-to-many (an app can span multiple categories,
-- e.g. a "converter" that's also a "codec pack").
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS app_tags (
    app_id  INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (app_id, tag_id)
);

-- ---------------------------------------------------------------------
-- Scrape cache: populated later by the scraper module (phase 2).
-- Table exists now so the GUI/detail-panel can already show placeholder
-- fields without a schema migration later.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_cache (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id        INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,     -- 'mcp-server-appcatalog' | 'mpkg' | future sources
    raw_response      TEXT,             -- JSON blob as returned by source
    fetched_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scrape_cache_app ON scrape_cache(app_id);

-- ---------------------------------------------------------------------
-- Audit log: every manual edit / merge / split / re-resolve action,
-- so changes in a live session are traceable and (later) undoable.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type    TEXT NOT NULL,   -- app | variant
    entity_id        INTEGER NOT NULL,
    action              TEXT NOT NULL,  -- edit | merge | split | re-resolve | ignore
    detail_json           TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# =============================================================
# Connection + settings accessor (formerly db/connection.py)
# =============================================================

"""
Single-file SQLite connection + settings accessor.

Usage:
    db = Database("catalog.db")
    db.init_schema()
    db.set_setting("confidence_auto_accept", 0.85)
    threshold = db.get_setting("confidence_auto_accept")
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional



class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # connection lifecycle
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON;")
            # WAL mode: lets the GUI read while a background scan/resolve
            # job is writing, which matters since scans are unattended
            # and can run for a long time.
            self._conn.execute("PRAGMA journal_mode = WAL;")
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def init_schema(self):
        conn = self.connect()
        conn.executescript(DDL)
        conn.commit()
        self._run_migrations()
        self._ensure_defaults()

    def _run_migrations(self):
        """
        Additive-only column migrations for existing DBs (CREATE TABLE IF
        NOT EXISTS in schema.py never adds columns to an already-existing
        table). Each entry is (table, column, column_def); ALTER TABLE ADD
        COLUMN is attempted and a 'duplicate column' failure is silently
        ignored, so this is safe to run on every startup.
        """
        conn = self.connect()
        migrations = [
            ("variants", "file_name", "TEXT"),
            ("variants", "alt_name_candidate", "TEXT"),
            ("variants", "name_source", "TEXT"),
            ("apps", "alt_name_candidate", "TEXT"),
            ("apps", "alt_name_source", "TEXT"),
            # scraper (checkpoint 12): metadata enrichment from Winget/Chocolatey.
            # publisher/description/homepage_url/scrape_status already existed
            # (added in the original schema as placeholders) and are reused
            # directly rather than duplicated under the scraper report's field
            # names ("company"/"url"/"scrap_status").
            ("apps", "winget_id", "TEXT"),
            ("apps", "choco_id", "TEXT"),
            ("apps", "manifest_name", "TEXT"),      # display name as seen in the manifest, for future lookups
            ("apps", "alt_source_name", "TEXT"),    # name prior to an auto-rename, kept for transparency/undo
            ("apps", "latest_version", "TEXT"),
            ("apps", "last_scraped", "TEXT"),
            # checkpoint 14: winget show fallback for real description/
            # license (the bare manifest has neither -- see manifest.py).
            ("apps", "license", "TEXT"),
        ]
        for table, column, coldef in migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
                conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    def _ensure_defaults(self):
        # Only seed defaults for keys that don't already exist, so
        # re-running init_schema on an existing DB never clobbers
        # a user's live-session settings.
        from config import DEFAULT_SETTINGS

        for key, value in DEFAULT_SETTINGS.items():
            if self.get_setting(key, sentinel=True) is _MISSING:
                self.set_setting(key, value, bump_version=False, note="initial default")

        self._merge_new_keyword_defaults()

        # ensure at least one settings_version row exists
        conn = self.connect()
        row = conn.execute("SELECT COUNT(*) AS c FROM settings_version").fetchone()
        if row["c"] == 0:
            conn.execute(
                "INSERT INTO settings_version (note) VALUES (?)",
                ("initial",),
            )
            conn.commit()

    def _merge_new_keyword_defaults(self):
        """
        Unlike _ensure_defaults (which only seeds a key the FIRST time it's
        ever seen), this additively unions newly-introduced default list
        entries into a small set of keyword-list settings that already
        exist in an older DB -- e.g. an existing catalog.db already has an
        `ignore_filename_words` list saved from a previous session, so a
        code update that adds "isdel" to the DEFAULT_SETTINGS list would
        otherwise never reach it. Preserves the user's own custom entries
        and ordering; only appends genuinely-new default words that aren't
        already present (case-insensitive). Never removes anything.
        """
        from config import DEFAULT_SETTINGS

        additive_keys = [
            "ignore_filename_words",
            "noise_folder_keywords",
            "noise_short_only_keywords",
            "edition_keywords",
        ]
        for key in additive_keys:
            default_list = DEFAULT_SETTINGS.get(key)
            if not isinstance(default_list, list):
                continue
            current = self.get_setting(key, sentinel=True)
            if current is _MISSING or not isinstance(current, list):
                continue  # _ensure_defaults already handled the missing case
            current_lower = {str(v).lower() for v in current}
            additions = [v for v in default_list if str(v).lower() not in current_lower]
            if additions:
                self.set_setting(key, current + additions, bump_version=False,
                                  note="merged new keyword defaults")

    # ------------------------------------------------------------------
    # settings (live, JSON-encoded values, versioned)
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None, sentinel: bool = False) -> Any:
        conn = self.connect()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return _MISSING if sentinel else default
        return json.loads(row["value"])

    def get_all_settings(self) -> dict:
        conn = self.connect()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def set_setting(self, key: str, value: Any, bump_version: bool = True, note: str = None):
        """
        Write a setting immediately (live-session, no restart needed).

        bump_version=True creates a new settings_version row, which future
        resolve runs will stamp onto every app/variant they touch -- this
        is how "resolved before rule change X" filtering works.
        """
        conn = self.connect()
        conn.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = datetime('now')""",
            (key, json.dumps(value)),
        )
        if bump_version:
            conn.execute(
                "INSERT INTO settings_version (note) VALUES (?)",
                (note or f"changed {key}",),
            )
        conn.commit()

    def current_settings_version(self) -> int:
        conn = self.connect()
        row = conn.execute(
            "SELECT id FROM settings_version ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else 1

    # ------------------------------------------------------------------
    # GUI helpers (added for compatibility with split GUI code)
    # ------------------------------------------------------------------
    def get_versions_for_app(self, app_id: int) -> list[dict]:
        """
        Return all variants (versions) belonging to a given app ID as a list of dicts.
        """
        conn = self.connect()
        rows = conn.execute("SELECT * FROM variants WHERE app_id = ?", (app_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_app(self, app_id: int) -> None:
        """
        Delete an app and all its associated variants.
        """
        conn = self.connect()
        conn.execute("DELETE FROM variants WHERE app_id = ?", (app_id,))
        conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
        conn.commit()

class _MissingType:
    def __repr__(self):
        return "<MISSING>"


_MISSING = _MissingType()