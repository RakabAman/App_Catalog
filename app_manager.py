"""
app_manager.py -- catalog-hygiene tools that operate ACROSS the whole
catalog rather than one app/variant at a time (which resolver.py already
covers). Five independent feature groups, used by both the GUI's
"Organize" dialog (see app_organizer.py) and by monitor.py:

  1. DUPLICATE DETECTION      find_duplicate_groups()
  2. CATEGORY RENAME/MERGE    rename_catalog(), rename_subcatalog(), ...
  3. CATALOG REPORT           generate_catalog_report() and friends
  4. PHYSICAL REORGANIZATION  preview_reorganize() / execute_reorganize()

Groups 1-3 are entirely read-only until the caller explicitly confirms an
action (merge_apps() from resolver.py, or a plain SQL UPDATE) -- no
different in risk from anything else already in this app.

Group 4 is NOT -- it moves real files/folders on disk. See
preview_reorganize()'s and execute_reorganize()'s docstrings for the
specific safety choices made there (mandatory dry-run, collision-safe,
persisted move log, console logging of every item via the
"appcatalog.organizer.job" logger). This feature was adopted from a
parallel build of this app (DeepSeek's app_manager.py, kept for reference
as app_manager_old_version.py) after a side-by-side review; see
AI_MODULE_REFERENCE.md for the full reasoning.

Kept as its own module (rather than folded into app_organizer.py) because
monitor.py also imports directly from here -- app_organizer.py (the Qt
GUI) is one consumer of this module, not the only one.
"""
from __future__ import annotations

import csv
import errno
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from rapidfuzz import fuzz

import app_manifest                      # variant manifests (appcatalog.json)
from app_paths import get_logs_dir
from database import Database
from resolver import normalize_key

log = logging.getLogger("appcatalog.organizer")
log_job = logging.getLogger("appcatalog.organizer.job")

# ======================================================================
# 0. Scan root path management (Feature A: repath a moved/remounted drive)
# ======================================================================

def _starts_with_root(path: Optional[str], root_norm: str) -> bool:
    """
    Case-insensitive prefix match with a separator boundary -- i.e. a real
    ancestor-path check, not a substring check. `root_norm` must already be
    stripped of any trailing separator. Deliberately rejects a path that
    merely starts with the same characters but isn't actually under the
    root, e.g. root D:\\PROGRAMS must not match D:\\PROGRAMS2\\Foo.
    """
    if not path:
        return False
    if path.lower() == root_norm.lower():
        return True
    return path.lower().startswith(root_norm.lower() + os.sep) or \
        path.lower().startswith(root_norm.lower() + "/")

def _delete_zero_variant_apps(conn, app_ids) -> int:
    """
    Deletes any app in `app_ids` whose variant count is now zero. Shared
    by execute_clean_library() and apply_layout_change(): an app only
    exists because of its variants, so zero variants means there is
    nothing legitimate left to keep. Returns how many apps were deleted.
    """
    removed = 0
    for app_id in app_ids:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM variants WHERE app_id = ?", (app_id,)
        ).fetchone()["n"]
        if remaining == 0:
            conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
            removed += 1
    return removed

def _rewrite_prefix(path: Optional[str], old_root_norm: str, new_root_norm: str) -> Optional[str]:
    """
    Replaces the old_root_norm prefix with new_root_norm, preserving
    everything after it (the relative install-folder trail) byte-for-byte,
    including its original separator style/casing. Only called on paths
    already confirmed by _starts_with_root() to actually be under the root.
    """
    if path is None:
        return None
    if path.lower() == old_root_norm.lower():
        return new_root_norm
    tail = path[len(old_root_norm):]  # keeps its leading separator + original casing
    return new_root_norm + tail


def update_scan_root_path(db: Database, scan_root_id: int, new_root_path: str) -> dict:
    """
    Rewrites a scan root's path AND every absolute path in the catalog
    derived from it -- for when the external/backup drive it lives on
    changes mount point (D:\\ -> E:\\, or a UNC path gets remounted).
    Does NOT touch the filesystem and does NOT re-scan/re-resolve; the
    folder structure under the root is assumed unchanged. Prefix-match
    only (never a naive substring replace), so a sibling path that merely
    shares characters with the old root is never touched. Transactional:
    all four tables update together or not at all.

    Returns a per-table row-count dict for the GUI, e.g.
    {"scan_roots": 1, "raw_candidates": 214, "variants": 240, "scan_errors": 2}.
    """
    conn = db.connect()
    row = conn.execute("SELECT path FROM scan_roots WHERE id = ?", (scan_root_id,)).fetchone()
    if row is None:
        raise ValueError(f"No scan root with id={scan_root_id}")

    old_root = row["path"].rstrip("\\/")
    new_root = new_root_path.rstrip("\\/")
    if not new_root:
        raise ValueError("New root path cannot be empty")

    counts = {"scan_roots": 0, "raw_candidates": 0, "variants": 0, "scan_errors": 0}

    try:
        conn.execute("BEGIN")

        cur = conn.execute(
            "UPDATE scan_roots SET path = ? WHERE id = ?", (new_root, scan_root_id)
        )
        counts["scan_roots"] = cur.rowcount

        rc_rows = conn.execute(
            "SELECT id, folder_path FROM raw_candidates WHERE scan_root_id = ?",
            (scan_root_id,),
        ).fetchall()
        for r in rc_rows:
            if _starts_with_root(r["folder_path"], old_root):
                new_path = _rewrite_prefix(r["folder_path"], old_root, new_root)
                conn.execute(
                    "UPDATE raw_candidates SET folder_path = ? WHERE id = ?",
                    (new_path, r["id"]),
                )
                counts["raw_candidates"] += 1

        # variants doesn't store scan_root_id directly -- scope the rewrite
        # via the join so a variant from another root sharing a path prefix
        # is never touched.
        v_rows = conn.execute(
            """
            SELECT v.id, v.source_path FROM variants v
            JOIN raw_candidates rc ON rc.id = v.raw_candidate_id
            WHERE rc.scan_root_id = ?
            """,
            (scan_root_id,),
        ).fetchall()
        for r in v_rows:
            if _starts_with_root(r["source_path"], old_root):
                new_path = _rewrite_prefix(r["source_path"], old_root, new_root)
                conn.execute(
                    "UPDATE variants SET source_path = ? WHERE id = ?",
                    (new_path, r["id"]),
                )
                counts["variants"] += 1

        se_rows = conn.execute(
            "SELECT id, path FROM scan_errors WHERE scan_root_id = ?",
            (scan_root_id,),
        ).fetchall()
        for r in se_rows:
            if _starts_with_root(r["path"], old_root):
                new_path = _rewrite_prefix(r["path"], old_root, new_root)
                conn.execute(
                    "UPDATE scan_errors SET path = ? WHERE id = ?",
                    (new_path, r["id"]),
                )
                counts["scan_errors"] += 1

        conn.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
            ("scan_root", scan_root_id, "path_change",
             json.dumps({"old_path": old_root, "new_path": new_root, "counts": counts})),
        )

        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("Repath FAILED for scan_root_id=%s (%r -> %r); rolled back.",
                      scan_root_id, old_root, new_root)
        raise

    log.info("Repathed scan_root_id=%s: %r -> %r (%s)", scan_root_id, old_root, new_root, counts)
    return counts


def delete_scan_root(db: Database, scan_root_id: int) -> dict:
    """
    Forgets a scan root entirely: deletes the scan_roots row along with
    the raw_candidates/scan_errors rows staged from it (ON DELETE CASCADE).
    Does NOT touch the resolved catalog -- variants that came from this
    root have their raw_candidate_id set to NULL (ON DELETE SET NULL) and
    their parent apps are left exactly as they are. Use this to drop a
    root that was added by mistake, is a duplicate, or is permanently
    gone -- not as a way to remove apps from the catalog.

    Returns a row-count dict for the GUI, e.g.
    {"raw_candidates": 214, "scan_errors": 2, "variants_unlinked": 240}.
    """
    conn = db.connect()
    row = conn.execute("SELECT path FROM scan_roots WHERE id = ?", (scan_root_id,)).fetchone()
    if row is None:
        raise ValueError(f"No scan root with id={scan_root_id}")
    path = row["path"]

    counts = {"raw_candidates": 0, "scan_errors": 0, "variants_unlinked": 0}
    try:
        conn.execute("BEGIN")

        counts["raw_candidates"] = conn.execute(
            "SELECT COUNT(*) c FROM raw_candidates WHERE scan_root_id = ?", (scan_root_id,)
        ).fetchone()["c"]
        counts["scan_errors"] = conn.execute(
            "SELECT COUNT(*) c FROM scan_errors WHERE scan_root_id = ?", (scan_root_id,)
        ).fetchone()["c"]
        counts["variants_unlinked"] = conn.execute(
            """
            SELECT COUNT(*) c FROM variants v
            JOIN raw_candidates rc ON rc.id = v.raw_candidate_id
            WHERE rc.scan_root_id = ?
            """,
            (scan_root_id,),
        ).fetchone()["c"]

        conn.execute("DELETE FROM scan_roots WHERE id = ?", (scan_root_id,))

        conn.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
            ("scan_root", scan_root_id, "deleted", json.dumps({"path": path, "counts": counts})),
        )

        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("Delete FAILED for scan_root_id=%s (%r); rolled back.", scan_root_id, path)
        raise

    log.info("Deleted scan_root_id=%s (%r): %s", scan_root_id, path, counts)
    return counts



# ======================================================================
# 0b. Layout-change propagation (folder-role edits between scans)
# ======================================================================
#
# When FolderLayoutDialog saves a change that alters which folders count
# as install units (App <-> Single App/Variant <-> Subcatalog <-> Skip),
# the catalog must be brought into agreement with the new layout BEFORE
# the next scan, or every re-scan just leaves the old layout's apps
# behind (scanner upserts, resolver only creates/updates -- nothing in
# the pipeline removes a raw_candidate that's no longer walked under the
# current layout). See the Layout-Change Propagation plan for the full
# rationale and the live ADOBE/PHOTOSHOP/CS6 reproduction.

@dataclass
class LayoutChangeResult:
    """Counts returned by apply_layout_change() for the post-save status
    message, e.g. 'Removed 9 apps / 148 variants (12 folders affected).'"""
    folders_affected: int = 0
    raw_candidates_deleted: int = 0
    variants_deleted: int = 0
    apps_deleted: int = 0
    label_updates: int = 0
    role_change_folders: list = field(default_factory=list)
    label_only_folders: list = field(default_factory=list)


def _layout_role_of(entry) -> Optional[str]:
    """
    Same convention as scanner._layout_role_of(): a layout entry is
    either a bare role string or {"role": ..., "name": ...}. Duplicated
    here (rather than imported from scanner) so app_manager stays
    importable without dragging in scanner's optional archive/PE
    dependencies -- monitor.py and other consumers import app_manager
    without needing scanner at all.
    """
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get("role")
    if entry in ("catalog", "subcatalog", "app", "single_app", "skip"):
        return entry
    return None


def _layout_name_of(entry) -> Optional[str]:
    return entry.get("name") if isinstance(entry, dict) else None


def _effective_role(layout: dict, key: str, unconfigured_toplevel_role: str) -> Optional[str]:
    """
    The role a folder actually resolves to under `layout`, cascading from
    its parent's role exactly like scanner._resolve_role_chain() does --
    or the root's unconfigured_toplevel_role for a top-level key. None
    if nothing in the chain gives a role at all.

    Needed because "no explicit entry, so the default applies" and "an
    explicit entry whose value happens to equal the default" mean the
    same thing to the scanner, but look different to a naive
    old_role != new_role comparison -- which would misclassify the
    dialog's routine "write every top-level row explicitly" save as a
    role change, and delete subtrees that haven't actually changed.
    """
    explicit = _layout_role_of(layout.get(key))
    if explicit is not None:
        return explicit
    if "/" not in key:
        return unconfigured_toplevel_role
    parent_key = "/".join(key.split("/")[:-1])
    parent_role = _effective_role(layout, parent_key, unconfigured_toplevel_role)
    if parent_role is None:
        return None
    # Mirrors scanner.ROLE_CASCADE -- catalog -> subcatalog,
    # subcatalog -> app, app/single_app/skip stay put.
    return {"catalog": "subcatalog", "subcatalog": "app"}.get(parent_role, parent_role)

def _raw_candidates_under(conn, scan_root_id: int, folder_abs_norm: str) -> list:
    """
    All raw_candidates rows whose folder_path is folder_abs_norm itself
    or anywhere under it, using the same case-insensitive, separator-
    boundary-aware prefix check as update_scan_root_path() -- reusing
    _starts_with_root() rather than re-implementing it, so a path like
    D:\\PROGRAMS\\ADOBE2 is never treated as under D:\\PROGRAMS\\ADOBE.
    """
    rows = conn.execute(
        "SELECT id, folder_path, catalog, subcatalog FROM raw_candidates "
        "WHERE scan_root_id = ?",
        (scan_root_id,),
    ).fetchall()
    return [r for r in rows if _starts_with_root(r["folder_path"], folder_abs_norm)]


