"""
app_manifest.py -- per-variant manifest files ("appcatalog.json").

WHY
---
Every manual correction (renamed app, locked version, confirmed installer
file, scraped description / Winget / Choco ids ...) lives only in
catalog.db.  Lose or reset the DB, or reorganize the files, and a rescan has
nothing but messy folder names to work from.  A *variant manifest* stores
that work NEXT TO THE FILES, so a fresh database can recognise a variant
again no matter which category folder it was moved to.

WHAT A MANIFEST IS
------------------
One JSON file per variant (= install unit), independent of catalog and
subcatalog (those are deliberately NOT stored -- a move can never leave a
stale category behind):

  * ``app``      name, scraped details (description, publisher, homepage,
                 license, Winget / Choco ids, tags), name lock.
  * ``variant``  version, edition, architecture, language, ignored/verified,
                 locks, the original (pre-cleanup) name.
  * ``unit``     the ENTRY file (the setup file, optionally *confirmed*) plus
                 ``members``: every dependent file / folder of the unit
                 (``required``) and extras such as readme / crack
                 (``companion``).  Everything is relative to the manifest's
                 own folder, so the unit can live on any drive.

PLACEMENT AND NAMING
--------------------
Every manifest is named after the thing it describes, so one can never be
mistaken for -- or overwritten by -- another variant's:

  * variant owns its folder  ->  ``<folder>/<App name> <version> [edition]
                                 [arch] [language].appcatalog.json``
    (moving the folder moves its manifest with it)
  * folder shared by several variants (or containing other apps)
                             ->  ``<folder>/<entry file name>.appcatalog.json``

The writer never overwrites a manifest that belongs to a different variant
(it adds a short uid to the name instead).  A renamed app simply gets its
manifest renamed; the old file is removed.  The legacy plain
``appcatalog.json`` name is still read and is migrated on the next write.

KEEPING OLD DATABASES SAFE
--------------------------
Everything here is additive.  An existing catalog.db gets a few extra
columns, three uid/dirty triggers and a one-off uid backfill -- no existing
value is touched, and NO manifest is written for an old database until a
variant changes (auto mode) or the user asks (Create manifests button).
A manifest never overrides what an existing database variant already has:
see ``apply_manifest`` for the exact precedence.

AUTO MODE
---------
SQLite triggers (created in ``ensure_manifest_schema``) set
``variants.manifest_dirty`` whenever a manifest-relevant column really
changes -- no matter which code path did the edit.  ``flush_dirty`` then
(re)writes the dirty variants; the GUI calls it from a timer, and the
scraper / reorganize / monitor call it (or the writer) directly.  Content
hashing makes a flush of an unchanged variant a no-op.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

log = logging.getLogger("appcatalog.manifest")

MANIFEST_FILENAME = "appcatalog.json"
SIBLING_SUFFIX = ".appcatalog.json"
SCHEMA_VERSION = 1
KIND = "appcatalog.variant"
WRITTEN_BY = "AppCatalog"

_VOLATILE_KEYS = ("updated_at", "written_by")

DEFAULT_COMPANION_WORDS = [
    "readme", "read me", "read_me", "crack", "keygen", "key gen", "patch",
    "patcher", "serial", "activat", "licen", "instruction", "how to",
    "howto", "release note", "changelog", "nfo", "diz", "info", "website",
    "cover", "screenshot", "thumbs", "desktop.ini",
]
_COMPANION_EXTS = {".nfo", ".diz", ".txt", ".url", ".lnk", ".md", ".jpg",
                   ".jpeg", ".png", ".gif", ".sfv", ".md5", ".sha1"}


# ---------------------------------------------------------------------------
# filename helpers
# ---------------------------------------------------------------------------
def is_manifest_filename(name: str) -> bool:
    n = (name or "").lower()
    return n == MANIFEST_FILENAME or n.endswith(SIBLING_SUFFIX)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# schema: additive columns + uid/dirty triggers (safe on every startup)
# ---------------------------------------------------------------------------
_COLUMN_MIGRATIONS = [
    ("apps", "app_uid", "TEXT"),
    ("variants", "variant_uid", "TEXT"),
    ("variants", "manifest_hash", "TEXT"),       # hash of the content last written / applied
    ("variants", "manifest_dirty", "INTEGER DEFAULT 0"),
    ("variants", "manifest_path", "TEXT"),
    ("variants", "manifest_written_at", "TEXT"),
    ("variants", "manifest_error", "TEXT"),
]

_APP_DIRTY_COLS = (
    "name", "name_locked", "publisher", "description", "homepage_url",
    "winget_id", "choco_id", "license", "latest_version", "scrape_status",
    "last_scraped", "status",
)
_VARIANT_DIRTY_COLS = (
    "app_id", "version", "version_locked", "edition", "architecture",
    "language", "source_path", "file_name", "file_locked", "is_ignored", "app_pinned",
)


def _trigger_defs() -> dict[str, str]:
    app_when = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _APP_DIRTY_COLS)
    var_when = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _VARIANT_DIRTY_COLS)
    return {
        # new rows always get a uid (resolver, monitor, split ... all covered)
        "trg_apps_uid_ins": """CREATE TRIGGER trg_apps_uid_ins AFTER INSERT ON apps
           WHEN NEW.app_uid IS NULL
           BEGIN UPDATE apps SET app_uid = lower(hex(randomblob(16))) WHERE id = NEW.id; END""",
        "trg_variants_uid_ins": """CREATE TRIGGER trg_variants_uid_ins AFTER INSERT ON variants
           BEGIN
             UPDATE variants
                SET variant_uid = COALESCE(variant_uid, lower(hex(randomblob(16)))),
                    manifest_dirty = 1
              WHERE id = NEW.id;
           END""",
        # a REAL change to a manifest-relevant app field dirties all its variants.
        # (catalog / subcatalog are intentionally absent: category edits never
        # touch a manifest.)
        "trg_apps_manifest_dirty": f"""CREATE TRIGGER trg_apps_manifest_dirty AFTER UPDATE ON apps
            WHEN {app_when}
            BEGIN UPDATE variants SET manifest_dirty = 1 WHERE app_id = NEW.id; END""",
        "trg_variants_manifest_dirty": f"""CREATE TRIGGER trg_variants_manifest_dirty AFTER UPDATE ON variants
            WHEN {var_when}
            BEGIN UPDATE variants SET manifest_dirty = 1 WHERE id = NEW.id; END""",
    }


def _norm_sql(sql: Optional[str]) -> str:
    return " ".join((sql or "").split())


def ensure_manifest_schema(conn: sqlite3.Connection) -> None:
    """Additive migration + triggers + one-off uid backfill.  Idempotent and
    cheap on repeat runs (triggers are only rebuilt if missing/different)."""
    for table, column, coldef in _COLUMN_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
            conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Backfill uids with the dirty-triggers OUT of the way, so an old
    # database never suddenly wants to write thousands of manifests.
    missing = {
        (table, col): [r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE {col} IS NULL")]
        for table, col in (("apps", "app_uid"), ("variants", "variant_uid"))
    }
    if any(missing.values()):
        for name in ("trg_apps_manifest_dirty", "trg_variants_manifest_dirty"):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for (table, col), ids in missing.items():
            if ids:
                conn.executemany(f"UPDATE {table} SET {col} = ? WHERE id = ?",
                                 [(uuid.uuid4().hex, i) for i in ids])
                log.info("manifest: backfilled %d %s value(s)", len(ids), col)

    have = {r[0]: _norm_sql(r[1]) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    for name, sql in _trigger_defs().items():
        if have.get(name) != _norm_sql(sql):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(sql)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_variants_manifest_dirty ON variants(manifest_dirty)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_variants_uid ON variants(variant_uid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_uid ON apps(app_uid)")
    conn.commit()


# ---------------------------------------------------------------------------
# fingerprints
# ---------------------------------------------------------------------------
def quick_fingerprint(path: str, chunk: int = 1 << 20) -> Optional[str]:
    """Path-independent file identity: size + head/middle/tail 1 MB hash.
    Cheap enough for multi-GB ISOs; survives rename and move."""
    try:
        size = os.path.getsize(path)
        h = hashlib.sha256()
        h.update(str(size).encode())
        with open(path, "rb") as f:
            if size <= 3 * chunk:
                h.update(f.read())
            else:
                h.update(f.read(chunk))
                f.seek(max(0, size // 2 - chunk // 2))
                h.update(f.read(chunk))
                f.seek(max(0, size - chunk))
                h.update(f.read(chunk))
        return f"q1:{size}:{h.hexdigest()[:32]}"
    except OSError:
        return None


def tree_stats(path: str) -> tuple[int, int, str]:
    """(file count, total bytes, structural fingerprint) of a folder tree.
    Structural = sorted relative paths + sizes; file contents are never read."""
    items: list[str] = []
    files = total = 0
    stack = [path]
    base = path.rstrip("\\/")
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif is_manifest_filename(e.name):
                            continue
                        else:
                            sz = e.stat(follow_symlinks=False).st_size
                            files += 1
                            total += sz
                            items.append(f"{e.path[len(base) + 1:].replace(os.sep, '/').lower()}|{sz}")
                    except OSError:
                        continue
        except OSError:
            continue
    items.sort()
    return files, total, hashlib.sha1("\n".join(items).encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------
# hashing the content (volatile keys excluded)
# ---------------------------------------------------------------------------
def payload_hash(payload: dict) -> str:
    clean = {k: v for k, v in payload.items() if k not in _VOLATILE_KEYS}
    blob = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# suspend / enable switches
# ---------------------------------------------------------------------------
_suspend_lock = threading.Lock()
_suspend_count = 0
_flush_lock = threading.Lock()


@contextmanager
def suspend_auto_flush():
    """Used around reorganize / monitor / scan so the background flusher
    never writes into a folder that is mid-move."""
    global _suspend_count
    with _suspend_lock:
        _suspend_count += 1
    try:
        yield
    finally:
        with _suspend_lock:
            _suspend_count -= 1


def auto_flush_suspended() -> bool:
    return _suspend_count > 0


def auto_enabled(settings: dict) -> bool:
    return bool(settings.get("manifest_auto_enabled", True))


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
@dataclass
class ManifestRef:
    path: str
    data: dict

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)

    @property
    def is_owned_layout(self) -> bool:
        """True if the unit is the whole folder (vs. one file in a shared folder)."""
        owned = (self.data.get("unit") or {}).get("owned")
        if owned is None:                       # legacy manifest: plain appcatalog.json
            return os.path.basename(self.path).lower() == MANIFEST_FILENAME
        return bool(owned)

    @property
    def entry(self) -> dict:
        return ((self.data.get("unit") or {}).get("entry")) or {}

    @property
    def entry_name(self) -> str:
        return (self.entry.get("path") or "").replace("\\", "/")

    @property
    def orig_name(self) -> str:
        return (self.entry.get("orig_name") or "").replace("\\", "/")

    @property
    def confirmed(self) -> bool:
        return bool(self.entry.get("confirmed"))

    @property
    def app(self) -> dict:
        return self.data.get("app") or {}

    @property
    def variant(self) -> dict:
        return self.data.get("variant") or {}

    @property
    def trusted(self) -> bool:
        """The user's own work is in here (not just an auto-guess): a confirmed
        setup file, verified, a locked name, a manual move/merge into this app,
        or a name that came from a scrape."""
        return bool(self.confirmed or self.variant.get("verified")
                    or self.variant.get("pinned")
                    or self.app.get("scrape_status") == "scraped"
                    or "name" in (self.app.get("locked") or []))

    def member_names(self) -> set[str]:
        out = set()
        for m in (self.data.get("unit") or {}).get("members") or []:
            p = (m.get("path") or "").replace("\\", "/")
            if p:
                out.add(p.split("/", 1)[0].lower())
        return out


def read_manifest(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("manifest unreadable: %s (%s)", path, e)
        return None
    if not isinstance(data, dict) or data.get("kind") != KIND:
        return None
    try:
        if int(data.get("schema_version", 0)) > SCHEMA_VERSION:
            log.warning("manifest %s has a newer schema (%s) -- reading what we understand",
                        path, data.get("schema_version"))
    except (TypeError, ValueError):
        return None
    return data


def load_folder_manifests(folder: str, manifest_names: Iterable[str]) -> list[ManifestRef]:
    refs = []
    for n in manifest_names:
        p = os.path.join(folder, n)
        data = read_manifest(p)
        if data is not None:
            refs.append(ManifestRef(p, data))
    return refs


def folder_manifest_names(folder: str) -> list[str]:
    try:
        return [n for n in os.listdir(folder) if is_manifest_filename(n)]
    except OSError:
        return []


class ManifestCache:
    """Per-resolve cache: folder -> [ManifestRef]."""

    def __init__(self):
        self._c: dict[str, list[ManifestRef]] = {}

    def refs(self, folder: str) -> list[ManifestRef]:
        if folder not in self._c:
            self._c[folder] = load_folder_manifests(folder, folder_manifest_names(folder))
        return self._c[folder]

    def find(self, folder: str, primary_file_name: Optional[str]) -> Optional[ManifestRef]:
        if not primary_file_name:
            return None
        want = primary_file_name.replace("\\", "/").lower()
        for r in self.refs(folder):
            if r.entry_name.lower() == want or (r.orig_name and r.orig_name.lower() == want):
                return r
        return None


# ---------------------------------------------------------------------------
# scanner support: claim-first grouping
# ---------------------------------------------------------------------------
def claim_groups(file_names: list[str], auto_groups: list[list[str]],
                 refs: list[ManifestRef]) -> list[list[str]]:
    """Merge confirmed manifests into the scanner's installer grouping.

    A confirmed manifest pins its entry file as THE representative of its
    unit and claims every member file, so dependent files can never turn
    into bogus extra variants and a different .exe can never be promoted to
    setup file.  Unconfirmed manifests do not change scanning at all."""
    lower_to_actual = {n.lower(): n for n in file_names}
    entry_groups: list[list[str]] = []
    claimed: set[str] = set()
    for r in refs:
        if not r.confirmed:
            continue
        entry = r.entry_name.lower()
        if "/" in entry or entry not in lower_to_actual:
            continue
        entry_groups.append([lower_to_actual[entry]])
        claimed.add(entry)
        claimed |= {m for m in r.member_names() if m in lower_to_actual}
    if not entry_groups:
        return auto_groups
    rest = [g for g in auto_groups if not any(f.lower() in claimed for f in g)]
    return entry_groups + rest


def claimed_dir_names(refs: list[ManifestRef]) -> set[str]:
    """Lower-cased names of sub-folders that confirmed manifests list as
    members (Redist/, crack/, data/ ...): they belong to the unit and must
    never be walked as separate candidate apps."""
    out: set[str] = set()
    for r in refs:
        if not r.confirmed:
            continue
        for m in (r.data.get("unit") or {}).get("members") or []:
            p = (m.get("path") or "").replace("\\", "/")
            if m.get("type") == "dir" and p and "/" not in p:
                out.add(p.lower())
    return out


def confirmed_entry_for_folder(refs: list[ManifestRef]) -> Optional[str]:
    """For single-app subtree folders: the confirmed entry relpath, if any."""
    for r in refs:
        if r.confirmed and r.entry_name:
            return r.entry_name
    return None


# ---------------------------------------------------------------------------
# building the payload
# ---------------------------------------------------------------------------
_VARIANT_SQL = """
SELECT v.*,
       a.name AS a_name, a.name_locked AS a_name_locked,
       a.publisher AS a_publisher, a.description AS a_description,
       a.homepage_url AS a_homepage, a.license AS a_license,
       a.winget_id AS a_winget_id, a.choco_id AS a_choco_id,
       a.latest_version AS a_latest_version,
       a.scrape_status AS a_scrape_status, a.last_scraped AS a_last_scraped,
       a.status AS a_status, a.app_uid AS a_uid,
       a.catalog AS a_catalog, a.subcatalog AS a_subcatalog
