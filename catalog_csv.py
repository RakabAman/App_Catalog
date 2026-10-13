"""
CSV export / import for the app catalog (no Qt in here -- unit-testable).

Two kinds of export
-------------------
SHORT  -- built for uploading to a chatbot that fills in missing scrape data.
          Only what the chatbot needs:
            context (do not edit):  app_id, name, catalog, subcatalog, sample_file_name
            to fill in:             description, publisher, homepage_url, winget_id,
                                    choco_id, latest_version, tags
          By default only apps that still miss one of the fill-in fields are
          included, and a ready-to-paste instruction file is written next to it.
FULL   -- the detailed report: every column of every app (catalog_full_apps) and
          every column of every variant (catalog_full_variants), ignored apps /
          variants included.

Import
------
Reads either file back (matched by app_uid, then app_id, then name+catalog).
By default it only FILLS BLANKS; values already in the catalog are kept unless
`overwrite=True`.  Values are validated (Winget / Chocolatey ids, URLs).
`dry_run=True` returns the same report without touching the database.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import math
import os
import re
from typing import Optional

from database import Database
from resolver import edit_app_field, normalize_key

# --------------------------------------------------------------------------
# Column definitions
# --------------------------------------------------------------------------

# read-only context the chatbot sees (never changed by a chatbot round-trip)
SHORT_CONTEXT_FIELDS = ["app_id", "name", "catalog", "subcatalog", "sample_file_name"]
# the columns the chatbot is asked to fill in
FILL_FIELDS = ["description", "publisher", "homepage_url", "winget_id", "choco_id", "latest_version"]
SHORT_FIELDS = SHORT_CONTEXT_FIELDS + FILL_FIELDS + ["tags"]
# "scrape" fields: filling any of these marks a never-scraped app as scraped
_SCRAPE_FIELDS = tuple(FILL_FIELDS)
# editable by import in addition to the fill-in columns
_EXTRA_IMPORT_FIELDS = ["license"]
_LOCKABLE = ("name", "catalog", "subcatalog")


def _tags_subquery() -> str:
    return ("(SELECT GROUP_CONCAT(t.name, ', ') FROM app_tags at "
            "JOIN tags t ON t.id = at.tag_id WHERE at.app_id = a.id)")


def _missing_sql() -> str:
    return " OR ".join(f"TRIM(COALESCE(a.{f}, '')) = ''" for f in FILL_FIELDS)


def count_apps_missing_data(db: Database) -> tuple[int, int]:
    """(apps that would go in a short export if 'only missing' is on, all non-ignored apps)."""
    conn = db.connect()
    total = conn.execute("SELECT COUNT(*) FROM apps a WHERE COALESCE(a.status,'') != 'ignored'").fetchone()[0]
    missing = conn.execute(
        f"SELECT COUNT(*) FROM apps a WHERE COALESCE(a.status,'') != 'ignored' AND ({_missing_sql()})"
    ).fetchone()[0]
    return missing, total


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def _flat(v) -> str:
    """One-line cell value (short export): no newlines, no runs of whitespace."""
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def _write_csv(path: str, fieldnames: list, rows, encoding: str = "utf-8", flat: bool = False):
    with open(path, "w", newline="", encoding=encoding) as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            d = dict(r)
            w.writerow({k: (_flat(d.get(k)) if flat else ("" if d.get(k) is None else d.get(k)))
                        for k in fieldnames})


def _short_rows(db: Database, only_missing: bool, include_versions: bool) -> list:
    conn = db.connect()
    where = "COALESCE(a.status,'') != 'ignored'"
    if only_missing:
        where += f" AND ({_missing_sql()})"
    return conn.execute(f"""
        SELECT a.id AS app_id, a.name, a.catalog, a.subcatalog,
               (SELECT v2.file_name FROM variants v2
                 WHERE v2.app_id = a.id AND v2.is_ignored = 0 AND v2.file_name IS NOT NULL
                 ORDER BY v2.id LIMIT 1) AS sample_file_name,
               (SELECT GROUP_CONCAT(DISTINCT v3.version) FROM variants v3
                 WHERE v3.app_id = a.id AND v3.is_ignored = 0 AND v3.version IS NOT NULL) AS local_versions,
               a.description, a.publisher, a.homepage_url, a.winget_id, a.choco_id, a.latest_version,
               {_tags_subquery()} AS tags
        FROM apps a
        WHERE {where}
        ORDER BY a.catalog, a.subcatalog, a.name
    """).fetchall()


def chatbot_prompt(fieldnames: list, n_apps: int, part: Optional[tuple] = None) -> str:
    """Ready-to-paste instructions that go with a short CSV."""
    part_line = f" (file {part[0]} of {part[1]})" if part else ""
    ver_line = ""
    if "local_versions" in fieldnames:
        ver_line = ("  - local_versions: the version(s) I own. This is only a hint for identifying the "
                    "app -- it is NOT the latest version.\n")
    return f"""I'm uploading a CSV{part_line} listing {n_apps} software apps from my personal library.
