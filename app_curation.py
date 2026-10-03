"""
app_curation.py -- protecting manual work from re-resolves / rescans, plus
the manual-curation actions that go with it (checkpoint 32).

THE BUG THIS FIXES
------------------
Resolve matched an existing app ONLY by ``apps.normalized_key`` computed
from the folder/file names, and ``_upsert_variant`` then overwrote the
variant's ``app_id`` with whatever app that gave.  So whenever a manual
action or the scraper changed an app's name (and therefore its
``normalized_key``), the next rescan / "Resolve all":

  * could not find that app any more -> created a brand-new duplicate app
    with the auto-derived name,
  * moved the variants into the duplicate, leaving the carefully renamed /
    scraped app EMPTY,
  * and silently undid manual merges, moves and splits the same way.

THE FIX (all additive; old catalog.db files keep working)
---------------------------------------------------------
1. **Aliases**  ``app_aliases(normalized_key -> app_id)``.  A trigger records
   an app's old key whenever its ``normalized_key`` changes (manual rename,
   scraper auto-rename, re-resolve acceptance ... every writer), and
   ``merge_apps`` aliases the absorbed app.  Resolve looks an unknown
   cluster key up in the aliases before creating an app.
2. **Pinning**  A variant is *pinned* to its current app when the app is
   protected (any lock, or verified) or the variant was manually moved /
   merged / split (``variants.app_pinned``).  Pinned variants are never
   re-clustered; resolve only refreshes their own fields.
3. **Adoption**  A variant created without a raw candidate (monitor, manual
   add) or left with a stale one (after Reorganize moved its folder) is
   re-linked to the fresh raw candidate describing the same folder + file
   instead of getting a duplicate variant.
4. **Repair**  ``find_empty_app_repairs`` / ``apply_empty_app_repairs`` fix a
   catalog that already has the duplicate-with-empty-app damage.
5. **Backup**  ``create_backup`` snapshots catalog.db before a bulk
   re-resolve.

MANUAL CURATION ADDED HERE
--------------------------
``add_app_manually``       add an app / variant without scanning a root
``override_variant_entry`` use an installer that lives outside the variant's
                           folder (re-points the unit to the common parent)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger("appcatalog.curation")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def norm_path(p: Optional[str]) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""


def _norm_file(name: Optional[str]) -> str:
    return (name or "").replace("\\", "/").lower()


def _is_inside(path: str, root: str) -> bool:
    p, r = norm_path(path), norm_path(root)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


# ---------------------------------------------------------------------------
# schema (additive, idempotent)
# ---------------------------------------------------------------------------
def ensure_curation_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS app_aliases (
               normalized_key TEXT PRIMARY KEY,
               app_id         INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
               created_at     TEXT NOT NULL DEFAULT (datetime('now'))
           )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_aliases_app ON app_aliases(app_id)")

    added = False
    try:
        conn.execute("ALTER TABLE variants ADD COLUMN app_pinned INTEGER DEFAULT 0")
        added = True
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise

    # Remember an app's previous key whenever it changes -- covers the
    # scraper's auto-rename, manual rename, re-resolve acceptance, anything.
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_apps_key_alias
           AFTER UPDATE OF normalized_key ON apps
           WHEN OLD.normalized_key IS NOT NULL AND OLD.normalized_key != ''
                AND OLD.normalized_key IS NOT NEW.normalized_key
           BEGIN
             INSERT OR REPLACE INTO app_aliases (normalized_key, app_id)
             VALUES (OLD.normalized_key, NEW.id);
           END""")

    if added:
        # Existing database: variants that were manually moved / split, and
        # apps that absorbed a merge, are recoverable from the audit log.
        conn.execute(
            """UPDATE variants SET app_pinned = 1 WHERE id IN
               (SELECT entity_id FROM audit_log
                 WHERE entity_type = 'variant' AND action IN ('move', 'split'))""")
        conn.execute(
            """UPDATE variants SET app_pinned = 1 WHERE app_id IN
               (SELECT entity_id FROM audit_log
                 WHERE entity_type = 'app' AND action = 'merge')""")
        log.info("curation: backfilled variant pins from the audit log")
    conn.commit()


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
def create_backup(db_path: str, label: str = "manual", keep: int = 10,
                  min_interval_s: int = 0) -> Optional[str]:
    """Snapshot the catalog next to the DB (``backups/``).  Returns the new
    file, or None if skipped / failed (a backup problem never blocks work).
    ``min_interval_s`` avoids spamming copies for repeated quick actions."""
    try:
        folder = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
        os.makedirs(folder, exist_ok=True)
        stem = os.path.splitext(os.path.basename(db_path))[0]
        prefix = f"{stem}-{label}-"
        existing = sorted(
            (f for f in os.listdir(folder) if f.startswith(prefix) and f.endswith(".db")),
            reverse=True)
        if min_interval_s and existing:
            newest = os.path.join(folder, existing[0])
            if time.time() - os.path.getmtime(newest) < min_interval_s:
                return None
        dest = os.path.join(folder, f"{prefix}{datetime.now():%Y%m%d-%H%M%S}.db")
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        for old in existing[max(0, keep - 1):]:
            try:
                os.remove(os.path.join(folder, old))
            except OSError:
                pass
        log.info("catalog backup written: %s", dest)
        return dest
    except Exception as e:
        log.warning("catalog backup failed (%s): %s", label, e)
        return None