FROM variants v JOIN apps a ON a.id = v.app_id
WHERE v.id = ?
"""


def _fetch_variant(conn, variant_id: int):
    return conn.execute(_VARIANT_SQL, (variant_id,)).fetchone()


def is_valuable(row) -> bool:
    """Auto mode writes only variants that carry human or scrape work; the
    resolver can recreate untouched auto-resolved ones from the files."""
    return bool(
        row["a_name_locked"] or row["version_locked"] or row["file_locked"]
        or row["is_ignored"] or row["a_status"] == "verified"
        or row["a_scrape_status"] == "scraped" or row["app_pinned"]
        or (row["name_source"] or "") in ("manual", "monitor")
    )


def _is_owned_folder(conn, folder: str, variant_id: int) -> bool:
    """True if this variant is the only one using `folder` and no other
    variant lives below it (so the whole folder IS this variant's unit)."""
    n = conn.execute(
        "SELECT COUNT(*) FROM variants WHERE source_path = ? AND id != ?",
        (folder, variant_id)).fetchone()[0]
    if n:
        return False
    prefix = folder.rstrip("\\/") + os.sep
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    n2 = conn.execute(
        "SELECT COUNT(*) FROM variants WHERE source_path LIKE ? ESCAPE '\\' AND id != ?",
        (like, variant_id)).fetchone()[0]
    return n2 == 0


_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename_part(text: str, limit: int = 110) -> str:
    t = _INVALID_FS_CHARS.sub("", text or "")
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:limit].rstrip(" .")


