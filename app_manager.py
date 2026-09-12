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
import json
import logging
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from rapidfuzz import fuzz

from app_paths import get_logs_dir
from database import Database
from resolver import normalize_key

log = logging.getLogger("appcatalog.organizer")
log_job = logging.getLogger("appcatalog.organizer.job")

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
    shared_folder: bool = False  # True if another variant also uses this exact source_path
                                  # (two distinct install units shipped in one folder) -- see
                                  # execute_reorganize()'s docstring for how this changes handling
    shared_extra_paths: list = field(default_factory=list)  # only for shared_folder plans: other
                                  # items in the folder unclaimed by any sibling variant's file
    app_name: str = ""           # display-only, for logs/reports (checkpoint 21) -- never used
                                  # for path-building logic, that's already baked into dest_path
    version: str = ""            # display-only, ditto


@dataclass
class ReorganizeResult:
    moved: int = 0
    archived: int = 0
    skipped_collisions: int = 0
    failed: list[dict] = field(default_factory=list)
    move_log_path: Optional[str] = None
    # checkpoint 21: every plan's outcome (not just failures), in the
    # order processed -- app_name/version included for a readable
    # report. This is the SAME data written incrementally to
    # move_log_path as the run progresses; kept here too so a caller
    # doesn't need to re-read and re-parse that JSON file just to build
    # a human-facing report of what happened.
    entries: list[dict] = field(default_factory=list)
    html_report_path: Optional[str] = None


def _safe_path_component(name: str) -> str:
    """Strips characters that are invalid in Windows path components."""
    cleaned = "".join(c for c in (name or "") if c not in '<>:"/\\|?*').strip()
    return cleaned or "Unnamed"