# ---------------------------------------------------------------------------
# resolver support: pinning, adoption, aliases
# ---------------------------------------------------------------------------
def load_pin_map(conn) -> dict[int, int]:
    """raw_candidate_id -> app_id for every variant that must stay where it is."""
    rows = conn.execute(
        """SELECT v.raw_candidate_id AS rid, v.app_id AS app_id
             FROM variants v JOIN apps a ON a.id = v.app_id
            WHERE v.raw_candidate_id IS NOT NULL
              AND (COALESCE(v.app_pinned, 0) = 1
                   OR COALESCE(a.name_locked, 0) = 1
                   OR COALESCE(a.catalog_locked, 0) = 1
                   OR COALESCE(a.subcatalog_locked, 0) = 1
                   OR a.status = 'verified')""").fetchall()
    return {r["rid"]: r["app_id"] for r in rows}


def adopt_orphan_variants(conn, raw_rows) -> tuple[int, set]:
    """Re-link variants that have no raw candidate (monitor / manual add) or a
    stale one (Reorganize moved the folder) to the fresh raw candidate that
    describes the same folder + file, so a rescan doesn't create duplicates.

    Returns (adopted count, ids of stale raw rows that were deleted).  A
    variant's OLD raw row is deleted when its folder no longer exists (it is a
    ghost of the moved folder -- the scanner would drop it on a rescan of that
    root anyway); the caller must skip those rows in the current resolve."""
    linked = {r[0] for r in conn.execute(
        "SELECT raw_candidate_id FROM variants WHERE raw_candidate_id IS NOT NULL")}
    wanting: dict[tuple, list[int]] = {}
    for v in conn.execute(
            """SELECT v.id, v.source_path, v.file_name, v.raw_candidate_id,
                      rc.folder_path AS rc_folder
                 FROM variants v LEFT JOIN raw_candidates rc ON rc.id = v.raw_candidate_id"""):
        if v["raw_candidate_id"] is not None and v["rc_folder"] is not None \
                and norm_path(v["rc_folder"]) == norm_path(v["source_path"]):
            continue                                   # properly linked already
        wanting.setdefault((norm_path(v["source_path"]), _norm_file(v["file_name"])), []).append(
            (v["id"], v["raw_candidate_id"], v["rc_folder"]))
    if not wanting:
        return 0, set()
    adopted = 0
    stale_deleted: set = set()
    for row in raw_rows:
        if row["id"] in linked:
            continue
        ids = wanting.get((norm_path(row["folder_path"]), _norm_file(row["primary_file_name"])))
        if ids:
            vid, old_rid, old_folder = ids.pop(0)
            conn.execute("UPDATE variants SET raw_candidate_id = ? WHERE id = ?", (row["id"], vid))
            linked.add(row["id"])
            adopted += 1
            if old_rid is not None and old_rid != row["id"] and old_folder and not os.path.isdir(old_folder):
                still_used = conn.execute("SELECT 1 FROM variants WHERE raw_candidate_id = ? LIMIT 1",
                                          (old_rid,)).fetchone()
                if not still_used:
                    conn.execute("DELETE FROM raw_candidates WHERE id = ?", (old_rid,))
                    stale_deleted.add(old_rid)
    return adopted, stale_deleted