def manifest_base_name(payload: dict, entry_rel: Optional[str], owned: bool) -> str:
    """The personal file name for a manifest (see 'PLACEMENT AND NAMING')."""
    if not owned and entry_rel:
        base = _safe_filename_part(os.path.basename(entry_rel.replace("\\", "/")))
    else:
        app, var = payload.get("app") or {}, payload.get("variant") or {}
        parts = [app.get("name"), var.get("version"), var.get("edition"),
                 var.get("architecture"), var.get("language")]
        base = _safe_filename_part(" ".join(str(p) for p in parts if p))
    if not base and entry_rel:
        base = _safe_filename_part(os.path.basename(entry_rel.replace("\\", "/")))
    return (base or "variant") + SIBLING_SUFFIX


def same_unit(data: Optional[dict], variant_uid: Optional[str], entry_rel: Optional[str]) -> bool:
    """Does this manifest describe THE unit we are about to write?  (Same
    variant uid, or the same entry file in the same folder.)"""
    if not data:
        return False
    if variant_uid and (data.get("variant") or {}).get("variant_uid") == variant_uid:
        return True
    e = (((data.get("unit") or {}).get("entry")) or {}).get("path") or ""
    return bool(entry_rel and e and e.replace("\\", "/").lower() == entry_rel.replace("\\", "/").lower())


