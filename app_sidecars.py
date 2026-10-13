"""
app_sidecars.py -- the sidecars of a variant (checkpoint 36).

A SIDECAR is a file or folder that belongs with a variant's installer: readme,
crack / keygen, plugins, skins, tutorials, dependent folders, the other parts of a
multi-part archive ... Until now the app decided this implicitly (Reorganize
copied "whatever is unclaimed" next to a shared-folder installer, or moved the
whole folder); the user could neither see nor change it.

This module makes it explicit.

AUTOMATIC DETECTION (same rules Reorganize uses for a shared folder)
    everything directly inside the variant's folder EXCEPT
      * the installer itself (or the top-level folder that contains it),
      * junk (Thumbs.db, desktop.ini ...) and metadata files (*.appcatalog.json),
      * files claimed by another variant,
      * (shared folders only) a folder that is, or contains, another variant's
        install unit, and anything larger than the sidecar cap.

MANUAL OVERRIDES (``variant_sidecars``)
    state 'added'    this path is a sidecar even though detection would not list it
                     (any file / folder anywhere inside the variant's folder)
    state 'removed'  this automatically detected path is NOT a sidecar

    effective sidecars = (automatic - removed) + added

Overrides are stored in the database, mirrored into the variant's metadata file
(``unit.sidecars``) so they survive a lost database, and used by
  * the variants table / Sidecars dialog (what is considered a sidecar),
  * Reorganize (shared-folder copies, and "compress the whole variant"),
  * the metadata file's member list.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("appcatalog.sidecars")

DEFAULT_MAX_SIDECAR_GB = 5.0
_JUNK = {"thumbs.db", "desktop.ini", ".ds_store"}
_ORIGIN_ORDER = {"auto": 0, "added": 1, "excluded": 2}


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def ensure_sidecar_schema(conn: sqlite3.Connection) -> None:
    """Additive + idempotent. Call AFTER the manifest schema (the triggers touch
    variants.manifest_dirty)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS variant_sidecars (
               variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
               rel_path   TEXT    NOT NULL,
               state      TEXT    NOT NULL CHECK (state IN ('added', 'removed')),
               created_at TEXT    NOT NULL DEFAULT (datetime('now')),
               PRIMARY KEY (variant_id, rel_path)
           )""")
    for name, event, ref in (("ins", "INSERT", "NEW"), ("upd", "UPDATE", "NEW"), ("del", "DELETE", "OLD")):
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS trg_sidecars_{name} AFTER {event} ON variant_sidecars
                BEGIN UPDATE variants SET manifest_dirty = 1 WHERE id = {ref}.variant_id; END""")
    conn.commit()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _norm(p: Optional[str]) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""


def _slash(p: str) -> str:
    return (p or "").replace("\\", "/")


def _is_manifest_name(name: str) -> bool:
    n = name.lower()
    return n == "appcatalog.json" or n.endswith(".appcatalog.json")


def _exceeds(path: str, cap_bytes: int) -> bool:
    """True if the file / folder is bigger than cap_bytes (stops counting early)."""
    if not cap_bytes:
        return False
    try:
        if os.path.isfile(path):
            return os.path.getsize(path) > cap_bytes
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
                if total > cap_bytes:
                    return True
    except OSError:
        pass
    return False


def path_size(path: str) -> int:
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
        return total
    except OSError:
        return 0


@dataclass
class SidecarItem:
    rel_path: str                  # '/'-separated, relative to the variant's folder
    abs_path: str
    is_dir: bool
    origin: str                    # 'auto' | 'added' | 'excluded'
    exists: bool = True
    size: Optional[int] = None     # filled on request only (folders can be large)


class SidecarContext:
    """Library-wide facts needed to detect sidecars, loaded once and shared by
    every variant of a batch (one query instead of one per variant)."""

    def __init__(self, conn: sqlite3.Connection, settings: Optional[dict] = None):
        self.conn = conn
        rows = conn.execute("SELECT id, source_path, file_name FROM variants").fetchall()
        self.unit_paths: set = set()
        self.claimed: dict = {}            # folder -> {variant_id: top-level name, lower}
        self.folder_users: dict = {}       # folder -> number of variants
        for r in rows:
            if not r["source_path"]:
                continue
            n = _norm(r["source_path"])
            self.unit_paths.add(n)
            self.folder_users[n] = self.folder_users.get(n, 0) + 1
            if r["file_name"]:
                self.claimed.setdefault(n, {})[r["id"]] = _slash(r["file_name"]).split("/", 1)[0].lower()
        gb = DEFAULT_MAX_SIDECAR_GB
        try:
            gb = float((settings or {}).get("reorganize_max_sidecar_gb", DEFAULT_MAX_SIDECAR_GB))
        except (TypeError, ValueError):
            pass
        self.cap_bytes = int(gb * 1024 ** 3) if gb and gb > 0 else 0

    def contains_unit(self, path: str) -> bool:
        n = _norm(path).rstrip(os.sep)
        prefix = n + os.sep
        return n in self.unit_paths or any(p.startswith(prefix) for p in self.unit_paths)

    def is_shared(self, folder: str) -> bool:
        """Several variants use the folder, or it contains other variants' folders."""
        n = _norm(folder).rstrip(os.sep)
        if self.folder_users.get(n, 0) > 1:
            return True
        prefix = n + os.sep
        return any(p.startswith(prefix) for p in self.unit_paths)