def find_app_by_alias(conn, key: str):
    return conn.execute(
        "SELECT a.* FROM app_aliases al JOIN apps a ON a.id = al.app_id WHERE al.normalized_key = ?",
        (key,)).fetchone()


def learn_alias(conn, key: str, app_id: int) -> None:
    """Remember that auto-derived name `key` belongs to `app_id` (only if no
    other app legitimately owns that key)."""
    if not key:
        return
    owner = conn.execute("SELECT id FROM apps WHERE normalized_key = ?", (key,)).fetchone()
    if owner is not None:
        return
    conn.execute("INSERT OR REPLACE INTO app_aliases (normalized_key, app_id) VALUES (?, ?)",
                 (key, app_id))


def alias_absorbed_app(conn, source_app_id: int, target_app_id: int) -> None:
    """merge_apps support: the absorbed app's names must keep pointing at the
    survivor, otherwise the next rescan re-creates the absorbed app."""
    src = conn.execute("SELECT normalized_key FROM apps WHERE id = ?", (source_app_id,)).fetchone()
    conn.execute("UPDATE app_aliases SET app_id = ? WHERE app_id = ?", (target_app_id, source_app_id))
    if src and src["normalized_key"]:
        conn.execute("INSERT OR REPLACE INTO app_aliases (normalized_key, app_id) VALUES (?, ?)",
                     (src["normalized_key"], target_app_id))


def remove_empty_unprotected_apps(conn) -> int:
    """Apps left with no variants that carry no manual or scraped work are
    pure clutter -- remove them.  Protected / scraped / described ones stay
    (they are what the repair tool below rescues)."""
    cur = conn.execute(
        """DELETE FROM apps
            WHERE id NOT IN (SELECT DISTINCT app_id FROM variants)
              AND COALESCE(name_locked, 0) = 0 AND COALESCE(catalog_locked, 0) = 0
              AND COALESCE(subcatalog_locked, 0) = 0
              AND COALESCE(status, '') != 'verified'
              AND COALESCE(scrape_status, 'not_scraped') != 'scraped'
              AND description IS NULL AND winget_id IS NULL AND choco_id IS NULL""")
    return cur.rowcount


# ---------------------------------------------------------------------------
# repair: apps that already ended up empty
# ---------------------------------------------------------------------------
def _has_manual_work(a) -> bool:
    return bool(a["name_locked"] or a["catalog_locked"] or a["subcatalog_locked"]
                or a["status"] == "verified" or a["scrape_status"] == "scraped"
                or a["description"] or a["winget_id"] or a["choco_id"])


def find_empty_app_repairs(db, min_score: int = 70) -> list[dict]:
    """For every app with zero variants, propose what to do:

      merge   an app that has variants and a similar name in the same catalog
              looks like the auto-created duplicate -> its variants move INTO
              the empty app (which keeps the manual name / scraped data)
      delete  nothing worth keeping and no counterpart -> remove the empty row
      review  has manual/scraped data but no plausible counterpart -> leave

    Returns dicts: empty_id, empty_name, catalog, subcatalog, action, sibling_id,
    sibling_name, sibling_variants, score, has_manual."""
    from rapidfuzz import fuzz
    conn = db.connect()
    empties = conn.execute(
        """SELECT a.* FROM apps a LEFT JOIN variants v ON v.app_id = a.id
            WHERE v.id IS NULL ORDER BY a.name COLLATE NOCASE""").fetchall()
    if not empties:
        return []
    donors = conn.execute(
        """SELECT a.*, COUNT(v.id) AS vcount FROM apps a JOIN variants v ON v.app_id = a.id
            GROUP BY a.id""").fetchall()
    by_cat: dict[str, list] = {}
    for d in donors:
        by_cat.setdefault((d["catalog"] or "").lower(), []).append(d)

    out = []
    used_donors: set[int] = set()
    for e in empties:
        manual = _has_manual_work(e)
        item = {"empty_id": e["id"], "empty_name": e["name"], "catalog": e["catalog"],
                "subcatalog": e["subcatalog"], "has_manual": manual, "action": "delete",
                "sibling_id": None, "sibling_name": None, "sibling_variants": 0, "score": 0}
        ekey = e["normalized_key"] or (e["name"] or "").lower()
        best, best_score = None, 0
        for d in by_cat.get((e["catalog"] or "").lower(), []):
            if d["id"] in used_donors:
                continue
            dkey = d["normalized_key"] or (d["name"] or "").lower()
            score = max(fuzz.token_sort_ratio(ekey, dkey),
                        fuzz.token_set_ratio(ekey, dkey) - 5,
                        fuzz.partial_ratio(ekey, dkey) - 12)
            if score > best_score:
                best, best_score = d, score
        if best is not None and best_score >= min_score:
            item.update(action="merge", sibling_id=best["id"], sibling_name=best["name"],
                        sibling_variants=best["vcount"], score=int(best_score))
            used_donors.add(best["id"])
        elif manual:
            item["action"] = "review"
        out.append(item)
    return out