def apply_layout_change(
    db: Database, scan_root_id: int,
    old_layout: dict, new_layout: dict,
    *,
    old_unconfigured_toplevel_role: Optional[str] = None,
    new_unconfigured_toplevel_role: Optional[str] = None,
) -> LayoutChangeResult:
    """
    Brings the catalog into agreement with an edited folder layout so a
    re-scan rebuilds the affected subtrees cleanly instead of leaving
    stale apps behind. Called by FolderLayoutDialog._on_ok() right after
    save_folder_layout(); save_folder_layout() itself stays dumb (just
    writes the JSON) -- propagation is the caller's concern.

    Two classes of change, classified per-key from the diff of old vs
    new layout:

      LABEL-ONLY (role unchanged; only the optional `name` differs):
        UPDATE raw_candidates.catalog/subcatalog in place across the
        affected subtree. No deletion, no re-scan needed.

      ROLE CHANGE (role value differs, or an entry was added/removed):
        delete variants + raw_candidates under the affected subtree,
        then delete any app left with zero variants (same rule
        execute_clean_library() follows). Because the fingerprints go
        with the raw_candidates, the next incremental scan cannot skip
        these folders even if their mtime is unchanged -- a natural
        re-scan of just the affected subtree is automatic, no new
        'force re-scan' flag needed.

    All steps run in one transaction. No filesystem operations -- this
    is catalog cleanup, structurally identical to execute_clean_library,
    just scoped by folder path instead of by missing files. Apps that
    still have variants from outside the affected scope are left
    untouched (name_locked / catalog_locked / subcatalog_locked /
    status / scraper fields all survive because the app itself survives).

    Returns a LayoutChangeResult with counts for the post-save status
    message.
    """
    # Lazy import -- resolve_scan_root_layout is the same function the
    # scanner uses, so label computation cannot drift between the two.
    # Done inside the function so app_manager's other consumers don't
    # pay for scanner's optional archive/PE imports.
    from scanner import resolve_scan_root_layout

    conn = db.connect()
    result = LayoutChangeResult()

    scan_root_row = conn.execute(
        "SELECT path, unconfigured_toplevel_role FROM scan_roots WHERE id = ?",
        (scan_root_id,),
    ).fetchone()
    if scan_root_row is None:
        raise ValueError(f"No scan root with id={scan_root_id}")
    root_path = scan_root_row["path"].rstrip("\\/")
    old_top_default = (
        old_unconfigured_toplevel_role
        or scan_root_row["unconfigured_toplevel_role"]
        or "catalog"
    )
    new_top_default = (
        new_unconfigured_toplevel_role
        or scan_root_row["unconfigured_toplevel_role"]
        or "catalog"
    )
    # ---- 1. Classify each key in the union of old/new layouts ----
    # Compares EFFECTIVE roles (with cascade defaults applied), not raw
    # explicit-entry values -- "no entry, so the default applies" and
    # "explicit entry that equals the default" mean the same thing to
    # the scanner, so they must mean the same thing here too. Otherwise
    # the dialog's routine "write every top-level row explicitly" save
    # would misclassify as a role change and wipe untouched subtrees.
    all_keys = set(old_layout) | set(new_layout)
    role_change_keys: list[str] = []
    label_only_keys: list[str] = []
    for key in sorted(all_keys):
        old_entry = old_layout.get(key)
        new_entry = new_layout.get(key)
        old_eff = _effective_role(old_layout, key, old_top_default)
        new_eff = _effective_role(new_layout, key, new_top_default)
        if old_eff != new_eff:
            role_change_keys.append(key)
        elif _layout_name_of(old_entry) != _layout_name_of(new_entry):
            label_only_keys.append(key)
        # else: identical -> ignored

    if not role_change_keys and not label_only_keys:
        return result

    try:
        conn.execute("BEGIN")

        # ---- 2. Role changes: delete raw_candidates / variants / apps ----
        if role_change_keys:
            rc_ids: set = set()
            for key in role_change_keys:
                abs_norm = os.path.join(root_path, *key.split("/")).rstrip("\\/")
                for r in _raw_candidates_under(conn, scan_root_id, abs_norm):
                    rc_ids.add(r["id"])

            app_ids_touched: set = set()
            if rc_ids:
                id_list = list(rc_ids)
                ph = ",".join("?" * len(id_list))
                v_rows = conn.execute(
                    f"SELECT id, app_id FROM variants WHERE raw_candidate_id IN ({ph})",
                    id_list,
                ).fetchall()
                variant_ids = [v["id"] for v in v_rows]
                for v in v_rows:
                    app_ids_touched.add(v["app_id"])

                if variant_ids:
                    vph = ",".join("?" * len(variant_ids))
                    conn.execute(
                        f"DELETE FROM variants WHERE id IN ({vph})", variant_ids
                    )
                    result.variants_deleted = len(variant_ids)

                conn.execute(
                    f"DELETE FROM raw_candidates WHERE id IN ({ph})", id_list
                )
                result.raw_candidates_deleted = len(id_list)

            if app_ids_touched:
                result.apps_deleted = _delete_zero_variant_apps(conn, app_ids_touched)

            result.role_change_folders = list(role_change_keys)

        # ---- 3. Label-only changes: in-place UPDATE of catalog/subcatalog ----
        for key in label_only_keys:
            abs_norm = os.path.join(root_path, *key.split("/")).rstrip("\\/")
            old_cat, old_sub, _, skip_old, _, _ = resolve_scan_root_layout(
                root_path, abs_norm, old_layout,
                unconfigured_toplevel_role=old_top_default,
            )
            if skip_old:
                continue
            new_cat, new_sub, _, skip_new, _, _ = resolve_scan_root_layout(
                root_path, abs_norm, new_layout,
                unconfigured_toplevel_role=new_top_default,
            )
            if skip_new or (old_cat, old_sub) == (new_cat, new_sub):
                continue
            for r in _raw_candidates_under(conn, scan_root_id, abs_norm):
                conn.execute(
                    "UPDATE raw_candidates SET catalog = ?, subcatalog = ? WHERE id = ?",
                    (new_cat, new_sub, r["id"]),
                )
                result.label_updates += 1
            result.label_only_folders.append(key)

        # ---- 4. Audit log (same pattern as update_scan_root_path/delete_scan_root) ----
        conn.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) "
            "VALUES (?,?,?,?)",
            ("scan_root", scan_root_id, "layout_change", json.dumps({
                "role_change_folders": result.role_change_folders,
                "label_only_folders": result.label_only_folders,
                "raw_candidates_deleted": result.raw_candidates_deleted,
                "variants_deleted": result.variants_deleted,
                "apps_deleted": result.apps_deleted,
                "label_updates": result.label_updates,
            })),
        )

        conn.commit()
    except Exception:
        conn.rollback()
        log.exception(
            "Layout-change propagation FAILED for scan_root_id=%s; rolled back.",
            scan_root_id,
        )
        raise

    result.folders_affected = len(set(role_change_keys) | set(label_only_keys))
    log.info(
        "Layout change propagated for scan_root_id=%s: %d role change(s), "
        "%d label-only change(s) -> removed %d raw_candidate(s) / %d variant(s) / "
        "%d app(s); %d label update(s)",
        scan_root_id, len(role_change_keys), len(label_only_keys),
        result.raw_candidates_deleted, result.variants_deleted,
        result.apps_deleted, result.label_updates,
    )
    return result


# ======================================================================
# 1. Duplicate detection
# ======================================================================

@dataclass
class DuplicateGroup:
    members: list[dict]        # each dict: id, name, catalog, subcatalog, variant_count, sample_paths
    reason: str
    score: float

    @property
    def app_ids(self) -> list[int]:
        return [m["id"] for m in self.members]

    @property
    def names(self) -> list[str]:
        return [m["name"] for m in self.members]

# Helper to strip trailing version for base key
def _base_key(name: str) -> str:
    if not name:
        return ""
    # Remove trailing version: " 26.3.0", " v5.5", " [2024]", "(2024)"
    stripped = re.sub(r'\s*[vV]?\s*[\d.]+$', '', name)
    stripped = re.sub(r'\s*[\[\(][\d.]+[\]\)]\s*$', '', stripped)
    return normalize_key(stripped)   # reuse aggressive normalizer

# Helper for token set – split into words, filter short/common
def _token_set(name: str) -> set[str]:
    if not name:
        return set()
    # remove version-like tokens (digits and dots)
    words = re.findall(r'[a-zA-Z]+', name.lower())
    # filter out very short or common stopwords
    stopwords = {'the', 'for', 'and', 'of', 'with', 'without', 'edition', 'version', 'v', 'vs', 'pro', 'lite', 'free'}
    return {w for w in words if len(w) > 2 and w not in stopwords}

# app_manager.py (additions/modifications)

def _dup_normalize(name: str) -> str:
    """Fixed normalizer for duplicate detection – not influenced by resolver settings.
       - lowercases
       - strips trailing version numbers (v2.3, -v5, [2024])
       - strips parentheticals like (x64), (Pro)
       - removes non-alphanumerics (spaces become '_')
    """
    if not name:
        return ""
    s = name.lower()
    # strip trailing version: " v2.3", " -v5", " [2024]", "(2024)"
    s = re.sub(r'\s*[vV]?\s*[\d.]+$', '', s)
    s = re.sub(r'\s*[\[\(][\d.]+[\]\)]\s*$', '', s)
    # strip common parentheticals
    s = re.sub(r'\s*\([^)]*\)\s*', ' ', s)
    # remove non-alphanumerics and collapse spaces
    s = re.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')

def find_duplicate_groups(db: Database, fuzzy_threshold: Optional[int] = None) -> list[DuplicateGroup]:
    conn = db.connect()
    if fuzzy_threshold is None:
        settings = db.get_all_settings()
        fuzzy_threshold = int(settings.get("fuzzy_match_threshold", 88))

    # Fetch full app details + variant counts and sample paths
    rows = conn.execute("""
        SELECT a.id, a.name, a.catalog, a.subcatalog,
               COUNT(v.id) AS variant_count,
               GROUP_CONCAT(DISTINCT v.source_path) AS sample_paths
        FROM apps a
        LEFT JOIN variants v ON v.app_id = a.id AND v.is_ignored = 0
        GROUP BY a.id
        ORDER BY a.name
    """).fetchall()
    apps = []
    for r in rows:
        d = dict(r)
        d["sample_paths"] = d["sample_paths"].split(',') if d["sample_paths"] else []
        d["base"] = _base_key(d["name"])
        d["norm"] = normalize_key(d["name"])
        d["dup_norm"] = _dup_normalize(d["name"])
        d["tokens"] = _token_set(d["name"])
        apps.append(d)

    groups = []
    grouped_ids = set()

    def _add_group(members: list[dict], reason: str, score: float):
        nonlocal groups, grouped_ids
        ids = [m["id"] for m in members]
        # Avoid double‑grouping
        if any(i in grouped_ids for i in ids):
            return
        groups.append(DuplicateGroup(
            members=members,
            reason=reason,
            score=score
        ))
        grouped_ids.update(ids)

    # ---- Pass 1: exact dup_norm ----
    by_dup_norm: dict[str, list[dict]] = {}
    for a in apps:
        by_dup_norm.setdefault(a["dup_norm"], []).append(a)
    for key, members in by_dup_norm.items():
        if len(members) > 1:
            _add_group(members, "exact fixed name", 100.0)

    # ---- Pass 2: exact base (version stripped) ----
    # (only for leftovers)
    remaining = [a for a in apps if a["id"] not in grouped_ids]
    by_base: dict[str, list[dict]] = {}
    for a in remaining:
        by_base.setdefault(a["base"], []).append(a)
    for key, members in by_base.items():
        if len(members) > 1:
            _add_group(members, "exact base (version stripped)", 100.0)

    # ---- Pass 3: exact norm (for leftovers) ----
    remaining = [a for a in apps if a["id"] not in grouped_ids]
    by_norm: dict[str, list[dict]] = {}
    for a in remaining:
        by_norm.setdefault(a["norm"], []).append(a)
    for key, members in by_norm.items():
        if len(members) > 1:
            _add_group(members, "exact normalized", 100.0)

    # ---- Pass 4: fuzzy on base ----
    # (same as before, but with remaining)
    remaining = [a for a in apps if a["id"] not in grouped_ids]
    used = set()
    for i, a in enumerate(remaining):
        if a["id"] in used:
            continue
        cluster = [a]
        for b in remaining[i+1:]:
            if b["id"] in used:
                continue
            score = fuzz.ratio(a["base"], b["base"])
            if score >= fuzzy_threshold:
                cluster.append(b)
                used.add(b["id"])
        if len(cluster) > 1:
            best = min(fuzz.ratio(cluster[0]["base"], m["base"]) for m in cluster[1:])
            _add_group(cluster, "fuzzy base", float(best))
            used.update(m["id"] for m in cluster)

    # ---- Pass 5: token set ----
    remaining = [a for a in apps if a["id"] not in grouped_ids]
    used = set()
    for i, a in enumerate(remaining):
        if a["id"] in used:
            continue
        cluster = [a]
        for b in remaining[i+1:]:
            if b["id"] in used:
                continue
            if a["tokens"] and b["tokens"]:
                score = fuzz.token_set_ratio(" ".join(a["tokens"]), " ".join(b["tokens"]))
                if score >= fuzzy_threshold:
                    cluster.append(b)
                    used.add(b["id"])
        if len(cluster) > 1:
            best = min(fuzz.token_set_ratio(" ".join(cluster[0]["tokens"]), " ".join(m["tokens"])) for m in cluster[1:])
            _add_group(cluster, "token set", float(best))
            used.update(m["id"] for m in cluster)

    # ---- Pass 6: partial ratio ----
    remaining = [a for a in apps if a["id"] not in grouped_ids]
    used = set()
    for i, a in enumerate(remaining):
        if a["id"] in used:
            continue
        cluster = [a]
        for b in remaining[i+1:]:
            if b["id"] in used:
                continue
            score = fuzz.partial_ratio(a["base"], b["base"])
            if score >= fuzzy_threshold:
                cluster.append(b)
                used.add(b["id"])
        if len(cluster) > 1:
            best = min(fuzz.partial_ratio(cluster[0]["base"], m["base"]) for m in cluster[1:])
            _add_group(cluster, "partial match", float(best))
            used.update(m["id"] for m in cluster)

    log.info("Duplicate scan: %d app(s) examined -> %d possible group(s) found", len(apps), len(groups))
    return groups

# ======================================================================
# 2. Category / subcategory rename & merge
# ======================================================================