def _folder_manifests(folder: str) -> list[tuple[str, Optional[dict]]]:
    out = []
    for n in folder_manifest_names(folder):
        p = os.path.join(folder, n)
        out.append((p, read_manifest(p)))
    return out


def find_existing_manifest(folder: str, variant_uid: Optional[str], entry_rel: Optional[str],
                           hint_path: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """The manifest already describing this unit in `folder` (hint first)."""
    if hint_path and os.path.dirname(hint_path) == folder and os.path.isfile(hint_path):
        d = read_manifest(hint_path)
        if same_unit(d, variant_uid, entry_rel):
            return hint_path, d
    for p, d in _folder_manifests(folder):
        if same_unit(d, variant_uid, entry_rel):
            return p, d
    return None, None


def choose_manifest_path(folder: str, base_name: str, variant_uid: Optional[str],
                         entry_rel: Optional[str], own_previous: Optional[str] = None) -> str:
    """Target path for this unit's manifest.  A file that already exists
    there is only reused if it describes THIS unit; otherwise a short uid
    is added so another variant's manifest is never overwritten."""
    cand = os.path.join(folder, base_name)
    stem = base_name[: -len(SIBLING_SUFFIX)]
    tag = (variant_uid or "x")[:6]
    for attempt in range(6):
        if not os.path.exists(cand):
            return cand
        data = read_manifest(cand)
        if same_unit(data, variant_uid, entry_rel) or (data is None and own_previous == cand):
            return cand
        extra = tag if attempt == 0 else f"{tag}{attempt}"
        cand = os.path.join(folder, f"{stem} [{extra}]{SIBLING_SUFFIX}")
    return cand


def _classify_member(name: str, is_dir: bool, words: list[str]) -> str:
    n = name.lower()
    stem, ext = os.path.splitext(n)
    if any(w and w in n for w in words):
        return "companion"
    if not is_dir and ext in _COMPANION_EXTS:
        return "companion"
    return "required"


def _build_members(folder: str, entry_rel: Optional[str], owned: bool,
                   prev: Optional[dict], words: list[str]) -> list[dict]:
    prev_roles = {}
    for m in ((prev or {}).get("unit") or {}).get("members") or []:
        if m.get("path"):
            prev_roles[m["path"].lower()] = m.get("role")

    members: list[dict] = []
    entry_top = (entry_rel or "").replace("\\", "/").split("/", 1)[0].lower()

    if owned:
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            entries = []
        for e in entries:
            if is_manifest_filename(e.name):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            role = prev_roles.get(e.name.lower())
            if role is None:
                role = "entry" if (not is_dir and e.name.lower() == entry_top and "/" not in (entry_rel or "").replace("\\", "/")) \
                    else _classify_member(e.name, is_dir, words)
            if is_dir:
                files, nbytes, tfp = tree_stats(e.path)
                members.append({"path": e.name, "type": "dir", "role": role,
                                "files": files, "bytes": nbytes, "tree_fp": tfp})
            else:
                try:
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    size = None
                members.append({"path": e.name, "type": "file", "role": role, "size": size})
        return members

    # shared folder: just this variant's own file (+ its multi-part siblings)
    if entry_rel:
        group_key = None
        try:
            from scanner import _installer_group_key
            group_key = _installer_group_key(os.path.basename(entry_rel))
        except Exception:
            group_key = None
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        for n in sorted(names, key=str.lower):
            if is_manifest_filename(n):
                continue
            is_entry = n.lower() == os.path.basename(entry_rel).lower()
            same_group = False
            if group_key and not is_entry:
                try:
                    same_group = _installer_group_key(n) == group_key
                except Exception:
                    same_group = False
            if not (is_entry or same_group):
                continue
            full = os.path.join(folder, n)
            if not os.path.isfile(full):
                continue
            members.append({"path": n, "type": "file",
                            "role": "entry" if is_entry else "required",
                            "size": os.path.getsize(full)})
    return members


def build_payload(conn, variant_id: int, *, folder: Optional[str] = None,
                  file_name: Optional[str] = None, prev: Optional[dict] = None,
                  orig_name: Optional[str] = None, orig_fp: Optional[str] = None,
                  settings: Optional[dict] = None):
    """Returns (payload, folder, owned) or (None, folder, False) if the
    variant's folder is missing."""
    row = _fetch_variant(conn, variant_id)
    if row is None:
        return None, None, False
    settings = settings or {}
    folder = folder or row["source_path"]
    file_name = file_name if file_name is not None else row["file_name"]
    if not folder or not os.path.isdir(folder):
        return None, folder, False

    # an explicit destination folder (copy-mode reorganize) is dedicated by construction
    owned = _is_owned_folder(conn, folder, variant_id) if folder == row["source_path"] else True
    words = settings.get("manifest_companion_words") or DEFAULT_COMPANION_WORDS
    words = [w.lower() for w in words]

    entry_rel = (file_name or "").replace("\\", "/") or None
    entry_abs = os.path.join(folder, entry_rel.replace("/", os.sep)) if entry_rel else None
    entry_fp = quick_fingerprint(entry_abs) if entry_abs and os.path.isfile(entry_abs) else None
    entry_size = os.path.getsize(entry_abs) if entry_abs and os.path.isfile(entry_abs) else row["file_size"]

    prev_entry = ((prev or {}).get("unit") or {}).get("entry") or {}
    o_name = orig_name or prev_entry.get("orig_name")
    o_fp = orig_fp or prev_entry.get("orig_fp")
    # file renamed/archived since the last write (Setup.exe -> Setup.7z): keep both identities
    if not o_name and prev_entry.get("path") and entry_rel and prev_entry["path"] != entry_rel:
        if os.path.splitext(prev_entry["path"])[0].lower() == os.path.splitext(entry_rel)[0].lower():
            o_name, o_fp = prev_entry["path"], prev_entry.get("fp")

    confirmed = bool(row["file_locked"] or row["a_status"] == "verified"
                     or (row["name_source"] or "") == "monitor")

    tags = [r[0] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN app_tags at ON at.tag_id = t.id WHERE at.app_id = ?",
        (row["app_id"],))]
    # catalog / subcatalog names are mirrored into tags by the resolver --
    # that is category data, which manifests deliberately never carry
    skip = {(r[0] or "").lower() for r in conn.execute(
        "SELECT catalog FROM apps UNION SELECT subcatalog FROM apps")}
    tags = sorted(t for t in tags if t and t.lower() not in skip)

    original_name = (prev or {}).get("variant", {}).get("original_name")
    if not original_name and row["raw_candidate_id"]:
        rc = conn.execute("SELECT folder_path, primary_file_name FROM raw_candidates WHERE id = ?",
                          (row["raw_candidate_id"],)).fetchone()
        if rc:
            original_name = os.path.basename((rc["folder_path"] or "").rstrip("\\/")) or rc["primary_file_name"]

    app_locked = ["name"] if row["a_name_locked"] else []
    var_locked = []
    if row["version_locked"]:
        var_locked.append("version")
    if confirmed:
        var_locked.append("entry")

    entry = {"path": entry_rel, "kind": (row["file_type"] or "").lower() or None,
             "size": entry_size, "fp": entry_fp, "confirmed": confirmed}
    if o_name:
        entry["orig_name"] = o_name
    if o_fp:
        entry["orig_fp"] = o_fp

    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "written_by": WRITTEN_BY,
        "updated_at": _now_iso(),
        "app": {
            "app_uid": row["a_uid"],
            "name": row["a_name"],
            "locked": app_locked,
            "publisher": row["a_publisher"],
            "description": row["a_description"],
            "homepage": row["a_homepage"],
            "license": row["a_license"],
            "winget_id": row["a_winget_id"],
            "choco_id": row["a_choco_id"],
            "latest_version": row["a_latest_version"],
            "scrape_status": row["a_scrape_status"],
            "last_scraped": row["a_last_scraped"],
            "tags": tags,
        },
        "variant": {
            "variant_uid": row["variant_uid"],
            "version": row["version"],
            "edition": row["edition"],
            "architecture": row["architecture"],
            "language": row["language"],
            "ignored": bool(row["is_ignored"]),
            "verified": row["a_status"] == "verified",
            # manually moved / merged / split into this app (kept there on rescans)
            "pinned": bool(row["app_pinned"]),
            "locked": var_locked,
            "original_name": original_name,
            "name_source": row["name_source"],
        },
        "unit": {
            "root": ".",
            "owned": owned,
            "entry": entry,
            "members": _build_members(folder, entry_rel, owned, prev, words),
        },
    }
    return payload, folder, owned


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
@dataclass
class WriteResult:
    variant_id: int
    status: str            # created | updated | unchanged | skipped | failed
    path: Optional[str] = None
    message: str = ""


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def _remove_if_ours(path: Optional[str], variant_uid: Optional[str]) -> None:
    """Delete a stale manifest only if it demonstrably belongs to this variant."""
    if not path or not os.path.isfile(path) or not variant_uid:
        return
    data = read_manifest(path)
    if data and (data.get("variant") or {}).get("variant_uid") == variant_uid:
        try:
            os.remove(path)
        except OSError as e:
            log.warning("could not remove stale manifest %s: %s", path, e)