def apply_empty_app_repairs(db, items: list[dict]) -> dict:
    """Apply chosen proposals from find_empty_app_repairs()."""
    conn = db.connect()
    done = {"merged": 0, "deleted": 0, "skipped": 0}
    for it in items:
        e = conn.execute("SELECT * FROM apps WHERE id = ?", (it["empty_id"],)).fetchone()
        if e is None:
            done["skipped"] += 1
            continue
        has_variants = conn.execute("SELECT 1 FROM variants WHERE app_id = ? LIMIT 1", (e["id"],)).fetchone()
        if has_variants:
            done["skipped"] += 1
            continue
        if it["action"] == "merge" and it.get("sibling_id"):
            s = conn.execute("SELECT * FROM apps WHERE id = ?", (it["sibling_id"],)).fetchone()
            if s is None:
                done["skipped"] += 1
                continue
            conn.execute("UPDATE variants SET app_id = ?, app_pinned = 1, updated_at = datetime('now') WHERE app_id = ?",
                         (e["id"], s["id"]))
            alias_absorbed_app(conn, s["id"], e["id"])
            if not e["catalog"] and s["catalog"]:
                conn.execute("UPDATE apps SET catalog = ? WHERE id = ?", (s["catalog"], e["id"]))
            if not e["subcatalog"] and s["subcatalog"]:
                conn.execute("UPDATE apps SET subcatalog = ? WHERE id = ?", (s["subcatalog"], e["id"]))
            # The donor is an auto-created duplicate; drop it unless it carries work of its own.
            if not _has_manual_work(s):
                conn.execute("DELETE FROM apps WHERE id = ?", (s["id"],))
            conn.execute("INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
                         ("app", e["id"], "repair_empty_merge",
                          json.dumps({"absorbed_app_id": s["id"], "absorbed_name": s["name"]})))
            done["merged"] += 1
        elif it["action"] == "delete":
            conn.execute("DELETE FROM apps WHERE id = ?", (e["id"],))
            done["deleted"] += 1
        else:
            done["skipped"] += 1
    conn.commit()
    return done


# ---------------------------------------------------------------------------
# add an app directly (no scan root)
# ---------------------------------------------------------------------------
def suggest_app_fields(db, installer_path: str, unit_folder: Optional[str] = None) -> dict:
    """Prefill for the Add-app dialog, using the same extractor the resolver uses."""
    from resolver import extract_fields
    unit_folder = unit_folder or os.path.dirname(installer_path)
    folder_name = os.path.basename(unit_folder.rstrip("\\/")) or ""
    parent = os.path.basename(os.path.dirname(unit_folder.rstrip("\\/"))) or None
    try:
        f = extract_fields(folder_name=folder_name, primary_file_name=os.path.basename(installer_path),
                           pe_product_name=None, pe_product_version=None, pe_file_version=None,
                           settings=db.get_all_settings(), parent_folder_name=parent, folder_depth=3)
        return {"name": f.clean_name or folder_name, "version": f.version or "",
                "edition": f.edition or "", "architecture": f.architecture or "",
                "language": f.language or ""}
    except Exception:
        log.exception("suggest_app_fields failed")
        return {"name": folder_name, "version": "", "edition": "", "architecture": "", "language": ""}