def rename_catalog(db: Database, old_name: str, new_name: str) -> int:
    """
    Bulk-renames a catalog across every app that currently has it --
    also usable as a "merge" by renaming catalog A to an already-existing
    catalog name B (their apps simply combine under B, nothing special
    needed since this schema uses a plain text column rather than a
    foreign-keyed categories table). Returns the number of apps affected.
    """
    conn = db.connect()
    cur = conn.execute(
        "UPDATE apps SET catalog = ?, updated_at = datetime('now') WHERE catalog = ?",
        (new_name, old_name),
    )
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
        ("catalog", 0, "rename", json.dumps({"old": old_name, "new": new_name, "affected": cur.rowcount})),
    )
    conn.commit()
    log.info("Renamed catalog %r -> %r (%d app(s) affected)", old_name, new_name, cur.rowcount)
    return cur.rowcount


def rename_subcatalog(db: Database, new_name: str, old_name: str, catalog: Optional[str] = None) -> int:
    """
    Bulk-renames a subcatalog. If `catalog` is given, only apps under that
    specific catalog are affected (the normal case -- the same subcatalog
    NAME can validly mean different things in different catalogs). If
    `catalog` is None, renames the subcatalog name everywhere regardless
    of catalog -- this is the "merge a subcategory name that's been
    accidentally duplicated across categories" case from the structural
    report (see generate_structural_report()'s duplicate_subcategory_names).
    """
    conn = db.connect()
    if catalog is not None:
        cur = conn.execute(
            "UPDATE apps SET subcatalog = ?, updated_at = datetime('now') "
            "WHERE subcatalog = ? AND catalog = ?",
            (new_name, old_name, catalog),
        )
    else:
        cur = conn.execute(
            "UPDATE apps SET subcatalog = ?, updated_at = datetime('now') WHERE subcatalog = ?",
            (new_name, old_name),
        )
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
        ("subcatalog", 0, "rename",
         json.dumps({"old": old_name, "new": new_name, "catalog": catalog, "affected": cur.rowcount})),
    )
    conn.commit()
    log.info("Renamed subcatalog %r -> %r%s (%d app(s) affected)",
              old_name, new_name, f" in catalog {catalog!r}" if catalog else " (all catalogs)", cur.rowcount)
    return cur.rowcount


def get_category_tree(db: Database) -> list[dict]:
    """
    Read-only overview of the catalog/subcatalog structure actually in use,
    built live off the `apps` table (catalog/subcatalog are plain text
    columns -- there's no separate categories table to keep in sync, so
    this is always exactly what the apps currently say). Powers the
    Categories tab's tree view.

    Shape:
        [{"catalog": "Desktop Utilities", "app_count": 12,
          "subcatalogs": [{"subcatalog": "Customizers", "app_count": 5}, ...]},
         ...]

    Apps with no subcatalog set are grouped under subcatalog "" (the GUI
    displays this as "(none)"). Catalog is required to appear at all --
    an app with no catalog set isn't part of any category structure yet.
    """
    conn = db.connect()
    rows = conn.execute(
        "SELECT catalog, COALESCE(subcatalog, '') AS subcatalog, COUNT(*) AS n "
        "FROM apps WHERE catalog IS NOT NULL AND catalog != '' "
        "GROUP BY catalog, subcatalog ORDER BY catalog, subcatalog"
    ).fetchall()
    by_catalog: dict[str, dict] = {}
    for r in rows:
        cat = by_catalog.setdefault(
            r["catalog"], {"catalog": r["catalog"], "app_count": 0, "subcatalogs": []}
        )
        cat["app_count"] += r["n"]
        cat["subcatalogs"].append({"subcatalog": r["subcatalog"], "app_count": r["n"]})
    return list(by_catalog.values())


def move_subcatalog(db: Database, subcatalog: str, from_catalog: str, to_catalog: str,
                     new_subcatalog: Optional[str] = None) -> int:
    """
    Moves a subcatalog (and every app in it) from one catalog to another,
    optionally renaming it in the same step. If `to_catalog` already has a
    subcatalog with the resulting name, apps simply combine into it -- same
    merge-via-UPDATE behavior as rename_catalog/rename_subcatalog.
    """
    conn = db.connect()
    target_sub = subcatalog if new_subcatalog is None else new_subcatalog
    cur = conn.execute(
        "UPDATE apps SET catalog = ?, subcatalog = ?, updated_at = datetime('now') "
        "WHERE catalog = ? AND subcatalog = ?",
        (to_catalog, target_sub, from_catalog, subcatalog),
    )
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
        ("subcatalog", 0, "move", json.dumps({
            "subcatalog": subcatalog, "from_catalog": from_catalog,
            "to_catalog": to_catalog, "new_subcatalog": target_sub, "affected": cur.rowcount,
        })),
    )
    conn.commit()
    log.info("Moved subcatalog %r: %r -> %r as %r (%d app(s) affected)",
              subcatalog, from_catalog, to_catalog, target_sub, cur.rowcount)
    return cur.rowcount


def promote_subcatalog_to_catalog(db: Database, catalog: str, subcatalog: str,
                                   new_catalog_name: Optional[str] = None) -> int:
    """
    Turns a subcatalog into a top-level catalog of its own. Apps that had
    (catalog, subcatalog) get catalog=new_catalog_name (defaults to the
    subcatalog's own name) and subcatalog cleared.
    """
    conn = db.connect()
    target = subcatalog if new_catalog_name is None else new_catalog_name
    cur = conn.execute(
        "UPDATE apps SET catalog = ?, subcatalog = '', updated_at = datetime('now') "
        "WHERE catalog = ? AND subcatalog = ?",
        (target, catalog, subcatalog),
    )
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
        ("subcatalog", 0, "promote_to_catalog", json.dumps({
            "catalog": catalog, "subcatalog": subcatalog,
            "new_catalog": target, "affected": cur.rowcount,
        })),
    )
    conn.commit()
    log.info("Promoted subcatalog %r/%r to top-level catalog %r (%d app(s) affected)",
              catalog, subcatalog, target, cur.rowcount)
    return cur.rowcount


def demote_catalog_to_subcatalog(db: Database, catalog: str, new_parent_catalog: str,
                                  new_subcatalog_name: Optional[str] = None) -> int:
    """
    The inverse of promote: turns an entire top-level catalog into a
    subcatalog nested under a different catalog. Every app currently under
    `catalog` is re-parented -- its existing subcatalog value (if any) is
    discarded in favor of the single new subcatalog name, since a catalog
    collapsing into "just one subcatalog" can only carry one name. If the
    catalog had several distinct subcatalogs, run individual moves first
    (via move_subcatalog) for any that should keep their own identity.
    """
    conn = db.connect()
    target_sub = catalog if new_subcatalog_name is None else new_subcatalog_name
    cur = conn.execute(
        "UPDATE apps SET catalog = ?, subcatalog = ?, updated_at = datetime('now') "
        "WHERE catalog = ?",
        (new_parent_catalog, target_sub, catalog),
    )
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
        ("catalog", 0, "demote_to_subcatalog", json.dumps({
            "catalog": catalog, "new_parent_catalog": new_parent_catalog,
            "new_subcatalog": target_sub, "affected": cur.rowcount,
        })),
    )
    conn.commit()
    log.info("Demoted catalog %r to subcatalog %r under %r (%d app(s) affected)",
              catalog, target_sub, new_parent_catalog, cur.rowcount)
    return cur.rowcount


def merge_subcatalogs(db: Database, catalog: str, source_subcatalogs: list[str],
                       target_subcatalog: str) -> int:
    """
    Combines several subcatalogs within the same catalog into one. Thin
    wrapper looping rename_subcatalog() per source name -- kept as its own
    function since the GUI operation is "select several, merge into one"
    rather than "rename one at a time".
    """
    total = 0
    for src in source_subcatalogs:
        if src == target_subcatalog:
            continue
        total += rename_subcatalog(db, target_subcatalog, src, catalog=catalog)
    return total


# ======================================================================
# 3. Structural health report
# ======================================================================

@dataclass
class StructuralReport:
    spread_apps: list[dict] = field(default_factory=list)
    duplicate_subcategory_names: list[dict] = field(default_factory=list)
    unused_aliases: list[dict] = field(default_factory=list)


def generate_structural_report(db: Database) -> StructuralReport:
    """
    Read-only catalog-hygiene diagnostics -- nothing here changes any
    data, it's purely a "here's what might be worth cleaning up" list the
    GUI shows in the Organize dialog's Report tab.
    """
    conn = db.connect()

    # An app is "spread" if its variants live under more than one
    # DIFFERENT scan root -- i.e. the same app exists in more than one
    # backup location, which is worth knowing about (are they actually
    # the same content duplicated, or genuinely different collections?).
    spread_rows = conn.execute(
        """
        SELECT a.id AS app_id, a.name, a.catalog, a.subcatalog,
               COUNT(DISTINCT r.scan_root_id) AS root_count,
               GROUP_CONCAT(DISTINCT sr.path) AS root_paths
        FROM apps a
        JOIN variants v ON v.app_id = a.id AND v.is_ignored = 0
        JOIN raw_candidates r ON r.id = v.raw_candidate_id
        JOIN scan_roots sr ON sr.id = r.scan_root_id
        GROUP BY a.id
        HAVING root_count > 1
        ORDER BY root_count DESC, a.name
        """
    ).fetchall()
    spread_apps = [dict(r) for r in spread_rows]

    # This schema stores catalog/subcatalog as plain text on `apps`
    # (rather than a normalized categories/subcategories table with
    # foreign keys) -- a subcategory name reused under two DIFFERENT
    # catalogs isn't inherently wrong (it's a fully valid situation, e.g.
    # a "Themes" subcategory could sensibly exist under both "Desktop"
    # and "Mobile"), but it's common enough to be an accidental taxonomy
    # split worth surfacing for a manual look.
    dup_subcat_rows = conn.execute(
        """
        SELECT subcatalog, COUNT(DISTINCT catalog) AS catalog_count,
               GROUP_CONCAT(DISTINCT catalog) AS catalogs
        FROM apps
        WHERE subcatalog IS NOT NULL AND subcatalog != ''
        GROUP BY subcatalog
        HAVING catalog_count > 1
        ORDER BY catalog_count DESC, subcatalog
        """
    ).fetchall()
    duplicate_subcategory_names = [dict(r) for r in dup_subcat_rows]

    # "Orphaned category" in the DeepSeek build (a categories-table FK
    # with zero apps) doesn't map directly onto this schema since there's
    # no separate categories table to be orphaned FROM -- the closest
    # equivalent here is a folder_name_aliases entry whose resulting
    # display value no longer matches any current app's catalog OR
    # subcatalog, i.e. a leftover alias rule nothing uses anymore.
    settings = db.get_all_settings()
    aliases = settings.get("folder_name_aliases", {})
    used_catalogs = {r["catalog"] for r in conn.execute(
        "SELECT DISTINCT catalog FROM apps WHERE catalog IS NOT NULL"
    ).fetchall()}
    used_subcatalogs = {r["subcatalog"] for r in conn.execute(
        "SELECT DISTINCT subcatalog FROM apps WHERE subcatalog IS NOT NULL"
    ).fetchall()}
    used_values = used_catalogs | used_subcatalogs
    unused_aliases = [
        {"raw_name": k, "display_name": v}
        for k, v in aliases.items() if v not in used_values
    ]

    return StructuralReport(
        spread_apps=spread_apps,
        duplicate_subcategory_names=duplicate_subcategory_names,
        unused_aliases=unused_aliases,
    )


# ======================================================================
# 3b. Full catalog report -- overview + health checks in one place
# ======================================================================
#
# generate_structural_report() above answers one narrow question
# ("is the catalog/subcatalog taxonomy internally consistent?"). This
# section answers the broader one a user actually opens the Report tab
# to ask: "is my catalog in good shape, and if not, what do I do about
# it?" It's built as a list of independent ReportSection objects rather
# than one big fixed shape so that:
#   - the GUI can render it as a collapsible tree (one branch/section)
#     without knowing what's inside each section ahead of time,
#   - each finding that maps to a specific app carries that app's id,
#     so the GUI can turn "double-click" into "select this app in the
#     main table" instead of the report being a dead-end wall of text,
#   - export (Markdown/CSV/clipboard) is generic over the section list,
#   - adding a new check later means adding one function + one line in
#     generate_catalog_report(), not touching rendering code.

@dataclass
class ReportItem:
    """One line in a report section. app_id is set whenever the finding
    maps to a single app, so the GUI can jump straight to it."""
    text: str
    app_id: Optional[int] = None
    detail: dict = field(default_factory=dict)