Please research each app and fill in the BLANK cells only. Return the completed CSV.

COLUMNS - do NOT change these (they identify the row):
  - app_id, name, catalog, subcatalog
  - sample_file_name: the installer file name I have, a hint for identifying the exact app/edition
{ver_line}
COLUMNS - fill in when blank, leave existing values untouched:
  - description    1-2 plain sentences: what the app does (no marketing fluff)
  - publisher      company or author
  - homepage_url   the official website, full URL starting with https://
  - winget_id      exact Windows Package Manager id in "Publisher.Package" form, e.g. Mozilla.Firefox
                   (must really exist in the winget repository, otherwise leave blank)
  - choco_id       exact Chocolatey package id (must really exist on community.chocolatey.org,
                   otherwise leave blank)
  - latest_version newest stable release number available today (just the number, e.g. 24.08)
  - tags           comma-separated; KEEP the existing tags and add up to ~5 short, generic
                   words describing what the app is for (e.g. Media Player, Compression)

RULES
  1. Never guess ids or URLs. If you are not sure, leave the cell empty.
  2. Keep every row, the row order, the header and all columns exactly as given.
  3. Do not overwrite a non-blank cell.
  4. If an app is a repack/portable/crack edition, describe and look up the original software.
  5. Output: the complete CSV (same header, comma separated, UTF-8, cells containing commas
     in double quotes), as a downloadable file or in ONE code block - no other text.