def find_existing_app(db, name: str):
    """Existing app a typed name would collide with (by key or alias), or None."""
    from resolver import normalize_key
    key = normalize_key(name)
    conn = db.connect()
    return (conn.execute("SELECT * FROM apps WHERE normalized_key = ?", (key,)).fetchone()
            or find_app_by_alias(conn, key))


def find_variant_at(db, folder: str, file_name: str):
    conn = db.connect()
    for v in conn.execute("SELECT v.*, a.name AS app_name FROM variants v JOIN apps a ON a.id = v.app_id "
                          "WHERE v.file_name IS NOT NULL"):
        if norm_path(v["source_path"]) == norm_path(folder) and _norm_file(v["file_name"]) == _norm_file(file_name):
            return v
    return None


def add_app_manually(db, *, name: str, installer_path: str, unit_folder: Optional[str] = None,
                     catalog: Optional[str] = None, subcatalog: Optional[str] = None,
                     version: Optional[str] = None, edition: Optional[str] = None,
                     architecture: Optional[str] = None, language: Optional[str] = None,
                     description: Optional[str] = None, add_to_app_id: Optional[int] = None) -> dict:
    """Create an app (or a new variant of `add_to_app_id`) straight from an
    installer file -- no scan root involved.

    The variant is created like a Monitor attach: no raw candidate, file
    locked, name/catalog/subcatalog locked and the app verified, so nothing
    automatic later renames or regroups it.  If its folder sits inside a scan
    root, a later scan ADOPTS it (see adopt_orphan_variants) instead of
    creating a duplicate."""
    from resolver import normalize_key, _ensure_tag
    name = (name or "").strip()
    if not name:
        raise ValueError("Please enter an app name.")
    if not installer_path or not os.path.isfile(installer_path):
        raise ValueError(f"Installer file not found:\n{installer_path}")
    unit_folder = os.path.normpath(unit_folder or os.path.dirname(installer_path))
    if not os.path.isdir(unit_folder):
        raise ValueError(f"App folder not found:\n{unit_folder}")
    rel = os.path.relpath(installer_path, unit_folder)
    if rel.startswith("..") or os.path.isabs(rel):
        raise ValueError("The installer must be inside the app folder (or one of its sub-folders).")
    file_name = rel.replace(os.sep, "/")

    dup = find_variant_at(db, unit_folder, file_name)
    if dup is not None:
        raise ValueError(f"This file is already in the catalog, under “{dup['app_name']}”.")

    conn = db.connect()
    created_app = False
    if add_to_app_id is not None:
        app_id = add_to_app_id
    else:
        cur = conn.execute(
            """INSERT INTO apps (name, name_locked, catalog, catalog_locked, subcatalog, subcatalog_locked,
                                  normalized_key, confidence, status, description, updated_at)
               VALUES (?, 1, ?, ?, ?, ?, ?, 1.0, 'verified', ?, datetime('now'))""",
            (name, catalog or None, 1 if catalog else 0, subcatalog or None, 1 if subcatalog else 0,
             normalize_key(name), description or None))
        app_id = cur.lastrowid
        created_app = True
        for tag in (catalog, subcatalog):
            if tag:
                tid = _ensure_tag(conn, tag)
                conn.execute("INSERT OR IGNORE INTO app_tags (app_id, tag_id) VALUES (?, ?)", (app_id, tid))

    ext = os.path.splitext(installer_path)[1].lstrip(".").lower()
    cur = conn.execute(
        """INSERT INTO variants (app_id, version, edition, architecture, language, source_path,
                                  file_type, file_size, confidence, updated_at, file_name,
                                  name_source, file_locked, app_pinned)
           VALUES (?,?,?,?,?,?,?,?,1.0, datetime('now'), ?, 'manual', 1, 1)""",
        (app_id, version or None, edition or None, architecture or None, language or None,
         unit_folder, ext, os.path.getsize(installer_path), file_name))
    variant_id = cur.lastrowid
    conn.execute("INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
                 ("variant", variant_id, "add_manual",
                  json.dumps({"app_id": app_id, "name": name, "installer": installer_path})))
    conn.commit()
    return {"app_id": app_id, "variant_id": variant_id, "created_app": created_app}


