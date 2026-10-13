#!/usr/bin/env python3
"""
merge_catalogs.py -- merge one catalog.db into another.

Standalone utility. NOT part of the App Catalog app itself.

Built for the workflow: scan + organize one category in a small scratch
DB ("temp"), then fold that result into the big "master" catalog
WITHOUT re-scanning or re-organizing -- the work you already did in
temp comes over as-is.

Usage:
    python merge_catalogs.py master.db temp.db
    python merge_catalogs.py master.db temp.db --yes        # no prompt
    python merge_catalogs.py master.db temp.db --dry-run    # report only

What gets merged (source -> master):
    scan_roots        reused by path when master already has one
    raw_candidates    reused when an equivalent row already exists
                      (same scan root + folder_path + primary_file_name)
    scan_errors       deduped by (root, path, message)
    apps              ALWAYS added as NEW rows (fresh IDs assigned)
    variants          added with app_id / raw_candidate_id remapped
    tags              reused by name
    app_tags          remapped, deduped
    scrape_cache      remapped by app
    audit_log         entity_id remapped where possible, otherwise skipped

What is NOT touched:
    settings / settings_version        (master's live config wins)
    winget / winutil cache files       (plain files on disk, not DB rows)

Safety:
    - A timestamped backup of master is written first (unless --no-backup).
    - Everything runs inside one transaction: a mid-merge failure rolls
      back completely, leaving master exactly as it was.
    - Foreign keys are disabled during the import and re-enabled after;
      referential integrity is guaranteed by construction (parents are
      inserted before dependents, and IDs are remapped in the same pass).
    - Existing apps in master are NEVER modified or merged -- only new
      rows are added. If you want deduplication afterwards, use the
      app's own Organize -> Duplicates tab.
"""

import argparse
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# small schema helpers
# ---------------------------------------------------------------------------