@dataclass
class ReportSection:
    key: str                 # stable id, e.g. "needs_review" -- for diffing/export
    title: str
    severity: str             # "ok" (nothing to do) | "info" | "warning"
    items: list[ReportItem] = field(default_factory=list)
    empty_text: str = "None found."

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass
class CatalogReport:
    generated_at: str
    sections: list[ReportSection] = field(default_factory=list)

    def section(self, key: str) -> Optional[ReportSection]:
        return next((s for s in self.sections if s.key == key), None)

    @property
    def warning_count(self) -> int:
        return sum(s.count for s in self.sections if s.severity == "warning")

    @property
    def info_count(self) -> int:
        return sum(s.count for s in self.sections if s.severity == "info")


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def generate_catalog_report(
    db: Database,
    *,
    stale_scan_days: int = 30,
    duplicate_groups_limit: int = 15,
) -> CatalogReport:
    """
    Builds the full Report-tab report as an ordered list of sections.
    Entirely read-only (same guarantee as generate_structural_report()).

    stale_scan_days -- a scan root that hasn't finished a scan in this
    many days (or never has) is flagged, since its catalog contents may
    no longer reflect what's actually on disk.
    duplicate_groups_limit -- find_duplicate_groups() can be expensive
    and verbose on a large catalog; the report shows this many groups
    with a "+N more, use the Duplicates tab" note rather than all of
    them, since fixing duplicates belongs to that tab, not this report.
    """
    conn = db.connect()
    settings = db.get_all_settings()
    sections: list[ReportSection] = []

    # ---- Overview -----------------------------------------------------
    # Informational counts framing everything below. No app_ids here --
    # nothing to jump to, they're catalog-wide aggregates.
    total_apps = conn.execute("SELECT COUNT(*) c FROM apps").fetchone()["c"]
    total_variants = conn.execute(
        "SELECT COUNT(*) c FROM variants WHERE is_ignored = 0"
    ).fetchone()["c"]
    ignored_variants = conn.execute(
        "SELECT COUNT(*) c FROM variants WHERE is_ignored = 1"
    ).fetchone()["c"]
    status_rows = conn.execute(
        "SELECT status, COUNT(*) c FROM apps GROUP BY status"
    ).fetchall()
    status_counts = {r["status"] or "(none)": r["c"] for r in status_rows}
    catalog_count = conn.execute(
        "SELECT COUNT(DISTINCT catalog) c FROM apps WHERE catalog IS NOT NULL AND catalog != ''"
    ).fetchone()["c"]
    subcatalog_count = conn.execute(
        "SELECT COUNT(DISTINCT subcatalog) c FROM apps WHERE subcatalog IS NOT NULL AND subcatalog != ''"
    ).fetchone()["c"]
    scan_root_count = conn.execute("SELECT COUNT(*) c FROM scan_roots").fetchone()["c"]
    scraped = conn.execute(
        "SELECT COUNT(*) c FROM apps WHERE scrape_status = 'scraped'"
    ).fetchone()["c"]
    scrape_pct = (scraped / total_apps * 100) if total_apps else 0.0

    overview_items = [
        ReportItem(f"{total_apps} apps across {catalog_count} catalogs / {subcatalog_count} subcatalogs"),
        ReportItem(f"{total_variants} active variants" + (f" ({ignored_variants} ignored)" if ignored_variants else "")),
        ReportItem(f"{scan_root_count} scan root(s)"),
        ReportItem("Status breakdown: " + ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items()))),
        ReportItem(f"Scrape coverage: {scraped}/{total_apps} apps ({scrape_pct:.0f}%)"),
    ]
    sections.append(ReportSection("overview", "Overview", "ok", overview_items))

    # ---- Needs review queue --------------------------------------------
    # Apps the resolver itself flagged as uncertain. This is the single
    # most actionable list in the whole report -- surfacing it front and
    # center (rather than making the user find it via the Status filter)
    # is the main point of this section existing.
    review_rows = conn.execute(
        """
        SELECT id, name, catalog, subcatalog, confidence
        FROM apps WHERE status = 'needs_review'
        ORDER BY confidence ASC, name
        """
    ).fetchall()
    review_items = [
        ReportItem(
            f"{r['name']}  ({r['catalog'] or '?'}/{r['subcatalog'] or '?'}) -- "
            f"confidence {r['confidence']:.2f}" if r["confidence"] is not None else "n/a",
            app_id=r["id"],
        )
        for r in review_rows
    ]
    sections.append(ReportSection(
        "needs_review", f"Apps needing review ({len(review_items)})",
        "warning" if review_items else "ok", review_items,
        empty_text="Nothing waiting for review.",
    ))

    # ---- Confidence/status mismatch ------------------------------------
    # An app marked 'resolved' or 'verified' but sitting below the
    # configured needs-review threshold is inconsistent -- most often
    # the result of a manual status override after the fact, or a
    # settings change since the app was last (re-)resolved. Worth a
    # separate check from the queue above since these won't show up
    # when just filtering the table by status.
    threshold = float(settings.get("confidence_needs_review", 0.60))
    mismatch_rows = conn.execute(
        """
        SELECT id, name, catalog, subcatalog, confidence, status
        FROM apps
        WHERE status IN ('resolved', 'verified')
          AND confidence IS NOT NULL AND confidence < ?
        ORDER BY confidence ASC, name
        """,
        (threshold,),
    ).fetchall()
    mismatch_items = [
        ReportItem(
            f"{r['name']} ({r['catalog'] or '?'}/{r['subcatalog'] or '?'}) -- "
            f"marked {r['status']} but confidence {r['confidence']:.2f} is below "
            f"the review threshold ({threshold:.2f})",
            app_id=r["id"],
        )
        for r in mismatch_rows
    ]
    sections.append(ReportSection(
        "confidence_mismatch", f"Low-confidence apps marked resolved/verified ({len(mismatch_items)})",
        "warning" if mismatch_items else "ok", mismatch_items,
        empty_text="No resolved/verified app is below the review threshold.",
    ))

    # ---- Unresolved raw candidates --------------------------------------
    # raw_candidates the scanner found but that never became a variant --
    # i.e. install units sitting on disk that the resolver skipped or
    # couldn't place. These are otherwise invisible in the apps table
    # (which only shows apps/variants), so without this section they'd
    # only ever surface by chance during a manual folder browse.
    unresolved_rows = conn.execute(
        """
        SELECT r.id, r.folder_path, sr.path AS root_path
        FROM raw_candidates r
        LEFT JOIN variants v ON v.raw_candidate_id = r.id
        JOIN scan_roots sr ON sr.id = r.scan_root_id
        WHERE v.id IS NULL
        ORDER BY r.folder_path
        """
    ).fetchall()
    unresolved_items = [
        ReportItem(f"{r['folder_path']}  (root: {r['root_path']})")
        for r in unresolved_rows
    ]
    sections.append(ReportSection(
        "unresolved_candidates", f"Scanned folders never resolved into an app ({len(unresolved_items)})",
        "warning" if unresolved_items else "ok", unresolved_items,
        empty_text="Every scanned install unit is accounted for.",
    ))

    # ---- Metadata completeness ------------------------------------------
    # Apps with no description/publisher AND no successful scrape --
    # candidates for "Scrape selected" or a manual edit. Deliberately
    # excludes 'ignored' apps (metadata on something the user has
    # dismissed isn't worth flagging).
    missing_meta_rows = conn.execute(
        """
        SELECT id, name, catalog, subcatalog, scrape_status
        FROM apps
        WHERE status != 'ignored'
          AND (publisher IS NULL OR publisher = '')
          AND (description IS NULL OR description = '')
        ORDER BY name
        """
    ).fetchall()
    missing_meta_items = [
        ReportItem(
            f"{r['name']} ({r['catalog'] or '?'}/{r['subcatalog'] or '?'}) -- "
            f"scrape status: {r['scrape_status'] or 'not_scraped'}",
            app_id=r["id"],
        )
        for r in missing_meta_rows
    ]
    failed_scrape_rows = conn.execute(
        "SELECT id, name FROM apps WHERE scrape_status = 'failed' ORDER BY name"
    ).fetchall()
    if failed_scrape_rows:
        missing_meta_items.append(ReportItem(
            f"{len(failed_scrape_rows)} app(s) have a failed scrape attempt: "
            + ", ".join(r["name"] for r in failed_scrape_rows[:10])
            + (", ..." if len(failed_scrape_rows) > 10 else "")
        ))
    sections.append(ReportSection(
        "metadata_gaps", f"Apps missing publisher/description ({len(missing_meta_items)})",
        "info" if missing_meta_items else "ok", missing_meta_items,
        empty_text="Every active app has at least basic metadata.",
    ))

    # ---- Duplicate groups (summary only -- full detail lives in the
    #      Duplicates tab, this just tells the user there's work there) --
    try:
        dup_groups = find_duplicate_groups(db)
    except Exception as exc:  # pragma: no cover -- defensive, report must never crash the dialog
        log.warning("find_duplicate_groups failed during report generation: %s", exc)
        dup_groups = []
    dup_items = []
    for g in dup_groups[:duplicate_groups_limit]:
        dup_items.append(ReportItem(
            f"{g.reason} (score {g.score:.0f}): " + ", ".join(g.names)
        ))
    if len(dup_groups) > duplicate_groups_limit:
        dup_items.append(ReportItem(
            f"...and {len(dup_groups) - duplicate_groups_limit} more group(s) -- see the Duplicates tab."
        ))
    sections.append(ReportSection(
        "duplicate_groups", f"Possible duplicate groups ({len(dup_groups)})",
        "warning" if dup_groups else "ok", dup_items,
        empty_text="No likely duplicates detected.",
    ))

    # ---- Structural issues (reuses the existing narrower report so the
    #      two checks can never drift apart) ------------------------------
    structural = generate_structural_report(db)
    spread_items = [
        ReportItem(
            f"{a['name']} ({a['catalog']}/{a['subcatalog']}) -- {a['root_count']} roots: {a['root_paths']}",
            app_id=a["app_id"],
        )
        for a in structural.spread_apps
    ]
    sections.append(ReportSection(
        "spread_apps", f"Apps spread across multiple scan roots ({len(spread_items)})",
        "info" if spread_items else "ok", spread_items,
        empty_text="No app's variants span more than one scan root.",
    ))
    dupcat_items = [
        ReportItem(f"'{s['subcatalog']}' appears under: {s['catalogs']}")
        for s in structural.duplicate_subcategory_names
    ]
    sections.append(ReportSection(
        "duplicate_subcategory_names", f"Subcategory names reused across catalogs ({len(dupcat_items)})",
        "info" if dupcat_items else "ok", dupcat_items,
        empty_text="No subcategory name is reused across different catalogs.",
    ))
    alias_items = [
        ReportItem(f"'{al['raw_name']}' -> '{al['display_name']}' (no app currently uses this)")
        for al in structural.unused_aliases
    ]
    sections.append(ReportSection(
        "unused_aliases", f"Unused folder-name aliases ({len(alias_items)})",
        "info" if alias_items else "ok", alias_items,
        empty_text="Every configured alias is in use.",
    ))

    # ---- Scan health: errors + stale roots -------------------------------
    scan_error_items = []
    if _table_exists(conn, "scan_errors"):
        try:
            err_rows = conn.execute("SELECT * FROM scan_errors ORDER BY rowid DESC").fetchall()
            for r in err_rows:
                d = dict(r)
                # Column names for this table weren't pinned down at the time
                # this report was written -- render whatever's there rather
                # than guessing/hardcoding keys that might not exist.
                path = d.get("folder_path") or d.get("path") or "?"
                msg = d.get("error_message") or d.get("error") or d.get("message") or ""
                scan_error_items.append(ReportItem(f"{path}: {msg}".rstrip(": ")))
        except Exception as exc:  # pragma: no cover -- defensive
            log.warning("Reading scan_errors failed during report generation: %s", exc)
    sections.append(ReportSection(
        "scan_errors", f"Folders the scanner couldn't read ({len(scan_error_items)})",
        "warning" if scan_error_items else "ok", scan_error_items,
        empty_text="No scan errors recorded.",
    ))

    cutoff = (datetime.now() - timedelta(days=stale_scan_days)).isoformat()
    stale_rows = conn.execute(
        """
        SELECT path, last_scan_finished_at, last_scan_status
        FROM scan_roots
        WHERE last_scan_finished_at IS NULL OR last_scan_finished_at < ?
        ORDER BY last_scan_finished_at IS NOT NULL, last_scan_finished_at
        """,
        (cutoff,),
    ).fetchall()
    stale_items = [
        ReportItem(
            f"{r['path']} -- " + (
                f"last scanned {r['last_scan_finished_at']}"
                if r["last_scan_finished_at"] else "never finished a scan"
            ) + (f" (status: {r['last_scan_status']})" if r["last_scan_status"] else "")
        )
        for r in stale_rows
    ]
    sections.append(ReportSection(
        "stale_scan_roots", f"Scan roots not refreshed in {stale_scan_days}+ days ({len(stale_items)})",
        "info" if stale_items else "ok", stale_items,
        empty_text=f"Every scan root has been scanned within the last {stale_scan_days} days.",
    ))

    return CatalogReport(generated_at=datetime.now().isoformat(timespec="seconds"), sections=sections)


def report_to_markdown(report: CatalogReport) -> str:
    """Renders a CatalogReport as a Markdown document, for export/copy."""
    lines = [f"# Catalog Report", f"_Generated {report.generated_at}_", ""]
    lines.append(f"**{report.warning_count} warning(s), {report.info_count} informational finding(s).**")
    lines.append("")
    for s in report.sections:
        marker = {"warning": "⚠️", "info": "ℹ️", "ok": "✅"}.get(s.severity, "")
        lines.append(f"## {marker} {s.title}")
        if not s.items:
            lines.append(f"_{s.empty_text}_")
        else:
            for item in s.items:
                lines.append(f"- {item.text}")
        lines.append("")
    return "\n".join(lines)


def report_to_csv_rows(report: CatalogReport) -> list[list[str]]:
    """Flattens a CatalogReport into rows suitable for csv.writer --
    one row per finding, plus the section title/severity for filtering
    in a spreadsheet."""
    rows = [["Section", "Severity", "App ID", "Finding"]]
    for s in report.sections:
        if not s.items:
            rows.append([s.title, s.severity, "", s.empty_text])
        for item in s.items:
            rows.append([s.title, s.severity, item.app_id or "", item.text])
    return rows