def split_variant_with_details(db, variant_id: int, *, name: str, installer_path: Optional[str] = None,
                               unit_folder: Optional[str] = None, catalog: Optional[str] = None,
                               subcatalog: Optional[str] = None, version: Optional[str] = None,
                               edition: Optional[str] = None, architecture: Optional[str] = None,
                               language: Optional[str] = None, target_app_id: Optional[int] = None) -> dict:
    """Split a variant out of its app into a NEW app (or into `target_app_id`),
    applying the details the user confirmed in the dialog.

    Installer / folder edits:
      * same folder, different file  -> file_name changes and is locked (the scanner
        keeps honouring it, like 'Change installer file')
      * different folder             -> the variant is detached from its scan row
        (like a manually added variant) with the new folder + file locked; the old
        raw row is dropped if nothing else uses it.  If the old folder still exists
        a later rescan lists it again as a new unit.
    The variant is pinned to its new app, so rescans and Resolve all keep it there."""
    from resolver import normalize_key, _ensure_tag
    conn = db.connect()
    v = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    if v is None:
        raise ValueError("Variant not found.")
    name = (name or "").strip()
    if not name:
        raise ValueError("Please enter an app name.")
    old_app_id = v["app_id"]
    if target_app_id is not None and target_app_id == old_app_id:
        raise ValueError("The variant is already in that app.")

    # ---- installer / folder edits --------------------------------------------------------
    sets: dict = {}
    cur_folder, cur_file = v["source_path"], v["file_name"] or ""
    new_folder = os.path.normpath(unit_folder) if unit_folder else cur_folder
    new_file = cur_file
    if installer_path:
        if not os.path.isfile(installer_path):
            if norm_path(installer_path) != norm_path(os.path.join(cur_folder, cur_file)):
                raise ValueError(f"Installer file not found:\n{installer_path}")
        else:
            rel = os.path.relpath(installer_path, new_folder)
            if rel.startswith("..") or os.path.isabs(rel):
                raise ValueError("The installer must be inside the app folder (or one of its sub-folders).")
            new_file = rel.replace(os.sep, "/")
    folder_changed = norm_path(new_folder) != norm_path(cur_folder)
    file_changed = _norm_file(new_file) != _norm_file(cur_file)
    if folder_changed or file_changed:
        if not os.path.isdir(new_folder):
            raise ValueError(f"App folder not found:\n{new_folder}")
        clash = find_variant_at(db, new_folder, new_file)
        if clash is not None and clash["id"] != variant_id:
            raise ValueError(f"That file is already in the catalog, under “{clash['app_name']}”.")
        sets.update(source_path=new_folder, file_name=new_file, file_locked=1)
        full = os.path.join(new_folder, new_file)
        if os.path.isfile(full):
            sets.update(file_type=os.path.splitext(full)[1].lstrip(".").lower(), file_size=os.path.getsize(full))
        if folder_changed and v["raw_candidate_id"] is not None:
            old_rid = v["raw_candidate_id"]
            sets["raw_candidate_id"] = None
            conn.execute("UPDATE variants SET raw_candidate_id = NULL WHERE id = ?", (variant_id,))
            if not conn.execute("SELECT 1 FROM variants WHERE raw_candidate_id = ? LIMIT 1", (old_rid,)).fetchone():
                conn.execute("DELETE FROM raw_candidates WHERE id = ?", (old_rid,))
    for col, val, old in (("edition", edition, v["edition"]), ("architecture", architecture, v["architecture"]),
                          ("language", language, v["language"])):
        val = (val or "").strip() or None
        if val != (old or None):
            sets[col] = val
    ver = (version or "").strip() or None
    if ver != (v["version"] or None):
        sets["version"] = ver
        sets["version_locked"] = 1

    # ---- the app --------------------------------------------------------------------------
    created = False
    if target_app_id is not None:
        app_id = target_app_id
    else:
        cur = conn.execute(
            """INSERT INTO apps (name, name_locked, catalog, catalog_locked, subcatalog, subcatalog_locked,
                                  normalized_key, confidence, status, updated_at)
               VALUES (?, 1, ?, ?, ?, ?, ?, 1.0, 'verified', datetime('now'))""",
            (name, catalog or None, 1 if catalog else 0, subcatalog or None, 1 if subcatalog else 0,
             normalize_key(name)))
        app_id = cur.lastrowid
        created = True
        for tag in (catalog, subcatalog):
            if tag:
                conn.execute("INSERT OR IGNORE INTO app_tags (app_id, tag_id) VALUES (?, ?)",
                             (app_id, _ensure_tag(conn, tag)))

    sets.update(app_id=app_id, app_pinned=1)
    assign = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE variants SET {assign}, updated_at = datetime('now') WHERE id = ?",
                 (*sets.values(), variant_id))
    conn.execute("INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
                 ("variant", variant_id, "split",
                  json.dumps({"new_app_id": app_id, "new_name": name, "from_app_id": old_app_id,
                              "changed": sorted(k for k in sets if k not in ("app_id", "app_pinned"))})))
    remove_empty_unprotected_apps(conn)             # the old app, if this was its only variant
    conn.commit()
    return {"app_id": app_id, "variant_id": variant_id, "created_app": created}