def _cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _insert(conn, table, row_dict, col_names):
    placeholders = ",".join("?" for _ in col_names)
    col_sql = ",".join(col_names)
    cur = conn.execute(
        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
        [row_dict.get(c) for c in col_names],
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# the merge itself
# ---------------------------------------------------------------------------

def merge(master_path, source_path, dry_run=False):
    # isolation_level=None -> we manage BEGIN/COMMIT ourselves, and
    # PRAGMA foreign_keys=OFF actually takes effect (it's a no-op inside
    # an open transaction).
    master = sqlite3.connect(str(master_path), isolation_level=None)
    master.row_factory = sqlite3.Row
    master.execute("PRAGMA foreign_keys = OFF")
    master.execute("BEGIN")

    source = sqlite3.connect(str(source_path))
    source.row_factory = sqlite3.Row

    stats = {
        "scan_roots_reused":      0,
        "scan_roots_added":       0,
        "raw_candidates_reused":  0,
        "raw_candidates_added":   0,
        "apps_added":             0,
        "variants_added":         0,
        "variants_skipped":       0,
        "tags_reused":            0,
        "tags_added":             0,
        "app_tags_added":         0,
        "scan_errors_added":      0,
        "scrape_cache_added":     0,
        "audit_log_added":        0,
    }

    # Cache the intersection of columns per table, so we only try to copy
    # columns that exist in BOTH databases. Protects against the source
    # being from a slightly older/newer version of the app.
    _shared_cache = {}
    def shared(table):
        if table not in _shared_cache:
            src = set(_cols(source, table))
            dst = set(_cols(master, table))
            _shared_cache[table] = [c for c in _cols(source, table)
                                    if c in dst and c != "id"]
        return _shared_cache[table]

    # ---------- scan_roots ----------
    root_map = {}   # source_id -> master_id
    for r in source.execute("SELECT * FROM scan_roots").fetchall():
        row = dict(r)
        existing = master.execute(
            "SELECT id FROM scan_roots WHERE path = ?", (row["path"],)
        ).fetchone()
        if existing:
            root_map[row["id"]] = existing["id"]
            stats["scan_roots_reused"] += 1
        else:
            new_id = _insert(master, "scan_roots", row, shared("scan_roots"))
            root_map[row["id"]] = new_id
            stats["scan_roots_added"] += 1

    # ---------- raw_candidates ----------
    # Unique constraint is (scan_root_id, folder_path, primary_file_name).
    # If master already has that exact row (e.g. from a previous scan of
    # the same folder), reuse its id so the variants we're about to
    # insert point at the master's row rather than creating a duplicate.
    rc_map = {}     # source_id -> master_id
    for r in source.execute("SELECT * FROM raw_candidates").fetchall():
        row = dict(r)
        new_root = root_map.get(row["scan_root_id"])
        if new_root is None:
            continue    # orphan -- skip rather than insert something broken
        existing = master.execute(
            "SELECT id FROM raw_candidates "
            "WHERE scan_root_id = ? AND folder_path = ? "
            "AND primary_file_name IS ?",
            (new_root, row["folder_path"], row["primary_file_name"]),
        ).fetchone()
        if existing:
            rc_map[row["id"]] = existing["id"]
            stats["raw_candidates_reused"] += 1
            continue
        row["scan_root_id"] = new_root
        new_id = _insert(master, "raw_candidates", row, shared("raw_candidates"))
        rc_map[row["id"]] = new_id
        stats["raw_candidates_added"] += 1

    # ---------- apps ----------
    app_map = {}    # source_id -> master_id
    master_app_uids = {r["app_uid"] for r in master.execute(
        "SELECT app_uid FROM apps WHERE app_uid IS NOT NULL").fetchall()}
    for a in source.execute("SELECT * FROM apps").fetchall():
        row = dict(a)
        # uid collision (astronomically unlikely, but be safe): fresh one.
        if row.get("app_uid") and row["app_uid"] in master_app_uids:
            row["app_uid"] = str(uuid.uuid4())
            master_app_uids.add(row["app_uid"])
        # resolved_with_settings_version points at a settings_version row
        # that only exists in the SOURCE DB -- null it rather than leave
        # it pointing at some unrelated master row.
        if "resolved_with_settings_version" in row:
            row["resolved_with_settings_version"] = None
        new_id = _insert(master, "apps", row, shared("apps"))
        app_map[row["id"]] = new_id
        stats["apps_added"] += 1

    # ---------- variants ----------
    var_map = {}    # source_id -> master_id
    master_variant_uids = {r["variant_uid"] for r in master.execute(
        "SELECT variant_uid FROM variants WHERE variant_uid IS NOT NULL").fetchall()}
    for v in source.execute("SELECT * FROM variants").fetchall():
        row = dict(v)
        new_app = app_map.get(row["app_id"])
        if new_app is None:
            stats["variants_skipped"] += 1
            continue
        row["app_id"] = new_app
        old_rc = row.get("raw_candidate_id")
        if old_rc is not None:
            row["raw_candidate_id"] = rc_map.get(old_rc)
            # If it didn't map, this variant now has no raw_candidate.
            # That's a legal state (monitor-created variants have NULL
            # too), so keep the row rather than skipping it.
        if row.get("variant_uid") and row["variant_uid"] in master_variant_uids:
            row["variant_uid"] = str(uuid.uuid4())
            master_variant_uids.add(row["variant_uid"])
        new_id = _insert(master, "variants", row, shared("variants"))
        var_map[row["id"]] = new_id
        stats["variants_added"] += 1

    # ---------- tags ----------
    tag_map = {}    # source_id -> master_id
    for t in source.execute("SELECT * FROM tags").fetchall():
        row = dict(t)
        existing = master.execute(
            "SELECT id FROM tags WHERE name = ?", (row["name"],)
        ).fetchone()
        if existing:
            tag_map[row["id"]] = existing["id"]
            stats["tags_reused"] += 1
        else:
            cur = master.execute(
                "INSERT INTO tags (name) VALUES (?)", (row["name"],))
            tag_map[row["id"]] = cur.lastrowid
            stats["tags_added"] += 1

    # ---------- app_tags ----------
    for at in source.execute("SELECT * FROM app_tags").fetchall():
        new_app = app_map.get(at["app_id"])
        new_tag = tag_map.get(at["tag_id"])
        if new_app is None or new_tag is None:
            continue
        cur = master.execute(
            "INSERT OR IGNORE INTO app_tags (app_id, tag_id) VALUES (?, ?)",
            (new_app, new_tag),
        )
        if cur.rowcount:
            stats["app_tags_added"] += 1

    # ---------- scan_errors ----------
    if _table_exists(source, "scan_errors"):
        for e in source.execute("SELECT * FROM scan_errors").fetchall():
            row = dict(e)
            new_root = root_map.get(row["scan_root_id"])
            if new_root is None:
                continue
            dup = master.execute(
                "SELECT 1 FROM scan_errors "
                "WHERE scan_root_id = ? AND path = ? AND error_message = ?",
                (new_root, row["path"], row["error_message"]),
            ).fetchone()
            if dup:
                continue
            row["scan_root_id"] = new_root
            _insert(master, "scan_errors", row, shared("scan_errors"))
            stats["scan_errors_added"] += 1

    # ---------- scrape_cache ----------
    if _table_exists(source, "scrape_cache"):
        for s in source.execute("SELECT * FROM scrape_cache").fetchall():
            row = dict(s)
            new_app = app_map.get(row["app_id"])
            if new_app is None:
                continue
            row["app_id"] = new_app
            _insert(master, "scrape_cache", row, shared("scrape_cache"))
            stats["scrape_cache_added"] += 1

    # ---------- audit_log ----------
    # Copied with entity_id remapped where possible. Anything we can't
    # map (e.g. an audit row referring to a source-only settings_version)
    # is skipped rather than written with a dangling id.
    if _table_exists(source, "audit_log"):
        for a in source.execute("SELECT * FROM audit_log").fetchall():
            row = dict(a)
            et, eid = row.get("entity_type"), row.get("entity_id")
            if et == "app":
                new_eid = app_map.get(eid)
            elif et == "variant":
                new_eid = var_map.get(eid)
            elif et == "scan_root":
                new_eid = root_map.get(eid)
            else:
                new_eid = eid
            if new_eid is None:
                continue
            row["entity_id"] = new_eid
            _insert(master, "audit_log", row, shared("audit_log"))
            stats["audit_log_added"] += 1

    # ---------- commit / rollback ----------
    if dry_run:
        master.execute("ROLLBACK")
    else:
        master.execute("COMMIT")

    master.execute("PRAGMA foreign_keys = ON")
    master.close()
    source.close()
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _looks_like_catalog(path):
    try:
        c = sqlite3.connect(str(path))
        c.execute("SELECT 1 FROM apps LIMIT 1").fetchone()
        c.close()
        return True, None
    except sqlite3.Error as e:
        return False, str(e)


def main():
    p = argparse.ArgumentParser(
        description="Merge one catalog.db (source/temp) into another "
                    "(master). Standalone utility, not part of the app.",
    )
    p.add_argument("master", help="the master catalog.db to merge INTO")
    p.add_argument("source", help="the temp/scratch catalog.db to merge FROM")
    p.add_argument("--yes", "-y", action="store_true",
                   help="skip the interactive confirmation prompt")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be added, then roll back")
    p.add_argument("--no-backup", action="store_true",
                   help="skip the timestamped backup of master (not advised)")
    args = p.parse_args()

    master_path = Path(args.master).resolve()
    source_path = Path(args.source).resolve()

    if not master_path.is_file():
        print(f"ERROR: master DB not found: {master_path}")
        return 2
    if not source_path.is_file():
        print(f"ERROR: source DB not found: {source_path}")
        return 2
    if master_path == source_path:
        print("ERROR: master and source are the same file.")
        return 2

    for label, path in (("master", master_path), ("source", source_path)):
        ok, err = _looks_like_catalog(path)
        if not ok:
            print(f"ERROR: '{label}' doesn't look like a catalog DB: {err}")
            return 2

    print(f"Master : {master_path}")
    print(f"Source : {source_path}")

    if not args.yes and not args.dry_run:
        print()
        print("All apps/variants from source will be ADDED to master as new rows.")
        print("Existing master rows are NEVER touched or merged.")
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    backup_path = None
    if not args.dry_run and not args.no_backup:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = master_path.with_suffix(f".backup-{ts}.db")
        shutil.copy2(master_path, backup_path)
        print(f"Backup : {backup_path}")

    try:
        stats = merge(str(master_path), str(source_path), dry_run=args.dry_run)
    except Exception as e:
        print(f"\nMERGE FAILED: {e}")
        print("Master was NOT modified (transaction rolled back).")
        if backup_path:
            print(f"(A pre-run backup still exists at {backup_path}.)")
        return 3

    print()
    title = "Merge summary" + (" (DRY RUN - nothing written)" if args.dry_run else "")
    print(f"=== {title} ===")
    for k in ("scan_roots_added", "scan_roots_reused",
              "raw_candidates_added", "raw_candidates_reused",
              "apps_added",
              "variants_added", "variants_skipped",
              "tags_added", "tags_reused", "app_tags_added",
              "scan_errors_added", "scrape_cache_added", "audit_log_added"):
        print(f"  {k:24s} {stats[k]}")

    if args.dry_run:
        print("\nDry run: no changes written.")
    else:
        print("\nDone. Open the master DB in the App Catalog to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())