def write_variant_manifest(db, variant_id: int, *, force: bool = False, dry_run: bool = False,
                           folder: Optional[str] = None, file_name: Optional[str] = None,
                           record: bool = True, orig_name: Optional[str] = None,
                           orig_fp: Optional[str] = None,
                           settings: Optional[dict] = None) -> WriteResult:
    """Write (or refresh) one variant's manifest.

    force=False  auto mode: skipped unless the variant carries real work
                 (see is_valuable) -- the dirty flag is cleared either way.
    folder/file_name  explicit target (copy-mode reorganize writes into the
                 destination while the DB row still points at the source);
                 pair with record=False so DB bookkeeping is untouched.
    """
    conn = db.connect()
    try:
        row = _fetch_variant(conn, variant_id)
        if row is None:
            return WriteResult(variant_id, "skipped", message="variant no longer exists")
        settings = settings if settings is not None else db.get_all_settings()

        if not force and not is_valuable(row):
            if record and not dry_run:
                conn.execute("UPDATE variants SET manifest_dirty = 0 WHERE id = ?", (variant_id,))
                conn.commit()
            return WriteResult(variant_id, "skipped", message="nothing manual or scraped to preserve")

        eff_folder = folder or row["source_path"]
        eff_file = file_name if file_name is not None else row["file_name"]
        if not eff_folder or not os.path.isdir(eff_folder):
            msg = "folder not found"
            if record and not dry_run:
                conn.execute("UPDATE variants SET manifest_dirty = 0, manifest_error = ? WHERE id = ?",
                             (msg, variant_id))
                conn.commit()
            return WriteResult(variant_id, "skipped", message=msg)

        owned = _is_owned_folder(conn, eff_folder, variant_id) if not folder else True
        uid = row["variant_uid"]
        entry_rel = (eff_file or "").replace("\\", "/") or None
        # the manifest that already describes this unit (any file name, incl. the
        # legacy plain appcatalog.json): its manual roles / original name are kept
        prev_path, prev = find_existing_manifest(
            eff_folder, uid, entry_rel, row["manifest_path"] if record else None)

        payload, _, _ = build_payload(conn, variant_id, folder=eff_folder, file_name=eff_file,
                                      prev=prev, orig_name=orig_name, orig_fp=orig_fp,
                                      settings=settings)
        if payload is None:
            return WriteResult(variant_id, "skipped", message="folder not found")
        if folder:                        # explicit destination: force owned-style content
            payload["unit"]["members"] = _build_members(
                eff_folder, (eff_file or "").replace("\\", "/") or None, True, prev,
                [w.lower() for w in (settings.get("manifest_companion_words") or DEFAULT_COMPANION_WORDS)])

        # personal file name; never overwrites another variant's manifest
        target = choose_manifest_path(
            eff_folder, manifest_base_name(payload, entry_rel, owned), uid, entry_rel,
            own_previous=prev_path)
        new_hash = payload_hash(payload)
        existing = read_manifest(target) if os.path.isfile(target) else None
        same = existing is not None and payload_hash(existing) == new_hash

        if dry_run:
            return WriteResult(variant_id, "unchanged" if same else ("updated" if (existing or prev) else "created"),
                               path=target)

        status = "unchanged"
        if not same:
            _atomic_write_json(target, payload)
            status = "updated" if (existing or prev) else "created"
        # tidy: older / differently-named manifests of THIS unit in the folder
        # (legacy appcatalog.json, the name before an app rename ...)
        for p, d in _folder_manifests(eff_folder):
            if p != target and same_unit(d, uid, entry_rel):
                try:
                    os.remove(p)
                    if status == "unchanged":
                        status = "updated"
                except OSError as e:
                    log.warning("could not remove superseded manifest %s: %s", p, e)
        old = row["manifest_path"]
        if record and old and old != target and not folder:
            _remove_if_ours(old, uid)

        if record:
            conn.execute(
                "UPDATE variants SET manifest_hash = ?, manifest_dirty = 0, manifest_path = ?, "
                "manifest_written_at = ?, manifest_error = NULL WHERE id = ?",
                (new_hash, target, _now_iso(), variant_id))
            conn.commit()
        return WriteResult(variant_id, status, path=target)

    except Exception as e:                                   # per-item isolation
        msg = f"{type(e).__name__}: {e}"
        log.warning("manifest write failed for variant %s: %s", variant_id, msg)
        if record and not dry_run:
            try:
                conn.execute("UPDATE variants SET manifest_dirty = 0, manifest_error = ? WHERE id = ?",
                             (msg, variant_id))
                conn.commit()
            except Exception:
                pass
        return WriteResult(variant_id, "failed", message=msg)