# ---------------------------------------------------------------------------
# installer override: a file outside the variant's own folder
# ---------------------------------------------------------------------------
MAX_OTHER_APPS_SWALLOWED = 5


def plan_installer_override(db, variant_id: int, chosen_path: str) -> dict:
    """Work out what 'use this file anyway' would do, without changing anything.

    The scanner only ever sees files under a variant's folder, so a file
    elsewhere (e.g. one level up, in a sibling sub-folder) can only be honoured
    by moving the variant's unit root up to the COMMON PARENT folder and
    treating that whole folder as one app (the scanner's existing
    `single_app` layout role), with the chosen file as its locked entry."""
    conn = db.connect()
    v = conn.execute(
        """SELECT v.*, rc.scan_root_id AS sr_id, sr.path AS sr_path
             FROM variants v
             LEFT JOIN raw_candidates rc ON rc.id = v.raw_candidate_id
             LEFT JOIN scan_roots sr ON sr.id = rc.scan_root_id
            WHERE v.id = ?""", (variant_id,)).fetchone()
    if v is None:
        raise ValueError("Variant not found.")
    chosen_dir = os.path.dirname(chosen_path)
    try:
        new_root = os.path.commonpath([norm_path(v["source_path"]), norm_path(chosen_dir)])
    except ValueError:
        return {"ok": False, "reason": "The file is on a different drive than the app folder."}
    # keep the casing/style of the stored path where possible
    if norm_path(v["source_path"]) == new_root:
        new_root = v["source_path"]
    else:
        # rebuild from the chosen file's own path so casing matches the disk
        cur = chosen_dir
        while cur and norm_path(cur) != new_root and os.path.dirname(cur) != cur:
            cur = os.path.dirname(cur)
        new_root = cur if norm_path(cur) == new_root else new_root

    sr_path = v["sr_path"]
    if sr_path and (norm_path(new_root) == norm_path(sr_path) or not _is_inside(new_root, sr_path)):
        return {"ok": False, "reason":
                "The common parent folder is the whole scan root (or above it), so it can't be "
                "turned into a single app. Pick a file closer to the app folder, or re-organise the folders."}
    if os.path.dirname(new_root) == new_root:
        return {"ok": False, "reason": "The common parent is a drive root."}

    rel = os.path.relpath(chosen_path, new_root).replace(os.sep, "/")
    swallowed = []
    for o in conn.execute(
            """SELECT v2.id, v2.app_id, v2.source_path, v2.file_name, a.name AS app_name
                 FROM variants v2 JOIN apps a ON a.id = v2.app_id WHERE v2.id != ?""", (variant_id,)):
        if _is_inside(o["source_path"], new_root):
            swallowed.append({"variant_id": o["id"], "app_id": o["app_id"], "app": o["app_name"],
                              "path": o["source_path"], "file": o["file_name"]})
    # The new unit root becomes ONE app/variant. Variants of other apps inside it
    # are normally junk the scanner made from the unit's own sub-folders (Redist/,
    # a second setup folder ...) and the user is shown them to confirm. But if
    # MANY different apps live there it is a category folder, not an app folder.
    others = [s for s in swallowed if s["app_id"] != v["app_id"]]
    other_apps = sorted({s["app"] for s in others})
    if len(other_apps) > MAX_OTHER_APPS_SWALLOWED:
        shown = ", ".join(other_apps[:5]) + f" … (+{len(other_apps) - 5} more)"
        return {"ok": False, "reason":
                f"The folder that contains both files ({new_root}) holds {len(other_apps)} other apps "
                f"({shown}) -- it looks like a category folder, not one app's folder, so it can't be "
                "treated as a single app.\n\nPick an installer closer to this app's folder, or "
                "reorganise the folders first."}
    n_dirs = n_installers = 0
    try:
        for _r, dirs, files in os.walk(new_root):
            n_dirs += len(dirs)
            n_installers += sum(1 for f in files if f.lower().endswith((".exe", ".msi", ".msix", ".zip", ".rar", ".7z", ".iso")))
            if n_dirs > 5000:
                break
    except OSError:
        pass
    return {"ok": True, "new_root": new_root, "rel": rel, "swallowed": swallowed,
            "other_apps": other_apps,
            "sub_folders": n_dirs, "installer_like_files": n_installers,
            "scan_root_id": v["sr_id"], "scan_root_path": sr_path}