# ======================================================================
# 4. Physical file reorganization
# ======================================================================
#
# IMPORTANT SCHEMA NOTE (confirmed against the real database.py/resolver.py,
# corrected from an earlier draft of this module that got it wrong):
# variants.source_path is the INSTALL-UNIT FOLDER (raw_candidates.folder_path),
# NOT the installer file itself -- the file's own name lives separately in
# variants.file_name. See resolver.py's _upsert_variant()/INSERT INTO variants,
# which sets source_path = raw_row["folder_path"] and file_name =
# raw_row["primary_file_name"].
#
# That matters here because raw_candidates has UNIQUE(scan_root_id,
# folder_path, primary_file_name) -- i.e. ONE FOLDER CAN LEGITIMATELY HOLD
# MORE THAN ONE INSTALL UNIT (e.g. FreeFileSync shipping a portable build
# and a regular installer side by side; see PROGRESS.md Checkpoint 8).
# When that happens, two variants share the exact same source_path. Moving
# "the whole folder" for one of them would silently drag the other
# variant's installer along with it and then break its plan. This module
# handles that (rare) case explicitly -- see "shared folders" below.
#
# Four things added on top of the original design, after comparing
# behavior against app_manager_old_version.py (the DeepSeek build this
# feature was originally adapted from):
#
#   1. PORTABLE ROUTING -- an app carrying the "Portable" tag (applied by
#      resolver.py's _sync_app_tags() whenever any of its variants was
#      detected as a portable/paf build) is filed under a dedicated
#      top-level "Portable" catalog instead of its normal catalog, with
#      its normal catalog demoted to the subcatalog underneath (so a
#      portable graphics tool ends up at Portable/Graphics/App/Version
#      instead of Graphics/App/Version). Note the tag -- and therefore
#      this routing -- is per-APP, not per-variant: if only one of an
#      app's several variants is a portable build, the whole app (all its
#      variants) still moves to Portable/, because that's the granularity
#      the schema actually tracks (variants has no is_portable column).
#   2. ARCHIVING -- the finished version folder can be compressed
#      (7z/zip/rar) instead of left as a loose folder. 7z uses the py7zr
#      package (pure Python, no external binary needed), imported lazily
#      so the app doesn't hard-depend on it, with an automatic fallback
#      to zip if it isn't installed; zip uses the stdlib; rar shells out
#      to a rar/WinRAR executable on PATH since Python has no library
#      that can WRITE the (proprietary) rar format, and fails gracefully
#      with a clear message if none is found rather than silently doing
#      nothing.
#   3. COPY VS MOVE -- caller-selectable, defaults to move (matching the
#      original behavior). On copy, the original is left in place and the
#      catalog's source_path is deliberately NOT repointed at the new
#      copy, since the original is still the live, valid location; only a
#      move updates the catalog.
#   4. SIDECAR FILES -- for the common case (a source_path folder used by
#      exactly one variant), this is automatic: the WHOLE folder moves as
#      one unit, so a readme/crack/keygen/theme/serial/etc sitting next to
#      the installer inside that same folder already travels with it,
#      no extra logic needed. Only the rare "shared folder" case (above)
#      needs special handling, since the folder can't be moved wholesale
#      for either variant without stepping on the other.

VALID_ARCHIVE_FORMATS = ("7z", "zip", "rar", "none")
VALID_COPY_MODES = ("move", "copy")

# ----------------------------------------------------------------------
# Archive password protection ("password in the filename")
# ----------------------------------------------------------------------
# When enabled, archives this module (and monitor.py) create are encrypted
# and the password is appended to the archive's base name in parentheses:
#     Setup.exe  ->  Setup(mypass).7z
# The password lives in the filename ON PURPOSE (so it's never forgotten
# and travels with the file), which means this is not real secrecy -- it
# keeps antivirus engines / previewers from opening the installer, that's
# all. Consequently the password must be usable as part of a filename:
# no characters Windows forbids in names, and no parentheses (they'd make
# "(...)" ambiguous to read back out of the name).
_PASSWORD_FORBIDDEN_CHARS = set('\\/:*?"<>|()')
MAX_ARCHIVE_PASSWORD_LEN = 64


def validate_archive_password(password: Optional[str]) -> Optional[str]:
    """Returns None if `password` can be used (and embedded in a filename),
    otherwise a short human-readable reason."""
    if password is None or password == "":
        return "The password is empty."
    if password != password.strip():
        return "The password can't start or end with a space."
    if len(password) > MAX_ARCHIVE_PASSWORD_LEN:
        return f"The password is longer than {MAX_ARCHIVE_PASSWORD_LEN} characters."
    bad = sorted({c for c in password if c in _PASSWORD_FORBIDDEN_CHARS or ord(c) < 32})
    if bad:
        shown = " ".join(c for c in bad if ord(c) >= 32) or "control characters"
        return (
            f"The password contains characters that can't appear in a filename "
            f"({shown}). It is stored in the archive's filename, so avoid "
            f"\\ / : * ? \" < > | ( )"
        )
    return None


def get_archive_password(settings: dict) -> Optional[str]:
    """The password to apply to newly created archives, or None when
    protection is off. Raises ValueError if protection is switched ON but
    the stored password is unusable -- callers must NOT quietly fall back
    to an unprotected archive in that case."""
    if not settings.get("archive_password_enabled", False):
        return None
    pw = settings.get("archive_password") or ""
    err = validate_archive_password(pw)
    if err:
        raise ValueError(f"Archive password protection is on but {err[0].lower() + err[1:]}")
    return pw


def with_password_in_name(stem: str, password: Optional[str]) -> str:
    """'Setup' + 'pw' -> 'Setup(pw)'. No-op without a password."""
    return f"{stem}({password})" if password else stem


def find_7z_executable() -> Optional[str]:
    for candidate in ("7z", "7za", "7z.exe"):
        exe = shutil.which(candidate)
        if exe:
            return exe
    return None