@dataclass
class BatchResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    items: list = field(default_factory=list)

    def add(self, r: WriteResult):
        setattr(self, r.status, getattr(self, r.status) + 1)
        if r.status in ("failed", "skipped"):
            self.items.append(r)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged + self.skipped + self.failed

    def summary(self) -> str:
        return (f"{self.created} created, {self.updated} updated, {self.unchanged} unchanged, "
                f"{self.skipped} skipped, {self.failed} failed")


def variant_ids_for_apps(db, app_ids: Iterable[int]) -> list[int]:
    ids = list(app_ids)
    out: list[int] = []
    conn = db.connect()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        out += [r[0] for r in conn.execute(
            f"SELECT id FROM variants WHERE app_id IN ({q}) ORDER BY id", chunk)]
    return out


def write_manifests(db, variant_ids: Optional[Iterable[int]] = None, *, dry_run: bool = False,
                    progress: Optional[Callable[[int, int, str], None]] = None,
                    cancel: Optional[Callable[[], bool]] = None) -> BatchResult:
    """Manual 'Create / update manifests': always writes (force=True)."""
    conn = db.connect()
    if variant_ids is None:
        ids = [r[0] for r in conn.execute("SELECT id FROM variants ORDER BY id")]
    else:
        ids = list(variant_ids)
    settings = db.get_all_settings()
    res = BatchResult()
    total = len(ids)
    for n, vid in enumerate(ids, start=1):
        if cancel and cancel():
            res.cancelled = True
            break
        r = write_variant_manifest(db, vid, force=True, dry_run=dry_run, settings=settings)
        res.add(r)
        if progress:
            progress(n, total, r.path or "")
    return res