def preview_reorganize(
    db: Database, dest_root: str, *,
    portable_to_dedicated_category: bool = True,
    portable_category_name: str = "Portable",
) -> list[PlannedMove]:
    """
    DRY RUN ONLY -- computes where every variant's install-unit folder
    WOULD move to under dest_root/Catalog/Subcatalog/AppName/Version/ (or
    dest_root/Portable/OriginalCatalog/AppName/Version/ for a
    portable-tagged app), and flags any destination folder that already
    exists as a heads-up, but moves nothing. This is the ONLY way to get
    a move plan; execute_reorganize() requires being handed the exact
    list this returns (see its docstring for why).
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

    # Folders used by more than one variant -- the rare multi-installer-
    # in-one-folder case that needs per-file handling instead of a
    # whole-folder move (see module docstring above).
    folder_counts: dict = {}
    for r in rows:
        folder_counts[r["source_path"]] = folder_counts.get(r["source_path"], 0) + 1

    portable_app_ids = set()
    if portable_to_dedicated_category:
        tag_rows = conn.execute(
            """SELECT at.app_id FROM app_tags at JOIN tags t ON t.id = at.tag_id
               WHERE LOWER(t.name) = 'portable'"""
        ).fetchall()
        portable_app_ids = {r["app_id"] for r in tag_rows}

    # For shared folders: every sibling variant's claimed filename, so we
    # can work out what's left over ("shared extras") by elimination.
    # Keyed by source_path -> set of claimed file_names.
    claimed_by_folder: dict = {}
    for r in rows:
        if folder_counts[r["source_path"]] > 1 and r["file_name"]:
            claimed_by_folder.setdefault(r["source_path"], set()).add(r["file_name"])

    plans = []
    for r in rows:
        is_portable = r["app_id"] in portable_app_ids
        catalog_raw = r["catalog"] or "Uncategorized"
        subcatalog_raw = r["subcatalog"] or "Misc"
        if is_portable and catalog_raw != portable_category_name:
            # Original catalog (e.g. "Graphics", "Desktop Utilities") becomes
            # the subcatalog under the dedicated Portable category.
            catalog_raw, subcatalog_raw = portable_category_name, catalog_raw

        catalog = _safe_path_component(catalog_raw)
        subcatalog = _safe_path_component(subcatalog_raw)
        app_name = _safe_path_component(r["app_name"])
        version = _safe_path_component(r["version"] or "unknown-version")
        dest = os.path.join(dest_root, catalog, subcatalog, app_name, version)

        shared = folder_counts[r["source_path"]] > 1
        shared_extras = []
        if shared:
            claimed = claimed_by_folder.get(r["source_path"], set())
            try:
                for name in os.listdir(r["source_path"]):
                    if name in claimed or name.lower() in _JUNK_FILENAMES:
                        continue
                    shared_extras.append(os.path.join(r["source_path"], name))
            except OSError:
                pass  # folder unreadable/gone -- execute_reorganize will catch this per-plan

        plans.append(PlannedMove(
            app_id=r["app_id"], variant_id=r["variant_id"],
            source_path=r["source_path"], primary_file_name=r["file_name"],
            dest_path=dest, collision=os.path.exists(dest),
            is_portable=is_portable, shared_folder=shared,
            shared_extra_paths=shared_extras,
            app_name=r["app_name"] or "", version=r["version"] or "",
        ))
    return plans


def _archive_single_file(file_path: str, archive_format: str) -> Optional[str]:
    """
    Compresses ONE file into an archive of the same base name next to it
    (e.g. Setup.exe -> Setup.7z), then removes the original file --
    everything else in that file's folder (readme, crack, keygen, theme,
    serial, ...) is left completely untouched. Archiving here is
    deliberately scoped to just the installer file, not the whole
    destination folder, so a version's extras stay as ordinary loose
    files/folders rather than getting swept into the archive too.

    Returns the archive's path on success, or None if archive_format is
    'none' or archiving failed -- on failure the original file is left
    exactly as it was (nothing partially deleted), and the caller decides
    how to report it.
    """
    if archive_format == "none":
        return None
    if archive_format not in VALID_ARCHIVE_FORMATS:
        log.warning("Unknown archive_format %r -- leaving %s as-is", archive_format, file_path)
        return None

    base, _ext = os.path.splitext(file_path)
    target = f"{base}.{archive_format}"
    if os.path.exists(target):
        log.warning("Archive target %s already exists -- leaving %s uncompressed", target, file_path)
        return None

    try:
        if archive_format == "7z":
            try:
                import py7zr
            except ImportError:
                log.warning("py7zr is not installed -- falling back to zip for %s", file_path)
                return _archive_single_file(file_path, "zip")
            with py7zr.SevenZipFile(target, "w") as archive:
                archive.write(file_path, arcname=os.path.basename(file_path))

        elif archive_format == "zip":
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(file_path, os.path.basename(file_path))

        elif archive_format == "rar":
            try:
                proc = subprocess.run(
                    ["rar", "a", "-ep1", target, file_path],
                    capture_output=True, text=True,
                    # checkpoint 23: see scraper.py's fetch_winget_show()
                    # for why this needs an explicit encoding -- WinRAR's
                    # own console output isn't guaranteed to be the
                    # platform's default codepage (cp1252 on typical
                    # Windows), and a decode crash here would take down
                    # the whole reorganize run over one archiving message.
                    encoding="utf-8", errors="replace",
                )
            except FileNotFoundError:
                log.warning(
                    "No 'rar' executable found on PATH -- RAR archiving requires "
                    "WinRAR/rar installed separately (Python can't write .rar itself). "
                    "Leaving %s uncompressed.", file_path,
                )
                return None
            if proc.returncode != 0:
                log.error("RAR archiving failed for %s: %s", file_path, proc.stderr)
                return None
    except OSError as e:
        log.error("Archiving %s to %s failed: %s", file_path, archive_format, e)
        return None

    try:
        os.remove(file_path)
    except OSError as e:
        log.warning("Archived %s to %s but could not remove the original file: %s", file_path, target, e)
    return target


def _transfer_item(item: str, dest_dir: str, copy_mode: str) -> tuple:
    """
    Moves or copies one file/folder into dest_dir. Returns
    (transferred: bool, collided: bool). Never overwrites: if
    dest_dir/basename(item) already exists, it's reported as a collision
    and left untouched.
    """
    if not os.path.exists(item):
        return False, False  # vanished between preview and execute -- not fatal
    dest_item = os.path.join(dest_dir, os.path.basename(item))
    if os.path.exists(dest_item):
        return False, True
    if copy_mode == "copy":
        if os.path.isdir(item):
            shutil.copytree(item, dest_item)
        else:
            shutil.copy2(item, dest_item)
    else:
        shutil.move(item, dest_item)
    return True, False


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


def execute_reorganize(
    db: Database, planned_moves: list[PlannedMove], *,
    copy_mode: str = "move",
    archive_format: str = "none",
    move_log_dir: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int, "PlannedMove", str], None]] = None,
) -> ReorganizeResult:
    """
    Executes EXACTLY the plan handed in (the caller is expected to have
    gotten this from preview_reorganize(), reviewed it, and dropped/kept
    whichever rows it wants) -- deliberately does NOT recompute the plan
    itself, so a caller can't accidentally execute a plan that's gone
    stale relative to what a user actually reviewed.

    progress_callback(index, total, plan, status), if given, is invoked
    after EVERY plan (index is 1-based) regardless of outcome -- lets a
    GUI drive a progress bar/live log without this function knowing
    anything about Qt. A callback that raises is logged and ignored
    rather than allowed to abort the actual file operation underway.
    Every plan is also always logged to the "appcatalog.organizer.job"
    logger (one line each: OK/SKIPPED/FAILED) regardless of whether a
    callback is given, so a console/log file always has a record even
    with no GUI attached -- see the module's existing scanner.job/
    resolver.job loggers for the established convention this follows.

    copy_mode: "move" (default) removes items from source_path as they're
    transferred and repoints variants.source_path at the new location.
    "copy" leaves the originals in place and does NOT repoint
    variants.source_path -- the original is still the live, valid
    location, so the catalog keeps referencing it rather than the new
    duplicate.

    archive_format: "none" (default) leaves every transferred file as-is.
    "7z"/"zip"/"rar" compresses ONLY the variant's own installer file in
    place inside the destination folder (e.g. Setup.exe -> Setup.7z) once
    the transfer completes, and updates the catalog's file_name (move
    mode only) to match -- everything else in that folder (readme, crack,
    keygen, theme, serial, ...) is left exactly as it was, never swept
    into the archive. Skipped entirely when the variant's own file is
    already a packaged/compressed format (.zip/.rar/.7z/.tar/.gz/.iso/...
    -- see DEFAULT_ALREADY_COMPRESSED_EXTENSIONS, overridable via the
    "monitor_already_compressed_extensions" setting shared with
    monitor.py): compressing an already-compressed installer again wastes
    time/CPU for no space savings. See _archive_single_file()'s docstring
    for format-specific caveats (7z needs py7zr, rar needs an external
    rar/WinRAR binary).

    Two transfer strategies, chosen per plan:
      - ORDINARY plan (source_path folder used by only this variant,
        the vast majority of cases): the WHOLE folder's contents move/
        copy into dest_path in one pass. Anything sitting alongside the
        installer inside that folder -- readme, crack, keygen, theme,
        serial, whatever -- travels with it automatically, since it was
        never a separate item to track.
      - SHARED-FOLDER plan (plan.shared_folder is True -- another variant
        in THIS SAME planned_moves batch also has this source_path,
        e.g. a portable build and a regular installer shipped in one
        folder): only this variant's own file (primary_file_name) is
        MOVED/COPIED per copy_mode. Anything else left in that folder
        that no sibling variant claims (plan.shared_extra_paths) is
        always COPIED (never moved), regardless of copy_mode, into every
        sibling's destination -- since deleting it would risk breaking a
        sibling plan not-yet-processed in the same run. This means a
        shared folder's unclaimed extras end up duplicated across every
        variant that shared the folder, and the original folder is left
        in place afterward rather than auto-deleted, so nothing is ever
        silently lost; it can be cleaned up by hand once confirmed.

    Safety choices (see this module's docstring for why, vs. the
    DeepSeek build this was adapted from):
      - Collision handling is per-ITEM, not per-plan: dest_path is always
        created and each item is checked against its own destination
        filename individually, so a version folder that already has SOME
        of its files from an earlier partial run doesn't block the rest
        from completing, and nothing is EVER overwritten. A plan only
        counts as fully "skipped_collision" if every one of its items
        already exists at the destination.
      - Every plan is wrapped individually; one failure is recorded and
        skipped rather than aborting the whole batch.
      - A JSON move log (source, dest, timestamp, per-item detail) is
        written to move_log_dir (defaults to next to catalog.db) both
        before starting (the full plan, so a crash mid-run still leaves a
        record of intent) and updated with each result, for manual
        audit/undo -- there's no automatic "undo" button, but the log has
        everything needed to reverse it by hand.
    """
    if copy_mode not in VALID_COPY_MODES:
        raise ValueError(f"copy_mode must be one of {VALID_COPY_MODES}, got {copy_mode!r}")
    if archive_format not in VALID_ARCHIVE_FORMATS:
        raise ValueError(f"archive_format must be one of {VALID_ARCHIVE_FORMATS}, got {archive_format!r}")

    conn = db.connect()
    result = ReorganizeResult()
    total = len(planned_moves)

    # Shared with monitor.py's own "already compressed" handling (see
    # gui_main.py's Monitor tab setting of the same name) so both features
    # agree on what counts as "already packaged" without two separate
    # places to configure it. Falls back to a sensible built-in list if
    # the setting has never been touched (it defaults to an empty list).
    settings = db.get_all_settings()
    already_compressed_exts = {
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in (settings.get("monitor_already_compressed_extensions") or DEFAULT_ALREADY_COMPRESSED_EXTENSIONS)
    }

    # checkpoint 21: logs live in logs/ next to catalog.db (see
    # app_paths.py), not bare next to it -- keeps the catalog's own
    # folder from filling up with timestamped JSON/HTML files over time.
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

    log.info(
        "REORGANIZE STARTING: %d item(s) to process (mode=%s, archive=%s)",
        total, copy_mode, archive_format,
    )

    def _report(index: int, plan: "PlannedMove", status: str):
        if progress_callback is not None:
            try:
                progress_callback(index, total, plan, status)
            except Exception:  # a GUI callback misbehaving must never abort the file operation
                log.exception("progress_callback raised -- ignoring and continuing")

    for i, plan in enumerate(planned_moves, start=1):
        entry = {
            "variant_id": plan.variant_id, "app_id": plan.app_id,
            "app_name": plan.app_name, "version": plan.version,
            "source": plan.source_path, "dest": plan.dest_path,
            "shared_folder": plan.shared_folder, "copy_mode": copy_mode,
            "archive_format": archive_format,
            "timestamp": datetime.now().isoformat(), "status": "pending",
        }
        log_entries.append(entry)
        _flush_log()

        # Reported/logged BEFORE any file work starts on this item, not
        # just after it finishes -- a large folder or a slow archive step
        # can take a real amount of time with nothing else to show for it
        # in between, and without this the console/activity log can look
        # stalled between one item's completion and the next.
        log_job.info("[%d/%d] STARTING: %s", i, total, plan.source_path)
        _report(i, plan, "starting")

        if not os.path.exists(plan.source_path):
            entry["status"] = "failed"
            entry["error"] = "source folder no longer exists"
            result.failed.append({
                "variant_id": plan.variant_id, "app_name": plan.app_name,
                "version": plan.version, "source": plan.source_path,
                "dest": plan.dest_path, "error": entry["error"],
            })
            log_job.error("[%d/%d] FAILED: %s -- source folder no longer exists", i, total, plan.source_path)
            _report(i, plan, entry["status"])
            _flush_log()
            continue

        try:
            os.makedirs(plan.dest_path, exist_ok=True)

            item_collisions = []
            moved_any = False

            if plan.shared_folder:
                # Only this variant's own file, plus a copy of whatever's
                # unclaimed in the shared folder (see docstring above).
                own_items = []
                if plan.primary_file_name:
                    own_items.append(os.path.join(plan.source_path, plan.primary_file_name))
                for item in own_items:
                    transferred, collided = _transfer_item(item, plan.dest_path, copy_mode)
                    moved_any = moved_any or transferred
                    if collided:
                        item_collisions.append(item)
                for item in plan.shared_extra_paths:
                    transferred, collided = _transfer_item(item, plan.dest_path, "copy")
                    moved_any = moved_any or transferred
                    if collided:
                        item_collisions.append(item)
                # Shared folders are never auto-cleaned -- see docstring.
            else:
                items = [os.path.join(plan.source_path, n) for n in os.listdir(plan.source_path)]
                for item in items:
                    transferred, collided = _transfer_item(item, plan.dest_path, copy_mode)
                    moved_any = moved_any or transferred
                    if collided:
                        item_collisions.append(item)

            if item_collisions and not moved_any:
                entry["status"] = "skipped_collision"
                entry["colliding_items"] = item_collisions
                result.skipped_collisions += 1
                log_job.warning("[%d/%d] SKIPPED (destination already has this content): %s",
                                 i, total, plan.source_path)
                _report(i, plan, entry["status"])
                _flush_log()
                continue
            if item_collisions:
                entry["partial_collisions"] = item_collisions

            # Move mode: clean up a now-empty ORDINARY source folder, then
            # keep climbing and removing now-empty parent folders too (an
            # app's version folder disappearing often leaves an empty
            # AppName/ folder behind, and sometimes an empty Subcatalog/
            # or Catalog/ folder above that) -- a plain os.rmdir() only
            # ever removes the one directory that's actually empty, so it
            # naturally stops the moment it hits a folder still holding
            # something else. (Shared folders are deliberately left in
            # place -- see docstring. A bare-file transfer has nothing
            # left to clean up, shutil.move already removed the file.)
            if copy_mode == "move" and not plan.shared_folder:
                try:
                    if os.path.exists(plan.source_path) and not os.listdir(plan.source_path):
                        os.rmdir(plan.source_path)
                        _cleanup_empty_ancestors(os.path.dirname(plan.source_path))
                except OSError as e:
                    log.warning("Could not remove empty source folder %s: %s", plan.source_path, e)

            # dest_path (the version folder) never changes because of
            # archiving now -- only the specific installer file inside it
            # gets replaced with a compressed version; every sidecar next
            # to it (readme, crack, theme, ...) stays exactly as it was.
            new_file_name = plan.primary_file_name
            already_compressed = _is_already_compressed(plan.primary_file_name, already_compressed_exts)
            if archive_format != "none" and not already_compressed:
                if plan.primary_file_name:
                    installer_path = os.path.join(plan.dest_path, plan.primary_file_name)
                    if os.path.exists(installer_path):
                        log_job.info("[%d/%d] Archiving installer as .%s: %s",
                                     i, total, archive_format, installer_path)
                        archived_path = _archive_single_file(installer_path, archive_format)
                        if archived_path:
                            new_file_name = os.path.basename(archived_path)
                            result.archived += 1
                            entry["archive_path"] = archived_path
                    else:
                        log_job.warning(
                            "[%d/%d] Could not find installer file to archive at %s -- left uncompressed",
                            i, total, installer_path,
                        )
                else:
                    log_job.warning(
                        "[%d/%d] No known installer filename for this variant -- left uncompressed",
                        i, total,
                    )
            elif archive_format != "none" and already_compressed:
                log_job.info(
                    "[%d/%d] Not archiving -- %s is already a compressed/packaged file",
                    i, total, plan.primary_file_name,
                )
                entry["archive_skipped_already_compressed"] = True

            # Only a MOVE repoints the catalog's live pointer -- a COPY
            # leaves the original untouched and still valid, so the catalog
            # keeps referencing it rather than the new duplicate. file_name
            # is updated alongside source_path when archiving renamed the
            # installer (e.g. Setup.exe -> Setup.7z).
            if copy_mode == "move":
                conn.execute(
                    "UPDATE variants SET source_path = ?, file_name = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (plan.dest_path, new_file_name, plan.variant_id),
                )
                conn.commit()

            entry["status"] = "moved" if copy_mode == "move" else "copied"
            result.moved += 1
            log_job.info("[%d/%d] %s OK: %s -> %s",
                          i, total, copy_mode.upper(), plan.source_path, plan.dest_path)
        except OSError as e:
            entry["status"] = "failed"
            entry["error"] = str(e)
            result.failed.append({
                "variant_id": plan.variant_id, "app_name": plan.app_name,
                "version": plan.version, "source": plan.source_path,
                "dest": plan.dest_path, "error": str(e),
            })
            log_job.error("[%d/%d] %s FAILED: %s -> %s: %s",
                           i, total, copy_mode.upper(), plan.source_path, plan.dest_path, e)
        _report(i, plan, entry["status"])
        _flush_log()

    result.entries = log_entries

    log.info(
        "REORGANIZE FINISHED: moved=%d archived=%d skipped=%d failed=%d",
        result.moved, result.archived, result.skipped_collisions, len(result.failed),
    )

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
            bits.append(f"compressed to {os.path.basename(e['archive_path'])}")
        elif e.get("archive_skipped_already_compressed"):
            bits.append("already compressed, not re-archived")
        return "; ".join(bits)

    _STATUS_MAP = {
        "moved": ("Moved", "good"), "copied": ("Copied", "good"),
        "failed": ("Failed", "bad"), "skipped_collision": ("Skipped", "warn"),
        "pending": ("Interrupted", "bad"),  # never flushed past "pending" -- the run crashed/was killed
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
    `os.path.exists()` checks -- confirms every variant's source_path
    still exists. Scoped to variants whose source_path currently lives
    under `root_path` when one is given (so running this from one scan
    root's row in ScanRootsDialog -- e.g. an external drive that simply
    isn't plugged in right now -- can't wrongly flag every OTHER root's
    apps as missing too); pass root_path=None to check the whole catalog
    regardless of which root each variant came from.
    """
    conn = db.connect()
    rows = conn.execute("""
        SELECT v.id AS variant_id, v.app_id, v.source_path, v.version,
               v.raw_candidate_id, a.name AS app_name
        FROM variants v JOIN apps a ON a.id = v.app_id
        ORDER BY a.name, v.version
    """).fetchall()

    if root_path:
        rows = [r for r in rows if _path_is_under(r["source_path"], root_path)]

    missing: list[MissingItem] = []
    total = len(rows)
    for i, r in enumerate(rows, start=1):
        if cancel_flag and cancel_flag():
            break
        if not os.path.exists(r["source_path"]):
            missing.append(MissingItem(
                variant_id=r["variant_id"], app_id=r["app_id"],
                app_name=r["app_name"] or "", version=r["version"] or "",
                source_path=r["source_path"], raw_candidate_id=r["raw_candidate_id"],
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

    for app_id in touched_app_ids:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM variants WHERE app_id = ?", (app_id,)
        ).fetchone()["n"]
        if remaining == 0:
            conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
            result.removed_apps += 1
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