def write_zip_archive(src: str, dest: str, password: Optional[str] = None) -> None:
    """Writes `src` (one file) into a new zip at `dest`. Without a password
    this is the stdlib. With one, the stdlib can't encrypt, so it uses
    pyzipper (AES-256) if installed, else a 7z binary on PATH (also
    AES-256). Raises RuntimeError if neither exists -- deliberately never
    producing an UNENCRYPTED zip when a password was asked for."""
    arcname = os.path.basename(src)
    if not password:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(src, arcname)
        return
    try:
        import pyzipper
    except ImportError:
        pyzipper = None
    if pyzipper is not None:
        with pyzipper.AESZipFile(dest, "w", compression=pyzipper.ZIP_DEFLATED,
                                 encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(password.encode("utf-8"))
            zf.setencryption(pyzipper.WZ_AES, nbits=256)
            zf.write(src, arcname)
        return
    exe = find_7z_executable()
    if exe:
        subprocess.run([exe, "a", "-tzip", "-mem=AES256", f"-p{password}", dest, src],
                       check=True, capture_output=True, timeout=1800)
        return
    raise RuntimeError(
        "Password-protected zip needs the 'pyzipper' package (pip install pyzipper) "
        "or a 7z binary on PATH. Use 7z format instead, or install one of those."
    )

# Filenames never worth carrying into a reorganized destination even when
# found alongside a shared-folder installer (OS/filesystem bookkeeping
# files, not anything the app or its extras actually need).
_JUNK_FILENAMES = {"thumbs.db", "desktop.ini", ".ds_store"}

# Fallback used when the "monitor_already_compressed_extensions" setting
# (shared with monitor.py's own already-compressed handling, see
# gui_main.py's Monitor tab) is unset/empty -- a bare installer (.exe,
# .msi, ...) is worth wrapping in an archive; something that's already
# .zip/.rar/.7z/etc isn't, since compressing an already-compressed file
# again just burns CPU for no space savings and no benefit.
DEFAULT_ALREADY_COMPRESSED_EXTENSIONS = [
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso",
]


def _is_already_compressed(file_name: Optional[str], already_compressed_exts: set) -> bool:
    """True if file_name's extension is one of the "already compressed"
    formats -- used to skip archiving a version that's already a single
    packaged file, rather than a bare installer. A file_name we can't
    determine (None/empty, e.g. an app_manager_old_version edge case with
    no primary_file_name on record) is treated as NOT already compressed,
    the safer default that still archives it as configured."""
    if not file_name:
        return False
    return os.path.splitext(file_name)[1].lower() in already_compressed_exts


@dataclass
class PlannedMove:
    app_id: int
    variant_id: int
    source_path: str            # the install-unit FOLDER (see module note above)
    primary_file_name: Optional[str]   # this variant's specific file within source_path, if known
    dest_path: str
    collision: bool = False      # dest_path folder already existed at preview time (informational --
                                  # final collision handling happens per-item at execute time)
    is_portable: bool = False
    shared_folder: bool = False  # True if the folder is used by several variants OR contains other
                                  # install units (see container_folder) -- per-file handling, never
                                  # a whole-folder move
    shared_extra_paths: list = field(default_factory=list)  # shared_folder plans only: files AND
                                  # folders (patch, crack, keygen, skins, tutorial, ...) not claimed
                                  # by any variant, copied along as sidecars. Never a folder that
                                  # is, or contains, ANOTHER variant's install unit.
    app_name: str = ""           # display-only, for logs/reports
    version: str = ""            # display-only, ditto
    # ---- size / risk information, computed at preview time -------------
    container_folder: bool = False   # source_path CONTAINS other variants' folders (e.g. the scan
                                     # root holding loose installers + app subfolders)
    own_bytes: int = 0           # size of this variant's own file (shared plans) or of the whole
                                 # folder (ordinary plans)
    extras_bytes: int = 0        # size of shared_extra_paths -- these are always COPIED
    cross_volume: bool = False   # source and destination are on different volumes, so a "move"
                                 # is a real copy + delete, not an instant rename
    skipped_items: list = field(default_factory=list)  # [(path, reason)] left in place on purpose:
                                 # another variant's install unit, or larger than the sidecar cap


def plan_bytes_to_write(plan: "PlannedMove", copy_mode: str) -> int:
    """How many bytes execute_reorganize() will physically WRITE for this plan.
    A same-volume move is a rename (0 bytes); extras are always copied."""
    if copy_mode == "copy" or plan.cross_volume:
        return plan.own_bytes + plan.extras_bytes
    return plan.extras_bytes


@dataclass
class ReorganizeResult:
    moved: int = 0
    archived: int = 0
    skipped_collisions: int = 0
    failed: list[dict] = field(default_factory=list)
    move_log_path: Optional[str] = None
    entries: list[dict] = field(default_factory=list)
    html_report_path: Optional[str] = None
    cancelled: bool = False
    not_started: int = 0
    bytes_copied: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class ReorgEvent:
    """One progress notification from execute_reorganize(). `final` is True
    exactly once per plan (its outcome is in `status`); every other event is
    a live update about what is happening RIGHT NOW."""
    index: int
    total: int
    plan: "PlannedMove"
    phase: str                  # start | measure | move | copy | archive | cleanup | finish
    message: str = ""           # one human sentence: "Copying extra file: setup.zip"
    detail: str = ""            # numbers: "3.2 GB / 18.4 GB - 17% - 45 MB/s - ETA 5m 10s"
    bytes_done: int = 0
    bytes_total: int = 0        # 0 = unknown -> show a busy indicator, not a percentage
    speed: float = 0.0          # bytes/second for the current operation
    bytes_copied_total: int = 0 # whole run so far
    final: bool = False
    status: str = ""            # moved | copied | failed | skipped_collision | cancelled


class ReorganizeCancelled(Exception):
    """Raised inside a running copy when the user pressed Cancel."""


def _safe_path_component(name: str) -> str:
    """Strips characters that are invalid in Windows path components."""
    cleaned = "".join(c for c in (name or "") if c not in '<>:"/\\|?*').strip()
    return cleaned or "Unnamed"


# ---------------------------------------------------------------------
# Progress plumbing (shared by move / copy / archive)
# ---------------------------------------------------------------------

_COPY_CHUNK = 4 * 1024 * 1024      # 4 MB read/write blocks -> progress + cancel granularity
_UI_INTERVAL = 0.2                 # seconds between GUI updates for one running operation
_LOG_INTERVAL = 5.0                # seconds between console heartbeat lines

# Only BARE installer files are ever archived. Everything else (docs, isos,
# scripts, already-packaged .zip/.rar/.7z, ...) is left exactly as it is.
DEFAULT_BARE_INSTALLER_EXTENSIONS = [
    ".exe", ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle", ".msp",
]


def _fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _path_size(path: str) -> int:
    """Total size in bytes of a file or a whole folder tree (symlinks not followed)."""
    try:
        if os.path.islink(path) or os.path.isfile(path):
            return os.lstat(path).st_size
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _nearest_existing(path: str) -> str:
    cur = os.path.abspath(path)
    while cur and not os.path.exists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return cur


def _same_volume(a: str, b: str) -> bool:
    try:
        return os.stat(_nearest_existing(a)).st_dev == os.stat(_nearest_existing(b)).st_dev
    except OSError:
        return True   # can't tell -> don't cry wolf


fmt_bytes = _fmt_bytes          # public aliases for the GUI
fmt_duration = _fmt_duration


class _Progress:
    """Turns low-level byte counts into ReorgEvent callbacks (throttled) and
    console heartbeat lines, and carries the cancel flag."""

    def __init__(self, callback, total, cancel_event):
        self.callback = callback
        self.total = total
        self.cancel_event = cancel_event
        self.index = 0
        self.plan = None
        self.bytes_copied_total = 0

    def begin(self, index, plan):
        self.index, self.plan = index, plan

    def cancelled(self) -> bool:
        return bool(self.cancel_event is not None and self.cancel_event.is_set())

    def check_cancel(self):
        if self.cancelled():
            raise ReorganizeCancelled()

    def emit(self, phase, message="", detail="", done=0, total=0, speed=0.0,
             final=False, status=""):
        if self.callback is None:
            return
        ev = ReorgEvent(
            index=self.index, total=self.total, plan=self.plan, phase=phase,
            message=message, detail=detail, bytes_done=int(done), bytes_total=int(total),
            speed=speed, bytes_copied_total=self.bytes_copied_total,
            final=final, status=status,
        )
        try:
            self.callback(ev)
        except Exception:   # a misbehaving GUI callback must never abort file operations
            log.exception("progress callback raised -- ignoring")

    def op(self, phase, message, total_bytes, counts_as_copy, detail_extra=""):
        return _Op(self, phase, message, total_bytes, counts_as_copy, detail_extra)


class _Op:
    """One long-running operation (copying one file/folder, compressing one file)."""

    def __init__(self, prog, phase, message, total_bytes, counts_as_copy, detail_extra=""):
        self.prog, self.phase, self.message = prog, phase, message
        self.total = int(total_bytes or 0)
        self.counts = counts_as_copy
        self.detail_extra = detail_extra
        self.done = 0
        self.start = time.monotonic()
        self._last_ui = 0.0
        self._last_log = self.start
        prog.emit(phase, message, self._detail(0.0), 0, self.total)
        log_job.info("[%d/%d] %s%s", prog.index, prog.total, message,
                     f" ({_fmt_bytes(self.total)})" if self.total else "")

    def _detail(self, speed):
        parts = []
        if self.total:
            pct = 100.0 * self.done / self.total if self.total else 0
            parts.append(f"{_fmt_bytes(self.done)} / {_fmt_bytes(self.total)} - {pct:.0f}%")
        else:
            parts.append(f"{_fmt_bytes(self.done)} written so far")
        if speed > 0:
            parts.append(f"{_fmt_bytes(speed)}/s")
            if self.total and self.done < self.total:
                parts.append(f"ETA {_fmt_duration((self.total - self.done) / speed)}")
        if self.detail_extra:
            parts.append(self.detail_extra)
        return " - ".join(parts)

    def add(self, n):
        self.done += n
        if self.counts:
            self.prog.bytes_copied_total += n
        self.tick()
        self.prog.check_cancel()

    def set_done(self, n):          # used by the archive size poller (another thread)
        self.done = n
        self.tick()

    def tick(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_ui < _UI_INTERVAL:
            return
        self._last_ui = now
        elapsed = max(now - self.start, 1e-6)
        speed = self.done / elapsed
        detail = self._detail(speed)
        self.prog.emit(self.phase, self.message, detail, self.done, self.total, speed)
        if now - self._last_log >= _LOG_INTERVAL:
            self._last_log = now
            log_job.info("[%d/%d] %s: %s", self.prog.index, self.prog.total, self.message, detail)

    def finish(self):
        self.tick(force=True)
        elapsed = time.monotonic() - self.start
        rate = f" ({_fmt_bytes(self.done / elapsed)}/s)" if elapsed > 1 and self.done else ""
        log_job.info("[%d/%d] finished: %s -- %s in %s%s", self.prog.index, self.prog.total,
                     self.message, _fmt_bytes(self.done), _fmt_duration(elapsed), rate)


# ---------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------

DEFAULT_MAX_SIDECAR_GB = 5.0     # setting "reorganize_max_sidecar_gb"; 0 = no limit


def preview_reorganize(
    db: Database, dest_root: str, *,
    portable_to_dedicated_category: bool = True,
    portable_category_name: str = "Portable",
    max_sidecar_gb: Optional[float] = None,
) -> list[PlannedMove]:
    """
    DRY RUN ONLY -- computes where every variant's install unit WOULD go under
    dest_root/Catalog/Subcatalog/AppName/Version/ (or
    dest_root/Portable/OriginalCatalog/AppName/Version/ for portable-tagged
    apps) and how much data each row will really write, but moves nothing.

    Two kinds of source folder:

      ORDINARY   used by exactly one variant and containing no other install
                 unit: the WHOLE folder moves as one unit, so everything next
                 to the installer (readme, crack, keygen, serial, skins,
                 tutorials, plugins ...) travels with it.

      SHARED     several variants point at the same folder, OR the folder
                 CONTAINS other variants' folders (typically the scan root with
                 loose installers beside app sub-folders). Handled per file:
                 the variant's own file moves and everything else there that
                 belongs to nobody -- files AND folders such as Patch/, Crack/,
                 Keygen/, Skins/ -- is COPIED alongside it as a sidecar.

    What is never a sidecar (it is left where it is and listed in
    PlannedMove.skipped_items):
      * a folder that IS, or CONTAINS, any variant's install unit -- that is
        another app, moved by its own plan. Copying it into every sibling
        was the bug that turned 40 GB into 70+ GB. Ignored variants count too.
      * a file that any variant (ignored or not) claims as its own installer.
      * anything bigger than the sidecar cap (default 5 GB per item, setting
        "reorganize_max_sidecar_gb", 0 = unlimited): an unattributed
        multi-GB item is far more likely to be an uncatalogued app than a
        patch, and it would be duplicated once per sibling. Raise the cap if
        that is really an extras folder.
    """
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT v.id AS variant_id, v.app_id, v.source_path, v.file_name, v.version,
               a.name AS app_name, a.catalog, a.subcatalog
        FROM variants v JOIN apps a ON a.id = v.app_id
        WHERE v.is_ignored = 0
        ORDER BY a.catalog, a.subcatalog, a.name, v.version
        """
    ).fetchall()
    # EVERY variant, ignored or not: they are all "somebody's" install unit/file.
    everyone = conn.execute("SELECT source_path, file_name FROM variants").fetchall()

    if max_sidecar_gb is None:
        try:
            max_sidecar_gb = float(db.get_all_settings().get("reorganize_max_sidecar_gb",
                                                             DEFAULT_MAX_SIDECAR_GB))
        except (TypeError, ValueError):
            max_sidecar_gb = DEFAULT_MAX_SIDECAR_GB
    cap_bytes = int(max_sidecar_gb * 1024 ** 3) if max_sidecar_gb and max_sidecar_gb > 0 else 0

    folder_counts: dict = {}
    for r in rows:
        folder_counts[r["source_path"]] = folder_counts.get(r["source_path"], 0) + 1

    all_unit_paths = {_norm(r["source_path"]) for r in everyone if r["source_path"]}
    claimed_names: dict = {}      # folder -> file names claimed by ANY variant
    for r in everyone:
        if r["source_path"] and r["file_name"]:
            claimed_names.setdefault(_norm(r["source_path"]), set()).add(r["file_name"].lower())

    _contains_cache: dict = {}

    def _contains_unit(path: str) -> bool:
        """True if any variant's folder is `path` itself or lives inside it."""
        if path not in _contains_cache:
            n = _norm(path).rstrip(os.sep)
            prefix = n + os.sep
            _contains_cache[path] = n in all_unit_paths or any(p.startswith(prefix) for p in all_unit_paths)
        return _contains_cache[path]

    def _is_container(path: str) -> bool:
        n = _norm(path).rstrip(os.sep) + os.sep
        return any(p.startswith(n) for p in all_unit_paths)

    portable_app_ids = set()
    if portable_to_dedicated_category:
        tag_rows = conn.execute(
            """SELECT at.app_id FROM app_tags at JOIN tags t ON t.id = at.tag_id
               WHERE LOWER(t.name) = 'portable'"""
        ).fetchall()
        portable_app_ids = {r["app_id"] for r in tag_rows}

    _size_cache: dict = {}

    def _size(path: str) -> int:
        if path not in _size_cache:
            _size_cache[path] = _path_size(path)
        return _size_cache[path]

    plans = []
    for r in rows:
        sp = r["source_path"]
        is_portable = r["app_id"] in portable_app_ids
        catalog_raw = r["catalog"] or "Uncategorized"
        subcatalog_raw = r["subcatalog"] or "Misc"
        if is_portable and catalog_raw != portable_category_name:
            catalog_raw, subcatalog_raw = portable_category_name, catalog_raw

        dest = os.path.join(
            dest_root, _safe_path_component(catalog_raw), _safe_path_component(subcatalog_raw),
            _safe_path_component(r["app_name"]), _safe_path_component(r["version"] or "unknown-version"),
        )

        container = _is_container(sp)
        shared = folder_counts[sp] > 1 or container
        extras, skipped, extras_bytes, own_bytes = [], [], 0, 0

        if shared:
            claimed = claimed_names.get(_norm(sp), set())
            try:
                names = os.listdir(sp)
            except OSError:
                names = []
            for name in names:
                full = os.path.join(sp, name)
                if (name.lower() in claimed or name.lower() in _JUNK_FILENAMES
                        or app_manifest.is_manifest_filename(name)):
                    continue       # (manifests are rewritten per variant after the move)
                if os.path.isdir(full) and not os.path.islink(full) and _contains_unit(full):
                    skipped.append((full, "another app's install folder (moved by its own entry)"))
                    continue
                size = _size(full)
                if cap_bytes and size > cap_bytes:
                    skipped.append((full, f"{_fmt_bytes(size)} is over the {max_sidecar_gb:g} GB sidecar limit"))
                    continue
                extras.append(full)
                extras_bytes += size
            if r["file_name"]:
                own_bytes = _size(os.path.join(sp, r["file_name"]))
        else:
            own_bytes = _size(sp)

        plans.append(PlannedMove(
            app_id=r["app_id"], variant_id=r["variant_id"],
            source_path=sp, primary_file_name=r["file_name"],
            dest_path=dest, collision=os.path.exists(dest),
            is_portable=is_portable, shared_folder=shared,
            shared_extra_paths=extras,
            app_name=r["app_name"] or "", version=r["version"] or "",
            container_folder=container, own_bytes=own_bytes, extras_bytes=extras_bytes,
            cross_volume=not _same_volume(sp, dest), skipped_items=skipped,
        ))
    return plans


# ---------------------------------------------------------------------
# Chunked copy (progress + cancel) and transfer
# ---------------------------------------------------------------------

def _copy_file_chunked(src: str, dst: str, op: _Op) -> None:
    if os.path.islink(src):
        os.symlink(os.readlink(src), dst)
        return
    with open(src, "rb") as fin, open(dst, "xb") as fout:   # "x": never overwrite
        while True:
            buf = fin.read(_COPY_CHUNK)
            if not buf:
                break
            fout.write(buf)
            op.add(len(buf))
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass


def _copy_tree_chunked(src: str, dst: str, op: _Op) -> None:
    os.makedirs(dst)
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        for d in dirs:
            os.makedirs(os.path.join(target_root, d), exist_ok=True)
        for f in files:
            _copy_file_chunked(os.path.join(root, f), os.path.join(target_root, f), op)


def _remove_partial(path: str) -> None:
    """Delete something WE were in the middle of creating (it did not exist before)."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)
    except OSError as e:
        log.warning("Could not remove partial copy %s: %s", path, e)


def _transfer_item(item: str, dest_dir: str, copy_mode: str, prog: _Progress) -> tuple:
    """
    Moves or copies one file/folder into dest_dir. Returns (transferred, collided).
    Never overwrites. A same-volume move is an instant rename; a copy (or a
    cross-volume move) is chunked with live progress and is cancellable --
    a half-written copy is deleted again, the original is never touched until
    the copy is complete.
    """
    if not os.path.lexists(item):
        return False, False
    name = os.path.basename(item)
    dest_item = os.path.join(dest_dir, name)
    if os.path.lexists(dest_item):
        return False, True

    if copy_mode == "move":
        try:
            prog.emit("move", f"Moving (rename): {name}")
            os.rename(item, dest_item)
            log_job.info("[%d/%d] renamed %s", prog.index, prog.total, name)
            return True, False
        except OSError as e:
            cross = getattr(e, "errno", None) == errno.EXDEV or getattr(e, "winerror", None) == 17
            if not cross:
                raise      # locked file / permission problem: report it, don't silently duplicate
            log_job.info("[%d/%d] %s is on another volume -> copy, then delete original",
                         prog.index, prog.total, name)

    verb = "Copying" if copy_mode == "copy" else "Moving across volumes"
    prog.emit("measure", f"Measuring {name} ...")
    total = _path_size(item)
    op = prog.op("copy", f"{verb}: {name}", total, counts_as_copy=True)
    try:
        if os.path.isdir(item) and not os.path.islink(item):
            _copy_tree_chunked(item, dest_item, op)
        else:
            _copy_file_chunked(item, dest_item, op)
    except BaseException:
        _remove_partial(dest_item)      # ReorganizeCancelled, OSError, KeyboardInterrupt...
        raise
    op.finish()

    if copy_mode == "move":             # only after a COMPLETE copy
        prog.emit("cleanup", f"Removing original: {name}")
        if os.path.isdir(item) and not os.path.islink(item):
            shutil.rmtree(item)
        else:
            os.remove(item)
    return True, False


# ---------------------------------------------------------------------
# Archiving (one bare installer file only)
# ---------------------------------------------------------------------

class _SizePoller:
    """Reports the growing size of an archive being written by a library that has
    no progress hook of its own (py7zr, rar.exe)."""

    def __init__(self, path, op, interval=0.5):
        self.path, self.op, self.interval = path, op, interval
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.op.set_done(os.path.getsize(self.path))
            except OSError:
                pass

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=2)


def _archive_single_file(file_path: str, archive_format: str,
                         password: Optional[str] = None,
                         prog: Optional[_Progress] = None) -> Optional[str]:
    """
    Compresses ONE file into an archive of the same base name next to it
    (Setup.exe -> Setup.7z, or Setup(password).7z when `password` is given),
    then removes the original. Nothing else in the folder is touched.
    Returns the archive path, or None if nothing was archived -- on ANY failure
    (or cancel) the partial archive is deleted and the original is left exactly
    as it was. A requested password is never silently dropped.
    """
    if archive_format == "none":
        return None
    if archive_format not in VALID_ARCHIVE_FORMATS:
        log.warning("Unknown archive_format %r -- leaving %s as-is", archive_format, file_path)
        return None
    if password:
        err = validate_archive_password(password)
        if err:
            log.error("Not archiving %s: %s", file_path, err)
            return None

    base, _ext = os.path.splitext(file_path)
    base = with_password_in_name(base, password)
    target = f"{base}.{archive_format}"
    if os.path.exists(target):
        log.warning("Archive target %s already exists -- leaving %s uncompressed", target, file_path)
        return None

    name = os.path.basename(file_path)
    size_in = os.path.getsize(file_path)
    prog = prog or _Progress(None, 0, None)
    pw_note = ", password-protected" if password else ""
    ok = False
    try:
        if archive_format == "7z":
            try:
                import py7zr
            except ImportError:
                log.warning("py7zr is not installed -- falling back to zip for %s", file_path)
                return _archive_single_file(file_path, "zip", password, prog)
            op = prog.op("archive", f"Compressing to 7z{pw_note}: {name}", 0, False,
                         detail_extra=f"input {_fmt_bytes(size_in)}; 7z reports no % -- watching output size")
            with _SizePoller(target, op):
                with py7zr.SevenZipFile(target, "w", password=password or None) as archive:
                    archive.write(file_path, arcname=name)
            op.finish()

        elif archive_format == "zip":
            if not password:      # plain zip: chunked, real percentage, cancellable
                op = prog.op("archive", f"Compressing to zip: {name}", size_in, False)
                info = zipfile.ZipInfo.from_file(file_path, name)
                info.compress_type = zipfile.ZIP_DEFLATED
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf, \
                        open(file_path, "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                    while True:
                        buf = src.read(_COPY_CHUNK)
                        if not buf:
                            break
                        dst.write(buf)
                        op.add(len(buf))
            else:                 # encrypted zip is written by pyzipper / 7z: no progress hook
                op = prog.op("archive", f"Compressing to zip{pw_note}: {name}", 0, False,
                             detail_extra=f"input {_fmt_bytes(size_in)}")
                with _SizePoller(target, op):
                    write_zip_archive(file_path, target, password)
            op.finish()

        elif archive_format == "rar":
            cmd = ["rar", "a", "-ep1"]
            if password:
                cmd.append(f"-p{password}")
            cmd += [target, file_path]
            op = prog.op("archive", f"Compressing to rar{pw_note}: {name}", 0, False,
                         detail_extra=f"input {_fmt_bytes(size_in)}")
            try:
                with _SizePoller(target, op):
                    proc = subprocess.run(cmd, capture_output=True, text=True,
                                          encoding="utf-8", errors="replace")
            except FileNotFoundError:
                log.warning("No 'rar' executable found on PATH -- leaving %s uncompressed.", file_path)
                return None
            if proc.returncode != 0:
                log.error("RAR archiving failed for %s: %s", file_path, proc.stderr)
                return None
            op.finish()
        ok = True
    except ReorganizeCancelled:
        raise
    except Exception as e:      # OSError, RuntimeError (no zip-encrypt backend), py7zr/7z errors
        log.error("Archiving %s to %s%s failed: %s", file_path, archive_format, pw_note, e)
        return None
    finally:
        if not ok:
            _remove_partial(target)

    try:
        os.remove(file_path)
    except OSError as e:
        log.warning("Archived %s to %s but could not remove the original file: %s", file_path, target, e)
    return target


def _cleanup_empty_ancestors(start_dir: str, max_levels: int = 8) -> None:
    """
    After a version folder is emptied out and removed, its parent (an
    app's folder with no versions left in it) and sometimes its
    grandparent (a subcatalog/catalog folder with no apps left in it)
    can end up empty too. Climbs upward from start_dir removing each
    folder in turn for as long as it's completely empty, stopping the
    moment a folder still has something in it (a sibling app/version
    that hasn't been moved yet, in the common case where this runs
    partway through a larger batch).

    Safe by construction: os.rmdir() only ever succeeds on a directory
    that's genuinely empty, so this can never remove something still in
    use. max_levels is just a sanity cap against an unexpectedly deep
    tree; hitting a permission error (e.g. a drive root) or a directory
    that no longer exists simply stops the climb.
    """
    current = start_dir
    for _ in range(max_levels):
        if not current or not os.path.isdir(current):
            return
        try:
            if os.listdir(current):
                return
            parent = os.path.dirname(current)
            os.rmdir(current)
        except OSError:
            return
        if not parent or parent == current:
            return
        current = parent


def execute_reorganize(*args, **kwargs) -> "ReorganizeResult":
    """See _execute_reorganize_impl(). Wrapper only: it holds the manifest
    auto-flusher back while files are moving, then flushes once at the end."""
    db = kwargs["db"] if "db" in kwargs else args[0]
    with app_manifest.suspend_auto_flush():
        result = _execute_reorganize_impl(*args, **kwargs)
    try:
        app_manifest.flush_dirty(db)
    except Exception as e:
        log.warning("manifest flush after reorganize failed: %s", e)
    return result


def _write_reorg_manifest(db: Database, plan: "PlannedMove", copy_mode: str,
                          new_file_name: Optional[str], orig_fp: Optional[str]) -> str:
    """Write the variant manifest at the destination after a successful
    move/copy. Returns the write status (for the move log)."""
    archived = bool(plan.primary_file_name and new_file_name
                    and new_file_name != plan.primary_file_name)
    orig_name = plan.primary_file_name if archived else None
    if copy_mode == "move":
        # the DB row already points at the destination
        res = app_manifest.write_variant_manifest(
            db, plan.variant_id, force=True, orig_name=orig_name, orig_fp=orig_fp)
    else:
        # copy mode: the variant stays at its source in the DB; the copy at the
        # destination still gets its own manifest
        res = app_manifest.write_variant_manifest(
            db, plan.variant_id, force=True, folder=plan.dest_path,
            file_name=new_file_name, record=False, orig_name=orig_name, orig_fp=orig_fp)
    return res.status if res.status != "failed" else f"failed: {res.message}"


def _execute_reorganize_impl(
    db: Database, planned_moves: list[PlannedMove], *,
    copy_mode: str = "move",
    archive_format: str = "none",
    archive_password: Optional[str] = None,
    move_log_dir: Optional[str] = None,
    progress_callback: Optional[Callable[[ReorgEvent], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> ReorganizeResult:
    """
    Executes EXACTLY the plan handed in (from preview_reorganize()).

    progress_callback(ReorgEvent) is called continuously -- when an item starts,
    at every phase change (move / copy / archive / cleanup) and about five times
    a second while bytes are being copied -- and once more with final=True when
    the item is done. It may be called from a helper thread (archive size
    polling), so a GUI must marshal it onto its own thread (Qt signals do).

    cancel_event: set it to stop. A running copy stops within a few MB and its
    half-written copy is deleted; the original is never touched until a copy is
    complete. A running 7z/rar compression can't be interrupted mid-file, so a
    cancel takes effect when that one file is finished.

    Handling per plan:
      ORDINARY plan   every item of the folder is moved (same-volume: instant
                      rename; cross-volume or copy mode: chunked copy).
      SHARED / CONTAINER plan (shared_folder=True)
                      only this variant's own file moves; its loose sidecar FILES
                      are copied. Sub-folders are never touched (see
                      preview_reorganize()).
    Only a bare installer (.exe/.msi/... -- setting
    "reorganize_bare_installer_extensions") is ever archived, and only that one
    file, never the folder.
    Every plan is isolated: one failure is recorded, the batch continues.
    A JSON move log is flushed before and after every plan.
    """
    if copy_mode not in VALID_COPY_MODES:
        raise ValueError(f"copy_mode must be one of {VALID_COPY_MODES}, got {copy_mode!r}")
    if archive_format not in VALID_ARCHIVE_FORMATS:
        raise ValueError(f"archive_format must be one of {VALID_ARCHIVE_FORMATS}, got {archive_format!r}")

    if archive_format == "none":
        archive_password = None       # a password only means something when archiving
    elif archive_password:
        _pw_err = validate_archive_password(archive_password)
        if _pw_err:
            raise ValueError(f"Invalid archive password: {_pw_err}")

    conn = db.connect()
    result = ReorganizeResult()
    total = len(planned_moves)
    t_run = time.monotonic()
    prog = _Progress(progress_callback, total, cancel_event)

    settings = db.get_all_settings()
    bare_exts = {
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in (settings.get("reorganize_bare_installer_extensions") or DEFAULT_BARE_INSTALLER_EXTENSIONS)
    }

    log_dir = Path(move_log_dir) if move_log_dir else get_logs_dir(db.path)
    run_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = log_dir / f"reorganize_log_{run_stamp}.json"
    result.move_log_path = str(log_path)
    log_entries = []

    def _flush_log():
        try:
            log_path.write_text(json.dumps(log_entries, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("Could not write reorganize move log to %s: %s", log_path, e)

    to_write = sum(plan_bytes_to_write(p, copy_mode) for p in planned_moves)
    log.info("REORGANIZE STARTING: %d item(s) (mode=%s, archive=%s) -- about %s will be physically written",
             total, copy_mode, archive_format, _fmt_bytes(to_write))

    def _finish(entry, plan, status, message):
        entry["status"] = status
        prog.emit("finish", message, final=True, status=status)
        _flush_log()

    for i, plan in enumerate(planned_moves, start=1):
        if prog.cancelled():
            break
        prog.begin(i, plan)
        entry = {
            "variant_id": plan.variant_id, "app_id": plan.app_id,
            "app_name": plan.app_name, "version": plan.version,
            "source": plan.source_path, "dest": plan.dest_path,
            "shared_folder": plan.shared_folder, "container_folder": plan.container_folder,
            "sidecars_copied": len(plan.shared_extra_paths),
            "copy_mode": copy_mode, "archive_format": archive_format,
            "timestamp": datetime.now().isoformat(), "status": "pending",
        }
        log_entries.append(entry)
        _flush_log()

        label = f"{plan.app_name} {plan.version}".strip() or os.path.basename(plan.source_path)
        log_job.info("[%d/%d] STARTING: %s  (%s)%s", i, total, label, plan.source_path,
                     "  [shared/container folder: own file only]" if plan.shared_folder else "")
        prog.emit("start", f"Starting: {label}", detail=f"{plan.source_path}  ->  {plan.dest_path}")

        if not os.path.exists(plan.source_path):
            entry["error"] = "source folder no longer exists"
            result.failed.append({
                "variant_id": plan.variant_id, "app_name": plan.app_name, "version": plan.version,
                "source": plan.source_path, "dest": plan.dest_path, "error": entry["error"],
            })
            log_job.error("[%d/%d] FAILED: %s -- source folder no longer exists", i, total, plan.source_path)
            _finish(entry, plan, "failed", entry["error"])
            continue

        try:
            os.makedirs(plan.dest_path, exist_ok=True)
            item_collisions = []
            moved_any = False

            if plan.shared_folder:
                if plan.primary_file_name:
                    own = os.path.join(plan.source_path, plan.primary_file_name)
                    transferred, collided = _transfer_item(own, plan.dest_path, copy_mode, prog)
                    moved_any = moved_any or transferred
                    if collided:
                        item_collisions.append(own)
                for item in plan.shared_extra_paths:
                    transferred, collided = _transfer_item(item, plan.dest_path, "copy", prog)
                    moved_any = moved_any or transferred
                    if collided:
                        item_collisions.append(item)
                if plan.skipped_items:
                    entry["left_in_place"] = [{"path": p, "reason": why} for p, why in plan.skipped_items]
                    for p_, why in plan.skipped_items:
                        log_job.info("[%d/%d] left in place: %s -- %s", i, total, p_, why)
            else:
                names = os.listdir(plan.source_path)
                for n_idx, n in enumerate(names, start=1):
                    prog.emit("move", f"Item {n_idx}/{len(names)}: {n}")
                    transferred, collided = _transfer_item(
                        os.path.join(plan.source_path, n), plan.dest_path, copy_mode, prog)
                    moved_any = moved_any or transferred
                    if collided:
                        item_collisions.append(os.path.join(plan.source_path, n))

            if item_collisions and not moved_any:
                entry["colliding_items"] = item_collisions
                result.skipped_collisions += 1
                log_job.warning("[%d/%d] SKIPPED (destination already has this content): %s",
                                i, total, plan.source_path)
                _finish(entry, plan, "skipped_collision", "Skipped: destination already has this content")
                continue
            if item_collisions:
                entry["partial_collisions"] = item_collisions

            if copy_mode == "move" and not plan.shared_folder:
                try:
                    if os.path.exists(plan.source_path) and not os.listdir(plan.source_path):
                        os.rmdir(plan.source_path)
                        _cleanup_empty_ancestors(os.path.dirname(plan.source_path))
                except OSError as e:
                    log.warning("Could not remove empty source folder %s: %s", plan.source_path, e)

            new_file_name = plan.primary_file_name
            orig_fp = None
            is_bare = bool(plan.primary_file_name) and \
                os.path.splitext(plan.primary_file_name)[1].lower() in bare_exts
            if archive_format != "none":
                if is_bare:
                    installer_path = os.path.join(plan.dest_path, plan.primary_file_name)
                    if os.path.exists(installer_path):
                        orig_fp = app_manifest.quick_fingerprint(installer_path)
                        archived_path = _archive_single_file(installer_path, archive_format,
                                                             archive_password, prog)
                        if archived_path:
                            new_file_name = os.path.basename(archived_path)
                            result.archived += 1
                            entry["archive_path"] = archived_path
                            if archive_password:
                                entry["archive_password_protected"] = True
                        else:
                            entry["archive_failed"] = True
                    else:
                        log_job.warning("[%d/%d] installer not found at %s -- left as-is",
                                        i, total, installer_path)
                else:
                    log_job.info("[%d/%d] Not archiving -- %s is not a bare installer (%s)",
                                 i, total, plan.primary_file_name or "(no file name)",
                                 "/".join(sorted(bare_exts)))
                    entry["archive_skipped_already_compressed"] = True

            if copy_mode == "move":
                conn.execute(
                    "UPDATE variants SET source_path = ?, file_name = ?, updated_at = datetime('now') "
                    "WHERE id = ?", (plan.dest_path, new_file_name, plan.variant_id))
                conn.commit()

            # variant manifest next to the files (never allowed to fail the move)
            try:
                entry["manifest"] = _write_reorg_manifest(db, plan, copy_mode, new_file_name, orig_fp)
            except Exception as e:
                entry["manifest"] = f"failed: {type(e).__name__}: {e}"
                log.warning("manifest write failed after reorganize of %s: %s", plan.dest_path, e)

            result.moved += 1
            log_job.info("[%d/%d] %s OK: %s -> %s", i, total, copy_mode.upper(),
                         plan.source_path, plan.dest_path)
            _finish(entry, plan, "moved" if copy_mode == "move" else "copied",
                    "Done" + (" (archived)" if entry.get("archive_path") else ""))

        except ReorganizeCancelled:
            entry["error"] = "cancelled by user"
            result.cancelled = True
            log_job.warning("[%d/%d] CANCELLED while processing %s (partial copy removed)",
                            i, total, plan.source_path)
            _finish(entry, plan, "cancelled", "Cancelled -- partial copy removed, original untouched")
            break
        except Exception as e:      # OSError and anything unexpected: isolate to this plan
            log_job.exception("[%d/%d] %s FAILED: %s -> %s", i, total, copy_mode.upper(),
                              plan.source_path, plan.dest_path)
            entry["error"] = f"{type(e).__name__}: {e}"
            result.failed.append({
                "variant_id": plan.variant_id, "app_name": plan.app_name, "version": plan.version,
                "source": plan.source_path, "dest": plan.dest_path, "error": entry["error"],
            })
            _finish(entry, plan, "failed", entry["error"])

    if result.cancelled or prog.cancelled():
        result.cancelled = True
        for plan in planned_moves[len(log_entries):]:
            log_entries.append({
                "variant_id": plan.variant_id, "app_id": plan.app_id, "app_name": plan.app_name,
                "version": plan.version, "source": plan.source_path, "dest": plan.dest_path,
                "status": "not_started",
            })
            result.not_started += 1
        _flush_log()

    result.entries = log_entries
    result.bytes_copied = prog.bytes_copied_total
    result.elapsed_seconds = time.monotonic() - t_run
    log.info("REORGANIZE %s: moved=%d archived=%d skipped=%d failed=%d not_started=%d copied=%s in %s",
             "CANCELLED" if result.cancelled else "FINISHED", result.moved, result.archived,
             result.skipped_collisions, len(result.failed), result.not_started,
             _fmt_bytes(result.bytes_copied), _fmt_duration(result.elapsed_seconds))

    try:
        result.html_report_path = generate_reorganize_html_report(
            result, copy_mode=copy_mode, archive_format=archive_format, report_dir=log_dir,
        )
    except OSError as e:
        log.warning("Could not write HTML reorganize report: %s", e)

    return result


def generate_reorganize_html_report(
    result: ReorganizeResult, *, copy_mode: str, archive_format: str,
    report_dir: Optional[Path] = None, title: str = "Reorganize Report",
) -> str:
    """
    Turns a ReorganizeResult's entries into a single self-contained HTML
    file (no external CSS/JS/fonts -- this app is offline-first, a report
    that needs the internet to render right would be a step backward) --
    a "Needs attention" section up top listing every failed/skipped item
    WITH ITS REASON (the JSON log always had this, but reading raw JSON
    to find out *why* something failed was the actual complaint this
    replaces), then a filterable/searchable "Everything" table below it
    covering every item regardless of outcome. Written next to the JSON
    move-log this run also produces (same directory, matching timestamp)
    and returns the path so a caller (the GUI) can open it automatically.
    """
    report_dir = Path(report_dir) if report_dir else (
        Path(os.path.dirname(result.move_log_path)) if result.move_log_path else Path(".")
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = report_dir / f"reorganize_report_{stamp}.html"
    successful = [e for e in result.entries if e.get("status") in ("moved", "copied")]

    from html_report import ReportRow, render_operation_html_report

    def _detail(e: dict) -> str:
        if e.get("status") == "failed":
            return e.get("error") or "Unknown error"
        if e.get("status") == "skipped_collision":
            items = e.get("colliding_items") or []
            return f"Destination already has this content ({len(items)} item(s))" if items else \
                "Destination already has this content"
        bits = []
        if e.get("partial_collisions"):
            bits.append(f"{len(e['partial_collisions'])} item(s) at the destination were left as-is (already there)")
        if e.get("archive_path"):
            bits.append(f"compressed to {os.path.basename(e['archive_path'])}"
                        + (" (password-protected)" if e.get("archive_password_protected") else ""))
        elif e.get("archive_failed"):
            bits.append("archiving failed -- installer left uncompressed (see log)")
        elif e.get("archive_skipped_already_compressed"):
            bits.append("not a bare installer (.exe/.msi/...), left uncompressed")
        if e.get("sidecars_copied"):
            bits.append(f"{e['sidecars_copied']} sidecar item(s) copied along")
        for it in e.get("left_in_place") or []:
            bits.append(f"LEFT IN PLACE {os.path.basename(it['path'])}: {it['reason']}")
        return "; ".join(bits)

    _STATUS_MAP = {
        "moved": ("Moved", "good"), "copied": ("Copied", "good"),
        "failed": ("Failed", "bad"), "skipped_collision": ("Skipped", "warn"),
        "pending": ("Interrupted", "bad"),  # never flushed past "pending" -- the run crashed/was killed
        "cancelled": ("Cancelled", "warn"), "not_started": ("Not started", "neutral"),
    }
    rows = [
        ReportRow(
            name=f"{e.get('app_name') or '(unknown app)'} {e.get('version') or ''}".strip(),
            source=e.get("source", ""), dest=e.get("dest", ""),
            status_label=_STATUS_MAP.get(e.get("status"), (e.get("status", "?"), "neutral"))[0],
            status_class=_STATUS_MAP.get(e.get("status"), (e.get("status", "?"), "neutral"))[1],
            detail=_detail(e),
        )
        for e in result.entries
    ]

    html = render_operation_html_report(
        title=title,
        subtitle=f"Mode: {copy_mode} &middot; Archive: {archive_format}",
        summary_cards=[
            ("Total planned", len(result.entries), "neutral"),
            ("Succeeded", len(successful), "good"),
            ("Archived", result.archived, "neutral"),
            ("Skipped (collision)", result.skipped_collisions, "warn" if result.skipped_collisions else "good"),
            ("Failed", len(result.failed), "bad" if result.failed else "good"),
            *([("Cancelled / not started", 1 + result.not_started, "warn")] if result.cancelled else []),
        ],
        rows=rows,
        json_log_path=result.move_log_path,
    )
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)

# ======================================================================
# 5. Clean library -- confirm existing variants still exist on disk
# ======================================================================
#
# Deliberately the OPPOSITE of a scan: a scan looks at the filesystem and
# asks "is there anything here I don't know about yet". This looks at
# the catalog and asks "does everything I already know about still
# physically exist" -- for apps that were deleted, moved out of the scan
# root, or replaced by hand outside this app entirely. Read-only until
# execute_clean_library() is explicitly called with a reviewed list.

@dataclass
class MissingItem:
    variant_id: int
    app_id: int
    app_name: str
    version: str
    source_path: str
    raw_candidate_id: Optional[int]
    reason: str = "file not found"


@dataclass
class CleanLibraryResult:
    checked: int = 0
    removed_variants: int = 0
    removed_apps: int = 0        # apps left with zero variants after removal, deleted outright
    move_log_path: Optional[str] = None
    html_report_path: Optional[str] = None


def _path_is_under(path: str, root: str) -> bool:
    """True if `path` is `root` itself or lives anywhere under it. Tolerant
    of the two sharing no common path at all (e.g. different drive letters
    on Windows) -- returns False rather than raising, since that's exactly
    the "this scan root isn't even the right drive" case this exists to
    handle gracefully."""
    try:
        path_n = os.path.normpath(os.path.abspath(path))
        root_n = os.path.normpath(os.path.abspath(root))
        return os.path.commonpath([path_n, root_n]) == root_n
    except ValueError:
        return False


def scan_for_missing_sources(
    db: Database, root_path: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    cancel_flag: Optional[Callable[[], bool]] = None,
) -> list[MissingItem]:
    """
    DRY RUN, read-only, never touches the DB or filesystem beyond
    os.path.exists()/os.path.isdir() checks. For each variant, in order:

      1. Its scan root is gone from scan_roots entirely (raw_candidate_id
         is NULL -- this happens once a root is removed via "Delete
         selected root", which deliberately leaves apps/variants in
         place but unlinks them). Flagged missing outright; there's no
         live root left to even check a path against.
      2. Its scan root IS still registered, but that root's own path
         isn't reachable right now (external/network drive unplugged,
         letter remounted elsewhere, etc). Flagged missing WITHOUT
         checking the individual file -- if the whole root is gone,
         everything under it is too, and this also sidesteps a slow or
         flaky per-file stat against a drive that isn't there.
      3. Otherwise, the normal per-file os.path.exists() check (the
         original behaviour): the file itself may have been deleted,
         moved, or replaced by hand even though its root is fine.

    Scoped to variants whose source_path currently lives under
    `root_path` when one is given (so running this from one scan root's
    row in ScanRootsDialog can't wrongly flag every OTHER root's apps as
    missing too); pass root_path=None to check the whole catalog
    regardless of which root each variant came from.
    """
    conn = db.connect()
    rows = conn.execute("""
        SELECT v.id AS variant_id, v.app_id, v.source_path, v.version,
               v.raw_candidate_id, a.name AS app_name, rc.scan_root_id
        FROM variants v
        JOIN apps a ON a.id = v.app_id
        LEFT JOIN raw_candidates rc ON rc.id = v.raw_candidate_id
        ORDER BY a.name, v.version
    """).fetchall()

    if root_path:
        rows = [r for r in rows if _path_is_under(r["source_path"], root_path)]

    # One reachability check per scan root (not per variant) -- cheap and
    # avoids repeatedly stat-ing a drive that isn't there.
    root_reachable: dict[int, bool] = {}
    for r in conn.execute("SELECT id, path FROM scan_roots").fetchall():
        root_reachable[r["id"]] = os.path.isdir(r["path"])

    missing: list[MissingItem] = []
    total = len(rows)
    for i, r in enumerate(rows, start=1):
        if cancel_flag and cancel_flag():
            break

        reason = None
        if r["raw_candidate_id"] is None:
            reason = "scan root no longer tracked"
        elif not root_reachable.get(r["scan_root_id"], True):
            reason = "scan root path unreachable"
        elif not os.path.exists(r["source_path"]):
            reason = "file not found"

        if reason:
            missing.append(MissingItem(
                variant_id=r["variant_id"], app_id=r["app_id"],
                app_name=r["app_name"] or "", version=r["version"] or "",
                source_path=r["source_path"], raw_candidate_id=r["raw_candidate_id"],
                reason=reason,
            ))
        if on_progress and (i % 25 == 0 or i == total):
            on_progress(i, total)
    return missing


def execute_clean_library(
    db: Database, missing_items: list["MissingItem"], *,
    move_log_dir: Optional[str] = None,
) -> CleanLibraryResult:
    """
    Removes the given (already user-reviewed and confirmed) missing
    variants from the catalog: deletes the variant row AND its
    raw_candidates row when it has one -- the raw_candidates row has to
    go too, or a future Resolve run (without a fresh Scan first) could
    silently re-create the exact variant just removed, since Resolve
    reads raw_candidates from the DB, not the live filesystem. Any app
    left with zero variants afterward is deleted outright (FK cascades
    take its tags/app_tags rows with it, same as everywhere else in this
    app an app row is ever deleted) -- an app only ever exists because
    of its variants, so zero variants means nothing legitimately left to
    keep. Nothing on the filesystem is touched -- there's nothing left
    there to touch, that's the entire premise of this feature.
    """
    conn = db.connect()
    result = CleanLibraryResult(checked=len(missing_items))

    log_dir = Path(move_log_dir) if move_log_dir else get_logs_dir(db.path)
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"clean_library_log_{run_stamp}.json"
    entries = []

    touched_app_ids = set()
    for item in missing_items:
        conn.execute("DELETE FROM variants WHERE id = ?", (item.variant_id,))
        if item.raw_candidate_id:
            conn.execute("DELETE FROM raw_candidates WHERE id = ?", (item.raw_candidate_id,))
        touched_app_ids.add(item.app_id)
        result.removed_variants += 1
        entries.append({
            "app_name": item.app_name, "version": item.version,
            "source": item.source_path, "status": "removed",
            "timestamp": datetime.now().isoformat(),
        })
    conn.commit()

    result.removed_apps = _delete_zero_variant_apps(conn, touched_app_ids)
    conn.commit()

    log_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    result.move_log_path = str(log_path)

    try:
        result.html_report_path = generate_clean_library_html_report(result, entries, report_dir=log_dir)
    except OSError as e:
        log.warning("Could not write HTML clean-library report: %s", e)

    return result


def generate_clean_library_html_report(
    result: CleanLibraryResult, entries: list[dict], *,
    report_dir: Optional[Path] = None, title: str = "Clean Library Report",
) -> str:
    """Same shared renderer as generate_reorganize_html_report -- see
    html_report.py's module docstring."""
    from html_report import ReportRow, render_operation_html_report

    report_dir = Path(report_dir) if report_dir else (
        Path(os.path.dirname(result.move_log_path)) if result.move_log_path else Path(".")
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = report_dir / f"clean_library_report_{stamp}.html"

    rows = [
        ReportRow(
            name=f"{e['app_name']} {e['version']}".strip(),
            source=e["source"], dest="",
            status_label="Removed", status_class="warn",
            detail="Source no longer exists on disk -- removed from catalog",
        )
        for e in entries
    ]

    html = render_operation_html_report(
        title=title,
        subtitle=f"{result.removed_variants} missing item(s) removed &middot; "
                 f"{result.removed_apps} app(s) fully removed (no variants left)",
        summary_cards=[
            ("Checked", result.checked, "neutral"),
            ("Removed (variants)", result.removed_variants, "warn" if result.removed_variants else "good"),
            ("Apps fully removed", result.removed_apps, "warn" if result.removed_apps else "good"),
        ],
        rows=rows,
        json_log_path=result.move_log_path,
    )
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)