def flush_dirty(db, *, limit: int = 500) -> BatchResult:
    """Auto mode: write every dirty variant that carries real work."""
    res = BatchResult()
    if auto_flush_suspended():
        return res
    if not _flush_lock.acquire(blocking=False):
        return res
    try:
        settings = db.get_all_settings()
        if not auto_enabled(settings):
            return res
        only_valuable = bool(settings.get("manifest_auto_only_valuable", True))
        conn = db.connect()
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM variants WHERE manifest_dirty = 1 ORDER BY id LIMIT ?", (limit,))]
        for vid in ids:
            if auto_flush_suspended():
                break
            res.add(write_variant_manifest(db, vid, force=not only_valuable, settings=settings))
        return res
    finally:
        _flush_lock.release()


def has_dirty(db) -> bool:
    try:
        return db.connect().execute(
            "SELECT 1 FROM variants WHERE manifest_dirty = 1 LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def manifest_stats(db) -> dict:
    c = db.connect()
    q = lambda s: c.execute(s).fetchone()[0]
    return {
        "variants": q("SELECT COUNT(*) FROM variants"),
        "with_manifest": q("SELECT COUNT(*) FROM variants WHERE manifest_hash IS NOT NULL"),
        "pending": q("SELECT COUNT(*) FROM variants WHERE manifest_dirty = 1"),
        "errors": q("SELECT COUNT(*) FROM variants WHERE manifest_error IS NOT NULL"),
    }


def remove_manifests(db, variant_ids: Iterable[int]) -> int:
    """Delete the manifest files of the given variants (only ones that are ours)."""
    conn = db.connect()
    n = 0
    for vid in variant_ids:
        row = conn.execute("SELECT variant_uid, manifest_path FROM variants WHERE id = ?", (vid,)).fetchone()
        if row and row["manifest_path"] and os.path.isfile(row["manifest_path"]):
            _remove_if_ours(row["manifest_path"], row["variant_uid"])
            n += 1
        conn.execute("UPDATE variants SET manifest_hash = NULL, manifest_path = NULL, "
                     "manifest_written_at = NULL, manifest_dirty = 0 WHERE id = ?", (vid,))
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# integrity (is everything the manifest lists still there?)
# ---------------------------------------------------------------------------
def check_unit(ref: ManifestRef) -> dict:
    folder = ref.folder
    res = {"entry_exists": False, "entry_size_ok": None, "missing": [], "changed": [], "extras": []}
    entry = ref.entry
    if entry.get("path"):
        ep = os.path.join(folder, entry["path"].replace("/", os.sep))
        res["entry_exists"] = os.path.isfile(ep)
        if res["entry_exists"] and entry.get("size") is not None:
            res["entry_size_ok"] = os.path.getsize(ep) == entry["size"]
    listed = set()
    for m in (ref.data.get("unit") or {}).get("members") or []:
        p = m.get("path")
        if not p:
            continue
        listed.add(p.split("/", 1)[0].lower())
        if m.get("role") not in ("entry", "required"):
            continue
        full = os.path.join(folder, p.replace("/", os.sep))
        if not os.path.exists(full):
            res["missing"].append(p)
        elif m.get("type") == "file" and m.get("size") is not None and os.path.getsize(full) != m["size"]:
            res["changed"].append(p)
    if ref.is_owned_layout:
        try:
            for n in os.listdir(folder):
                if n.lower() not in listed and not is_manifest_filename(n):
                    res["extras"].append(n)
        except OSError:
            pass
    return res


# ---------------------------------------------------------------------------
# resolver support: apply a manifest to the DB
# ---------------------------------------------------------------------------
def override_fields_from_manifest(fields, ref: ManifestRef) -> bool:
    """For a variant that does NOT exist in the DB yet (fresh DB / new file):
    let a trusted manifest decide name / version / edition / arch / language
    instead of the heuristics.  Returns True if applied.

    Every manifest counts, not only 'trusted' ones: it was written from the
    database's own state, so using it reproduces that state.  (Falling back to
    heuristics for an untrusted manifest is what let the fuzzy clustering glue
    differently-named apps back together.)"""
    name = (ref.app.get("name") or "").strip()
    if not name:
        return False
    if fields.clean_name and fields.clean_name.lower() != name.lower():
        fields.alt_name_candidate = fields.clean_name
        fields.alt_name_source = fields.name_source
    fields.clean_name = name
    fields.name_source = "manifest"
    fields.extraction_confidence = 1.0
    v = ref.variant
    for attr, key in (("version", "version"), ("edition", "edition"),
                      ("architecture", "architecture"), ("language", "language")):
        if v.get(key):
            setattr(fields, attr, v[key])
    return True


def _fill_empty_app(conn, app_id: int, app: dict) -> None:
    row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    mapping = (("publisher", "publisher"), ("description", "description"),
               ("homepage_url", "homepage"), ("license", "license"),
               ("winget_id", "winget_id"), ("choco_id", "choco_id"),
               ("latest_version", "latest_version"))
    updates = {col: app[key] for col, key in mapping if app.get(key) and not row[col]}
    if app.get("scrape_status") == "scraped" and row["scrape_status"] != "scraped":
        updates["scrape_status"] = "scraped"
        if app.get("last_scraped") and not row["last_scraped"]:
            updates["last_scraped"] = app["last_scraped"]
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE apps SET {sets}, updated_at = datetime('now') WHERE id = ?",
                     (*updates.values(), app_id))
    for t in app.get("tags") or []:
        if not t:
            continue
        r = conn.execute("SELECT id FROM tags WHERE name = ?", (t,)).fetchone()
        tid = r["id"] if r else conn.execute("INSERT INTO tags (name) VALUES (?)", (t,)).lastrowid
        conn.execute("INSERT OR IGNORE INTO app_tags (app_id, tag_id) VALUES (?, ?)", (app_id, tid))