# ---------------------------------------------------------------------------
# overrides
# ---------------------------------------------------------------------------
def overrides_for(conn, variant_id: int) -> dict:
    """{rel_path_lower: (rel_path, state)}"""
    return {r["rel_path"].lower(): (r["rel_path"], r["state"])
            for r in conn.execute("SELECT rel_path, state FROM variant_sidecars WHERE variant_id = ?",
                                  (variant_id,))}


def all_overrides(conn) -> dict:
    """{variant_id: {rel_lower: (rel, state)}} for every variant that has any."""
    out: dict = {}
    for r in conn.execute("SELECT variant_id, rel_path, state FROM variant_sidecars"):
        out.setdefault(r["variant_id"], {})[r["rel_path"].lower()] = (r["rel_path"], r["state"])
    return out


def export_overrides(conn, variant_id: int) -> dict:
    """For the metadata file: {'added': [...], 'removed': [...]} (empty lists omitted)."""
    ov = overrides_for(conn, variant_id)
    out = {}
    added = sorted(rel for rel, st in ov.values() if st == "added")
    removed = sorted(rel for rel, st in ov.values() if st == "removed")
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    return out


def import_overrides(conn, variant_id: int, data: dict) -> int:
    """Restore overrides from a metadata file (only if the variant has none yet)."""
    if not isinstance(data, dict):
        return 0
    if conn.execute("SELECT 1 FROM variant_sidecars WHERE variant_id = ? LIMIT 1", (variant_id,)).fetchone():
        return 0
    n = 0
    for state, key in (("added", "added"), ("removed", "removed")):
        for rel in data.get(key) or []:
            if isinstance(rel, str) and rel:
                conn.execute("INSERT OR IGNORE INTO variant_sidecars (variant_id, rel_path, state) VALUES (?,?,?)",
                             (variant_id, _slash(rel), state))
                n += 1
    return n