"""


def export_apps_csv(db: Database, output_dir: str, mode: str = "short", batch_size: int = 0,
                    only_missing: bool = True, include_versions: bool = False,
                    write_prompt: bool = True, base_name: str = "catalog",
                    app_ids=None) -> list[str]:
    """
    mode: "short" | "full" | "both".  Returns the paths written.
    app_ids: export only these apps (the current list, a category, a selection ...); None = all.
      short: catalog_short[_partNN].csv  (+ catalog_short[_partNN]_chatbot_prompt.txt)
      full : catalog_full_apps.csv + catalog_full_variants.csv
    """
    if mode not in ("short", "full", "both"):
        raise ValueError(f"unknown export mode {mode!r}")
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    written: list[str] = []

    if mode in ("short", "both"):
        fields = list(SHORT_FIELDS)
        if include_versions:
            fields.insert(fields.index("sample_file_name") + 1, "local_versions")
        rows = _short_rows(db, only_missing, include_versions)
        if app_ids is not None:
            _keep = set(app_ids)
            rows = [r for r in rows if r["app_id"] in _keep]
        if batch_size and batch_size > 0 and len(rows) > batch_size:
            total = math.ceil(len(rows) / batch_size)
            chunks = [(i + 1, total, rows[i * batch_size:(i + 1) * batch_size]) for i in range(total)]
        else:
            chunks = [(1, 1, rows)]
        for idx, total, chunk in chunks:
            suffix = f"_part{idx:02d}" if total > 1 else ""
            path = os.path.join(output_dir, f"{base_name}_short_{stamp}{suffix}.csv")
            _write_csv(path, fields, chunk, encoding="utf-8", flat=True)
            written.append(path)
            if write_prompt:
                ppath = os.path.join(output_dir, f"{base_name}_short_{stamp}{suffix}_chatbot_prompt.txt")
                with open(ppath, "w", encoding="utf-8") as f:
                    f.write(chatbot_prompt(fields, len(chunk), (idx, total) if total > 1 else None))
                written.append(ppath)

    if mode in ("full", "both"):
        conn = db.connect()
        app_cols = [r[1] for r in conn.execute("PRAGMA table_info(apps)")]
        extra = ["tags", "variant_count", "ignored_variant_count", "versions", "total_size_bytes",
                 "sample_file_name", "sample_path"]
        app_rows = conn.execute(f"""
            SELECT a.*,
                   {_tags_subquery()} AS tags,
                   (SELECT COUNT(*) FROM variants v WHERE v.app_id = a.id AND v.is_ignored = 0) AS variant_count,
                   (SELECT COUNT(*) FROM variants v WHERE v.app_id = a.id AND v.is_ignored = 1) AS ignored_variant_count,
                   (SELECT GROUP_CONCAT(DISTINCT v.version) FROM variants v
                     WHERE v.app_id = a.id AND v.is_ignored = 0) AS versions,
                   (SELECT SUM(v.file_size) FROM variants v WHERE v.app_id = a.id AND v.is_ignored = 0) AS total_size_bytes,
                   (SELECT v.file_name FROM variants v WHERE v.app_id = a.id AND v.file_name IS NOT NULL
                     ORDER BY v.id LIMIT 1) AS sample_file_name,
                   (SELECT v.source_path FROM variants v WHERE v.app_id = a.id ORDER BY v.id LIMIT 1) AS sample_path
            FROM apps a ORDER BY a.catalog, a.subcatalog, a.name
        """).fetchall()
        if app_ids is not None:
            _keep = set(app_ids)
            app_rows = [r for r in app_rows if r["id"] in _keep]
        # app_id first (the key the importer uses), then every real column, then the derived ones
        app_fields = ["app_id"] + app_cols + extra
        path = os.path.join(output_dir, f"{base_name}_full_apps_{stamp}.csv")
        _write_csv(path, app_fields, [dict(r, app_id=r["id"]) for r in app_rows], encoding="utf-8-sig")
        written.append(path)

        var_cols = [r[1] for r in conn.execute("PRAGMA table_info(variants)")]
        var_rows = conn.execute("""
            SELECT v.*, a.name AS app_name, a.catalog AS app_catalog, a.subcatalog AS app_subcatalog
            FROM variants v JOIN apps a ON a.id = v.app_id
            ORDER BY a.catalog, a.subcatalog, a.name, v.version, v.id
        """).fetchall()
        if app_ids is not None:
            var_rows = [r for r in var_rows if r["app_id"] in _keep]
        path = os.path.join(output_dir, f"{base_name}_full_variants_{stamp}.csv")
        _write_csv(path, var_cols + ["app_name", "app_catalog", "app_subcatalog"],
                   [dict(r) for r in var_rows], encoding="utf-8-sig")
        written.append(path)
    return written


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

_HEADER_ALIASES = {
    "id": "app_id", "appid": "app_id", "app_id": "app_id",
    "app": "name", "application": "name",
    "category": "catalog", "subcategory": "subcatalog", "sub_catalog": "subcatalog",
    "sub_category": "subcatalog",
    "homepage": "homepage_url", "home_page": "homepage_url", "url": "homepage_url",
    "website": "homepage_url", "web_site": "homepage_url", "homepage_link": "homepage_url",
    "winget": "winget_id", "winget_package": "winget_id", "winget_package_id": "winget_id",
    "winget_id": "winget_id",
    "choco": "choco_id", "chocolatey": "choco_id", "chocolatey_id": "choco_id",
    "chocolatey_package": "choco_id", "choco_package": "choco_id",
    "latest": "latest_version", "latest_available_version": "latest_version",
    "latest_version": "latest_version", "newest_version": "latest_version",
    "company": "publisher", "vendor": "publisher", "author": "publisher", "developer": "publisher",
    "desc": "description", "summary": "description",
    "tag": "tags", "keywords": "tags",
}
_BLANK_TOKENS = {"", "n/a", "na", "none", "null", "nil", "unknown", "-", "--", "—", "?", "tbd",
                 "not found", "not available", "no match", "not applicable"}
_WINGET_RE = re.compile(r"^[A-Za-z0-9][\w\-+]*(\.[\w\-+]+)+$")
_CHOCO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")
_DOMAIN_RE = re.compile(r"^[\w\-]+(\.[\w\-]+)+(:\d+)?(/\S*)?$")


def _norm_header(h: str) -> str:
    h = (h or "").strip().lstrip("\ufeff").lower().replace("-", "_").replace(" ", "_")
    return _HEADER_ALIASES.get(h, h)


def _clean(v) -> str:
    v = (v or "").strip()
    return "" if v.lower() in _BLANK_TOKENS else v


def _validate(field: str, value: str) -> tuple[Optional[str], str]:
    """-> (normalized value or None when invalid, reason when invalid)."""
    if field == "winget_id":
        if " " in value or not _WINGET_RE.match(value):
            return None, "not a valid Winget id (expected Publisher.Package)"
        return value, ""
    if field == "choco_id":
        if not _CHOCO_RE.match(value):
            return None, "not a valid Chocolatey id (no spaces / slashes)"
        return value, ""
    if field == "homepage_url":
        if re.match(r"^https?://\S+\.\S+", value, re.I):
            return value, ""
        if _DOMAIN_RE.match(value):
            return "https://" + value, ""
        return None, "not a valid web address"
    if field == "latest_version":
        if len(value) > 64 or "\n" in value:
            return None, "does not look like a version number"
        return value, ""
    if field == "publisher":
        return (value, "") if len(value) <= 200 else (None, "publisher text too long")
    if field == "description":
        value = re.sub(r"[ \t]+", " ", value)
        return (value, "") if len(value) <= 4000 else (None, "description too long")
    return value, ""


def _split_tags(text: str) -> list[str]:
    out, seen = [], set()
    for t in re.split(r"[,;|]", text or ""):
        t = t.strip()
        if t and t.lower() not in _BLANK_TOKENS and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _read_rows(csv_path: str) -> tuple[list, list]:
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        text = f.read()
    # chatbots sometimes wrap the CSV in a markdown code fence
    text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("```"))
    first = text.split("\n", 1)[0] if text else ""
    delim = ","
    if first.count(";") > first.count(","):
        delim = ";"
    elif first.count("\t") > first.count(","):
        delim = "\t"
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    headers = [_norm_header(h) for h in (reader.fieldnames or [])]
    rows = []
    for r in reader:
        rows.append({_norm_header(k): (v or "") for k, v in r.items() if k is not None})
    return headers, rows


def _find_app(conn, row: dict, uid_index: dict, name_index: dict):
    uid = (row.get("app_uid") or "").strip()
    if uid and uid in uid_index:
        return conn.execute("SELECT * FROM apps WHERE id = ?", (uid_index[uid],)).fetchone()
    raw = (row.get("app_id") or "").strip()
    if raw:
        try:
            a = conn.execute("SELECT * FROM apps WHERE id = ?", (int(float(raw)),)).fetchone()
            if a is not None:
                return a
        except ValueError:
            pass
    name = (row.get("name") or "").strip().lower()
    if name:
        ids = name_index.get((name, (row.get("catalog") or "").strip().lower())) \
            or name_index.get((name, None))
        if ids and len(ids) == 1:
            return conn.execute("SELECT * FROM apps WHERE id = ?", (ids[0],)).fetchone()
    return None


def _tag_ids(conn, app_id: int) -> list:
    return [r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN app_tags at ON at.tag_id = t.id WHERE at.app_id = ?", (app_id,))]


def _apply_tags(conn, app_id: int, names: list, replace: bool):
    if replace:
        conn.execute("DELETE FROM app_tags WHERE app_id = ?", (app_id,))
    for name in names:
        row = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        tag_id = row["id"] if row else conn.execute("INSERT INTO tags (name) VALUES (?)", (name,)).lastrowid
        conn.execute("INSERT OR IGNORE INTO app_tags (app_id, tag_id) VALUES (?, ?)", (app_id, tag_id))


def import_apps_csv(db: Database, csv_path: str, overwrite: bool = False, apply_names: bool = True,
                    tags_mode: str = "add", dry_run: bool = False, mark_complete: bool = True) -> dict:
    """
    Import a short (chatbot) or full apps CSV.

    overwrite   False (default): only blank catalog fields are filled; True: differing
                values in the file replace the catalog's.
    apply_names apply name / catalog / subcatalog changes (these lock the field, like a
                manual edit).
    tags_mode   "add" (keep existing tags, add new ones) | "replace".
    dry_run     compute the report only; nothing is written.
    mark_complete  an app whose description, publisher, homepage_url, winget_id, choco_id
                and latest_version are ALL filled after this import (and which the import
                changed) is marked scrape_status='scraped' and status='verified'.
                Partially filled apps keep their statuses.
    """
    headers, rows = _read_rows(csv_path)
    if "app_id" not in headers and "app_uid" not in headers and "name" not in headers:
        raise ValueError("No app_id / name column found -- this does not look like a catalog export.")
    if "source_path" in headers and "file_size" in headers:
        raise ValueError("This is the VARIANTS file of the full report. Import the apps file "
                         "(catalog_full_apps…) or a short file instead.")
    importable = set(FILL_FIELDS) | set(_EXTRA_IMPORT_FIELDS) | {"tags", "name", "catalog", "subcatalog"}
    if not importable & set(headers):
        raise ValueError("No importable columns found (expected e.g. description, homepage_url, "
                         "winget_id, choco_id, latest_version, tags).")

    conn = db.connect()
    uid_index = {r["app_uid"]: r["id"] for r in conn.execute(
        "SELECT id, app_uid FROM apps WHERE app_uid IS NOT NULL")}
    name_index: dict = {}
    for r in conn.execute("SELECT id, name, catalog FROM apps"):
        n = (r["name"] or "").strip().lower()
        name_index.setdefault((n, (r["catalog"] or "").strip().lower()), []).append(r["id"])
        name_index.setdefault((n, None), []).append(r["id"])

    stats = {"rows": len(rows), "matched": 0, "updated": 0, "skipped": 0, "not_found": 0,
             "filled": {}, "overwritten": {}, "kept_existing": 0, "tags_added": 0, "tag_apps": 0,
             "renamed": 0, "completed": 0, "rejected_count": 0, "rejected": [], "dry_run": dry_run,
             "overwrite": overwrite, "tags_mode": tags_mode}
    seen_ids: set = set()
    fillable = list(FILL_FIELDS) + _EXTRA_IMPORT_FIELDS

    def _bump(d, k):
        d[k] = d.get(k, 0) + 1

    for line_no, row in enumerate(rows, start=2):
        app = _find_app(conn, row, uid_index, name_index)
        if app is None:
            stats["not_found"] += 1
            continue
        if app["id"] in seen_ids:           # same app twice in the file: first row wins
            continue
        seen_ids.add(app["id"])
        stats["matched"] += 1
        label = app["name"]
        sets: dict = {}
        name_edits: dict = {}

        for field in fillable:
            if field not in row:
                continue
            raw = _clean(row[field])
            if not raw:
                continue
            value, why = _validate(field, raw)
            if value is None:
                stats["rejected_count"] += 1
                if len(stats["rejected"]) < 40:
                    stats["rejected"].append(f"row {line_no} ({label}): {field} '{raw[:60]}' -- {why}")
                continue
            current = (app[field] or "").strip() if field in app.keys() else ""
            if current == value:
                continue
            if not current:
                sets[field] = value
                _bump(stats["filled"], field)
            elif overwrite:
                sets[field] = value
                _bump(stats["overwritten"], field)
            else:
                stats["kept_existing"] += 1

        if apply_names:
            for field in _LOCKABLE:
                if field in row:
                    value = (row[field] or "").strip()
                    if value and value != (app[field] or ""):
                        name_edits[field] = value
                        stats["renamed"] += 1

        new_tags: list = []
        if "tags" in row:
            wanted = _split_tags(row["tags"])
            if wanted:
                have = _tag_ids(conn, app["id"])
                have_l = {t.lower() for t in have}
                if tags_mode == "replace":
                    if {t.lower() for t in wanted} != have_l:
                        new_tags = wanted
                        stats["tags_added"] += len([t for t in wanted if t.lower() not in have_l])
                else:
                    new_tags = [t for t in wanted if t.lower() not in have_l]
                    stats["tags_added"] += len(new_tags)
                if new_tags:
                    stats["tag_apps"] += 1

        mark = False
        if mark_complete and any(f in _SCRAPE_FIELDS for f in sets) and \
                (app["status"] or "") != "ignored":
            merged = {f: ((sets[f] if f in sets else app[f]) or "").strip() for f in FILL_FIELDS}
            mark = all(merged.values())
            if mark:
                stats["completed"] += 1

        if not (sets or name_edits or new_tags):
            stats["skipped"] += 1
            continue
        stats["updated"] += 1
        if dry_run:
            continue

        for field, value in name_edits.items():
            edit_app_field(db, app["id"], field, value)
        if sets:
            clause_extra = ""
            if mark:
                sets["scrape_status"] = "scraped"
                sets["status"] = "verified"
                clause_extra = ", last_scraped = datetime('now')"
            clause = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(f"UPDATE apps SET {clause}{clause_extra}, updated_at = datetime('now') WHERE id = ?",
                         (*sets.values(), app["id"]))
        if new_tags:
            _apply_tags(conn, app["id"], new_tags, replace=(tags_mode == "replace"))
        conn.execute("INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
                     ("app", app["id"], "csv_import",
                      json.dumps({"set": sets, "names": name_edits, "tags": new_tags})))
    if not dry_run:
        conn.commit()
    return stats


def format_import_report(stats: dict) -> str:
    """Human-readable summary (used for the preview and the final message)."""
    verb = "Will be" if stats.get("dry_run") else "Were"
    lines = [f"{stats['rows']} row(s) in the file; {stats['matched']} matched an app, "
             f"{stats['not_found']} not found in the catalog."]
    if stats["filled"]:
        lines.append(f"{verb} filled: " + ", ".join(f"{k} ×{v}" for k, v in stats["filled"].items()))
    if stats["overwritten"]:
        lines.append(f"{verb} OVERWRITTEN: " + ", ".join(f"{k} ×{v}" for k, v in stats["overwritten"].items()))
    if stats["tags_added"]:
        lines.append(f"Tags: {stats['tags_added']} tag(s) "
                     f"{'replaced/added' if stats['tags_mode'] == 'replace' else 'added'} on {stats['tag_apps']} app(s).")
    if stats["renamed"]:
        lines.append(f"Name / catalog / subcatalog changes: {stats['renamed']}.")
    if stats["kept_existing"]:
        lines.append(f"{stats['kept_existing']} cell(s) differ from the catalog but were kept "
                     f"(overwrite is off).")
    if stats.get("completed"):
        lines.append(f"{stats['completed']} app(s) now have every detail filled -> "
                     f"{'will be ' if stats.get('dry_run') else ''}marked scraped + verified.")
    lines.append(f"=> {stats['updated']} app(s) {'would change' if stats.get('dry_run') else 'changed'}, "
                 f"{stats['skipped']} unchanged.")
    if stats["rejected_count"]:
        lines.append(f"\n{stats['rejected_count']} value(s) rejected as invalid"
                     + (" (first 40 shown)" if stats["rejected_count"] > 40 else "") + ":")
        lines += ["  - " + r for r in stats["rejected"]]
    return "\n".join(lines)