def apply_installer_override(db, variant_id: int, chosen_path: str, *,
                             remove_swallowed: bool = False) -> dict:
    """Execute plan_installer_override().  Re-points the variant (and its raw
    candidate) at the common parent, locks the file, and marks that folder as
    a `single_app` in the scan root's layout so rescans keep the choice."""
    plan = plan_installer_override(db, variant_id, chosen_path)
    if not plan["ok"]:
        raise ValueError(plan["reason"])
    conn = db.connect()
    new_root, rel = plan["new_root"], plan["rel"]

    v = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    # other variants inside the new unit root become part of this one
    for s in plan["swallowed"]:
        if remove_swallowed:
            conn.execute("DELETE FROM variants WHERE id = ?", (s["variant_id"],))
    # raw candidate bookkeeping: re-point ours, drop the others under the new root
    rc_id = v["raw_candidate_id"]
    if plan["scan_root_id"] is not None and rc_id is not None:
        for r in conn.execute("SELECT id, folder_path FROM raw_candidates WHERE scan_root_id = ? AND id != ?",
                              (plan["scan_root_id"], rc_id)).fetchall():
            if _is_inside(r["folder_path"], new_root):
                conn.execute("DELETE FROM raw_candidates WHERE id = ?", (r["id"],))
        conn.execute(
            "UPDATE raw_candidates SET folder_path = ?, primary_file_name = ?, fingerprint = 'override' WHERE id = ?",
            (new_root, rel, rc_id))
        # scan-root layout: this folder is one app/variant
        layout = db.get_folder_layout(plan["scan_root_id"])
        key = "/".join(p.lower() for p in os.path.relpath(new_root, plan["scan_root_path"]).replace("\\", "/").split("/"))
        prev = layout.get(key)
        entry = dict(prev) if isinstance(prev, dict) else {}
        entry["role"] = "single_app"
        layout[key] = entry
        db.save_folder_layout(plan["scan_root_id"], layout)

    conn.execute(
        "UPDATE variants SET source_path = ?, file_name = ?, file_locked = 1, "
        "file_type = ?, file_size = ?, updated_at = datetime('now') WHERE id = ?",
        (new_root, rel, os.path.splitext(chosen_path)[1].lstrip(".").lower(),
         os.path.getsize(chosen_path) if os.path.isfile(chosen_path) else None, variant_id))
    conn.execute("INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
                 ("variant", variant_id, "installer_override",
                  json.dumps({"new_root": new_root, "file": rel, "old_root": v["source_path"],
                              "removed_variants": [s["variant_id"] for s in plan["swallowed"]] if remove_swallowed else []})))
    if remove_swallowed:
        remove_empty_unprotected_apps(conn)       # apps the removed variants leave empty
    conn.commit()
    return plan