def clear_overrides(db, variant_id: int) -> None:
    conn = db.connect()
    conn.execute("DELETE FROM variant_sidecars WHERE variant_id = ?", (variant_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def _variant_row(conn, variant_id: int):
    return conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()


def auto_items(ctx: SidecarContext, variant) -> list:
    """Automatically detected sidecars (origin 'auto'), top-level items of the folder."""
    folder = variant["source_path"]
    if not folder or not os.path.isdir(folder):
        return []
    entry = _slash(variant["file_name"] or "")
    entry_top = entry.split("/", 1)[0].lower() if entry else ""
    shared = ctx.is_shared(folder)
    claimed_by_others = {name for vid, name in ctx.claimed.get(_norm(folder), {}).items()
                         if vid != variant["id"]}
    items = []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return []
    for name in names:
        low = name.lower()
        if low == entry_top or low in _JUNK or _is_manifest_name(name):
            continue
        full = os.path.join(folder, name)
        is_dir = os.path.isdir(full) and not os.path.islink(full)
        if shared:
            if low in claimed_by_others:
                continue
            if is_dir and ctx.contains_unit(full):
                continue                              # another app's folder
            if _exceeds(full, ctx.cap_bytes):
                continue                              # far more likely an uncatalogued app
        items.append(SidecarItem(rel_path=name, abs_path=full, is_dir=is_dir, origin="auto"))
    return items


def list_sidecars(db, variant_id: int, ctx: Optional[SidecarContext] = None,
                  with_sizes: bool = False) -> list:
    """Every sidecar of the variant: automatic, added, and automatic-but-excluded."""
    return list_sidecars_conn(db.connect(), db.get_all_settings(), variant_id, ctx, with_sizes)


def list_sidecars_conn(conn, settings: Optional[dict], variant_id: int,
                       ctx: Optional[SidecarContext] = None, with_sizes: bool = False) -> list:
    v = _variant_row(conn, variant_id)
    if v is None:
        return []
    ctx = ctx or SidecarContext(conn, settings)
    ov = overrides_for(conn, variant_id)
    out = []
    auto_rels = set()
    for it in auto_items(ctx, v):
        auto_rels.add(it.rel_path.lower())
        if ov.get(it.rel_path.lower(), (None, None))[1] == "removed":
            it.origin = "excluded"
        out.append(it)
    folder = v["source_path"]
    for low, (rel, state) in ov.items():
        if state != "added" or low in auto_rels:
            continue
        full = os.path.join(folder, rel.replace("/", os.sep))
        out.append(SidecarItem(rel_path=rel, abs_path=full, is_dir=os.path.isdir(full),
                               origin="added", exists=os.path.exists(full)))
    if with_sizes:
        for it in out:
            it.size = path_size(it.abs_path) if it.exists else 0
    out.sort(key=lambda i: (_ORIGIN_ORDER[i.origin], i.rel_path.lower()))
    return out


def effective_rels(db, variant_id: int, ctx: Optional[SidecarContext] = None) -> list:
    """Relative paths that count as sidecars right now (automatic + added, present on disk)."""
    return [i.rel_path for i in list_sidecars(db, variant_id, ctx) if i.origin != "excluded" and i.exists]


def summaries(db, variants: list) -> dict:
    """{variant_id: {'count': n, 'excluded': m, 'names': [first few]}} for the variants table."""
    conn = db.connect()
    ctx = SidecarContext(conn, db.get_all_settings())
    out = {}
    for v in variants:
        items = list_sidecars(db, v["id"], ctx)
        live = [i for i in items if i.origin != "excluded"]
        out[v["id"]] = {"count": len(live), "excluded": len(items) - len(live),
                        "names": [i.rel_path + ("/" if i.is_dir else "") for i in live[:12]],
                        "more": max(0, len(live) - 12)}
    return out


# ---------------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------------
def add_sidecar(db, variant_id: int, path: str) -> str:
    """Make `path` (a file or folder inside the variant's folder) a sidecar.
    Returns 'added', 'restored' or 'already'. Raises ValueError with a reason."""
    conn = db.connect()
    v = _variant_row(conn, variant_id)
    if v is None:
        raise ValueError("Variant not found.")
    folder = v["source_path"]
    if not folder or not os.path.isdir(folder):
        raise ValueError("The variant's folder no longer exists.")
    if not os.path.exists(path):
        raise ValueError(f"Not found:\n{path}")
    rel = os.path.relpath(os.path.normpath(path), os.path.normpath(folder))
    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        raise ValueError("A sidecar must be inside the variant's own folder:\n" + folder)
    rel = _slash(rel)
    entry = _slash(v["file_name"] or "")
    if entry and (rel.lower() == entry.lower() or entry.lower().startswith(rel.lower() + "/")):
        raise ValueError("That is the installer itself (or the folder that holds it).")
    ov = overrides_for(conn, variant_id)
    state = ov.get(rel.lower(), (None, None))[1]
    if state == "removed":
        conn.execute("DELETE FROM variant_sidecars WHERE variant_id = ? AND lower(rel_path) = ?",
                     (variant_id, rel.lower()))
        conn.commit()
        return "restored"
    if state == "added":
        return "already"
    ctx = SidecarContext(conn, db.get_all_settings())
    if rel.lower() in {i.rel_path.lower() for i in auto_items(ctx, v)}:
        return "already"
    conn.execute("INSERT INTO variant_sidecars (variant_id, rel_path, state) VALUES (?,?, 'added')",
                 (variant_id, rel))
    conn.commit()
    return "added"


def remove_sidecar(db, variant_id: int, rel_path: str) -> str:
    """Take `rel_path` out of the sidecars: an added one is forgotten, an automatic one
    is excluded. Returns 'forgotten', 'excluded' or 'nothing'."""
    conn = db.connect()
    rel = _slash(rel_path)
    st = overrides_for(conn, variant_id).get(rel.lower(), (None, None))[1]
    if st == "added":
        conn.execute("DELETE FROM variant_sidecars WHERE variant_id = ? AND lower(rel_path) = ?",
                     (variant_id, rel.lower()))
        conn.commit()
        return "forgotten"
    if st == "removed":
        return "nothing"
    conn.execute("INSERT OR REPLACE INTO variant_sidecars (variant_id, rel_path, state) VALUES (?,?, 'removed')",
                 (variant_id, rel))
    conn.commit()
    return "excluded"


def restore_sidecar(db, variant_id: int, rel_path: str) -> None:
    """Undo an exclusion: the automatically detected item counts again."""
    conn = db.connect()
    conn.execute("DELETE FROM variant_sidecars WHERE variant_id = ? AND lower(rel_path) = ? AND state = 'removed'",
                 (variant_id, _slash(rel_path).lower()))
    conn.commit()