def apply_manifest(conn, ref: ManifestRef, *, app_id: int, variant_id: int,
                   app_created: bool, variant_created: bool) -> str:
    """Apply a manifest to DB rows.  Returns one of:
        applied | in_sync | db_wins_no_baseline | conflict_db_wins

    Precedence (so an existing, hand-organised catalog.db is never degraded):
      * variant created in THIS resolve (fresh DB / new file): manifest applies fully.
      * existing variant, manifest content unchanged since we last wrote/applied
        it: nothing to do.
      * existing variant, manifest changed on disk, DB has no pending edits
        (manifest_dirty = 0): manifest applies (it is newer).
      * existing variant, manifest changed AND DB has unsaved edits: DB wins,
        conflict is audit-logged; the DB version is rewritten at next flush.
      * existing variant with no baseline (DB predates manifests): DB wins; only
        EMPTY app details are filled in from the manifest.
    DB locks (name / version / file) always win over manifest values.
    """
    app, var = ref.app, ref.variant
    mh = payload_hash(ref.data)
    vrow = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    arow = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    if vrow is None or arow is None:
        return "in_sync"

    # ---- app level: always safe (names/locks only when the app is new) --------
    if app_created:
        sets = {}
        if app.get("name"):
            sets["name"] = app["name"]
            if "name" in (app.get("locked") or []):
                sets["name_locked"] = 1
        if var.get("verified"):
            sets["status"] = "verified"
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(f"UPDATE apps SET {cols} WHERE id = ?", (*sets.values(), app_id))
    uid = app.get("app_uid")
    if uid and (app_created or not arow["app_uid"]):
        clash = conn.execute("SELECT 1 FROM apps WHERE app_uid = ? AND id != ?", (uid, app_id)).fetchone()
        if not clash:
            conn.execute("UPDATE apps SET app_uid = ? WHERE id = ?", (uid, app_id))
    _fill_empty_app(conn, app_id, app)

    # ---- variant level ------------------------------------------------------
    baseline = vrow["manifest_hash"]
    if variant_created:
        mode = "full"
    elif baseline == mh:
        mode = "none"
    elif baseline is None:
        mode = "fill"
    elif vrow["manifest_dirty"]:
        mode = "conflict"
    else:
        mode = "full"

    result = "applied"
    if mode == "none":
        result = "in_sync"
    elif mode == "conflict":
        conn.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
            ("variant", variant_id, "manifest_conflict",
             json.dumps({"manifest": ref.path, "note": "DB has unsaved edits; DB kept"})))
        log.warning("manifest conflict (DB kept): %s", ref.path)
        result = "conflict_db_wins"
    else:
        sets = {}
        for col in ("edition", "architecture", "language"):
            val = var.get(col)
            if val and (mode == "full" or not vrow[col]):
                sets[col] = val
        if var.get("version") and not vrow["version_locked"] and (mode == "full" or not vrow["version"]):
            sets["version"] = var["version"]
            if "version" in (var.get("locked") or []) and mode == "full":
                sets["version_locked"] = 1
        if mode == "full":
            if var.get("pinned") and not vrow["app_pinned"]:
                sets["app_pinned"] = 1
            if var.get("ignored") and not vrow["is_ignored"]:
                sets["is_ignored"] = 1
            if ref.confirmed and not vrow["file_locked"]:
                sets["file_locked"] = 1
        uid_v = var.get("variant_uid")
        if uid_v and mode == "full":
            clash = conn.execute("SELECT 1 FROM variants WHERE variant_uid = ? AND id != ?",
                                 (uid_v, variant_id)).fetchone()
            if not clash:
                sets["variant_uid"] = uid_v
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(f"UPDATE variants SET {cols} WHERE id = ?", (*sets.values(), variant_id))
        if mode == "fill":
            result = "db_wins_no_baseline"

    # ---- integrity (never silent) ----------------------------------------
    if mode in ("full", "fill"):
        chk = check_unit(ref)
        problems = {k: v for k, v in (("missing", chk["missing"]), ("changed", chk["changed"])) if v}
        if not chk["entry_exists"]:
            problems["entry_missing"] = [ref.entry_name]
        if problems:
            conn.execute(
                "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
                ("variant", variant_id, "manifest_integrity",
                 json.dumps({"manifest": ref.path, **problems})))
            conn.execute("UPDATE apps SET status = 'needs_review' WHERE id = ? AND status != 'verified'",
                         (app_id,))
            log.warning("manifest integrity problem in %s: %s", ref.path, problems)

    # baseline = what the manifest said; our own updates above must not look like edits
    if mode in ("full", "none"):
        conn.execute("UPDATE variants SET manifest_hash = ?, manifest_dirty = 0, manifest_path = ? WHERE id = ?",
                     (mh, ref.path, variant_id))
    return result
