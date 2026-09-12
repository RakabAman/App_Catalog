"""
scraper.py -- Winget manifest + Chocolatey + winget-show enrichment, all in
one module (merged from the former manifest.py / winget_show.py / enrich.py
package for a flatter, single-file-per-concern project layout -- see
AI_MODULE_REFERENCE.md for the full file map and function index).

Three source types feed app metadata:
  1. WINGET MANIFEST (svrooij/winget-pkgs-index) -- a large, cached,
     offline JSON index. Exact-name lookup only (ManifestEntry/
     ManifestLoadResult/load_manifest/build_lookup/cache_path_for_db), plus
     a fuzzy manual search (search_manifest) for when the exact key misses.
  2. WINGET SHOW (winget_show CLI) -- one real package's full metadata,
     fetched on demand (WingetShowResult/fetch_winget_show/
     is_winget_available) since the manifest itself has no description/
     publisher/license fields at all.
  3. CHOCOLATEY (live search, see choco_search.py, a separate file kept
     independent since it's the piece most likely to need standalone
     tweaking against Chocolatey's own site changes) -- not implemented in
     this file; combined with the above only at the GUI layer.

Two entry points glue all of this to the `apps` table:
  - run_scrape()              background batch enrichment (Winget manifest
                               only, by design; see its docstring)
  - apply_manifest_candidate() / apply_choco_candidate()
                               manual, single-app, user-picked candidate
                               application (used by the GUI's
                               SearchMatchDialog)
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

from app_paths import get_manifest_dir
from database import Database
from resolver import _ensure_tag, normalize_key

log = logging.getLogger("appcatalog.scraper")

REQUEST_TIMEOUT = 60


# ======================================================================
# Part 1: Winget manifest (index.v2.json) -- cache, lookup, fuzzy search
# ======================================================================

@dataclass
class ManifestEntry:
    winget_id: str
    name: str
    version: str
    tags: list[str] = field(default_factory=list)
    company: str = ""
    description: str = ""
    last_update: str = ""


@dataclass
class ManifestLoadResult:
    lookup: dict[str, ManifestEntry]
    source: str          # "cache" | "network" | "stale_cache_fallback" | "none"
    fetched_at: Optional[float] = None   # unix timestamp of the cache file used
    entry_count: int = 0
    error: Optional[str] = None


def cache_path_for_db(db_path: str) -> Path:
    """
    The manifest cache lives in manifest/ next to catalog.db (checkpoint
    21 -- used to be dropped bare next to the .db, moved into its own
    subfolder alongside every other external-metadata cache file, see
    app_paths.py's module docstring), not in a hidden user-profile folder
    -- this app is already fully self-contained per-database (settings,
    scan roots, everything live in the .db file itself), so keeping the
    cache alongside it keeps that same "one folder is the whole install"
    property, and makes it obvious/easy to delete.
    """
    return get_manifest_dir(db_path) / "winget_manifest_cache.json"


# ----------------------------------------------------------------------
# Winutil applications.json (ChrisTitusTech/winutil) – curated metadata
# ----------------------------------------------------------------------

def cache_path_for_winutil(db_path: str) -> Path:
    """Cache file for winutil applications.json, stored in manifest/ next
    to catalog.db (checkpoint 21 -- see cache_path_for_db above)."""
    return get_manifest_dir(db_path) / "winutil_apps_cache.json"


def load_winutil_apps(db_path: str, settings: dict, force_refresh: bool = False) -> dict:
    """
    Load and cache the ChrisTitusTech/winutil applications.json.
    Returns a dict: winget_id (lowercased) -> full app entry.
    Uses the same staleness window as the winget manifest.
    """
    url = settings.get("scraper_winutil_apps_url",
                       "https://raw.githubusercontent.com/ChrisTitusTech/winutil/main/config/applications.json")
    staleness_seconds = float(settings.get("scraper_manifest_staleness_hours", 4)) * 3600
    cache_path = cache_path_for_winutil(db_path)

    # Try cache if not forced and fresh enough
    if not force_refresh and cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < staleness_seconds:
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                # Build lookup by winget id (lowercase)
                lookup = {v.get("winget", "").lower(): v for v in data.values() if v.get("winget")}
                log.info("Loaded winutil apps from cache: %d entries", len(lookup))
                return lookup
            except Exception as e:
                log.warning("Failed to read cached winutil apps: %s", e)

    # Fetch fresh
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": "appcatalog-scraper/1.0"})
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected shape: expected JSON object")
        cache_path.write_text(json.dumps(data), encoding="utf-8")
        lookup = {v.get("winget", "").lower(): v for v in data.values() if v.get("winget")}
        log.info("Fetched winutil apps: %d entries", len(lookup))
        return lookup
    except Exception as e:
        log.warning("Winutil fetch failed: %s", e)
        # Fall back to stale cache if available
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                lookup = {v.get("winget", "").lower(): v for v in data.values() if v.get("winget")}
                return lookup
            except Exception:
                pass
    return {}


def _guess_company(package_id: str) -> str:
    """
    Heuristic company name from a Winget PackageId's publisher segment
    (the part before the first dot, e.g. "Microsoft.VisualStudioCode" ->
    "Microsoft"). Documented in the report as unreliable -- numeric/generic
    publisher ids ("0-don.clippy") produce a nonsense-looking company name,
    which is an accepted limitation of the raw index having no real
    publisher field at all.
    """
    if not package_id or "." not in package_id:
        return package_id or ""
    first = package_id.split(".", 1)[0]
    words = first.replace("-", " ").replace("_", " ").split()
    return " ".join(w[:1].upper() + w[1:] if w else w for w in words)


def _entry_from_raw(raw: dict) -> Optional[ManifestEntry]:
    package_id = (raw.get("PackageId") or "").strip()
    name = (raw.get("Name") or "").strip()
    if not package_id or not name:
        return None
    return ManifestEntry(
        winget_id=package_id,
        name=name,
        version=(raw.get("Version") or "").strip(),
        tags=[t for t in (raw.get("Tags") or []) if t],
        company=_guess_company(package_id),
        description=f"{name} - Windows application.",
        last_update=(raw.get("LastUpdate") or "").strip(),
    )


def build_lookup(raw_entries: list[dict]) -> dict[str, ManifestEntry]:
    """
    Keyed by normalize_key(Name) -- the SAME normalizer the scanner/resolver
    already use for app-name clustering (see resolver.normalize_key), not a
    separate implementation, per the report's "Consistency" goal. On a key
    collision (two different packages whose names normalize the same way,
    e.g. two "7-Zip"-ish entries), the first one encountered wins and the
    rest are skipped -- a duplicate/near-duplicate manifest entry is rare
    enough that "first wins" is an acceptable simplification here.
    """
    lookup: dict[str, ManifestEntry] = {}
    for raw in raw_entries:
        entry = _entry_from_raw(raw)
        if entry is None:
            continue
        key = normalize_key(entry.name)
        if not key or key in lookup:
            continue
        lookup[key] = entry
    return lookup


def search_manifest(lookup: dict[str, ManifestEntry], term: str, max_results: int = 10) -> list[dict]:
    """
    Manual, fuzzy search against the already-loaded LOCAL manifest -- the
    offline counterpart to choco_search.search_chocolatey(), returning
    candidates in the SAME dict shape (name/version/company/description/
    tags/website/choco_id/id/match_percent) so the GUI's search dialog can
    treat a Winget result and a Chocolatey result identically. No network
    call here at all (the manifest is already in memory) -- it's the
    subsequent "Apply" step that may optionally do one winget_show call for
    the single picked candidate (see enrich.apply_manifest_candidate).

    Exact normalize_key() matches (used by the automatic background scrape)
    miss anything where the resolved app name is shorter/different from the
    manifest's own Name (e.g. "VLC" vs "VLC media player") -- this search
    is deliberately substring/fuzzy so the user can still find and pick the
    right entry by hand in exactly that situation.
    """
    term = (term or "").strip()
    if not term or not lookup:
        return []

    from rapidfuzz import fuzz

    term_key = normalize_key(term)
    term_lower = term.lower()
    scored: list[tuple[float, ManifestEntry]] = []
    for entry in lookup.values():
        name_lower = entry.name.lower()
        name_key = normalize_key(entry.name)
        if term_key and term_key == name_key:
            score = 100.0
        elif name_lower.startswith(term_lower):
            # "VLC" -> "VLC media player" ranks above "Jellyfin VLC Bridge"
            # (a mid-string match) by preferring a name that STARTS with
            # the search term, then breaking ties by how much shorter/
            # closer the rest of the name is to the term.
            score = 90.0 + max(0, 8.0 - (len(name_lower) - len(term_lower)) * 0.2)
        elif re.search(rf"\b{re.escape(term_lower)}\b", name_lower):
            # term appears as a whole WORD, just not at the start --
            # "Chrome" -> "Google Chrome", "Office" -> "Microsoft Office".
            # Ranked below startswith but above a raw substring match
            # (which would also catch "Chromium" or "ChromeDriver" for
            # "Chrome" -- those aren't wrong exactly, just less likely to
            # be what the user meant).
            score = 80.0 + max(0, 8.0 - (len(name_lower) - len(term_lower)) * 0.2)
        elif term_lower in name_lower:
            score = 65.0 + max(0, 10.0 - (len(name_lower) - len(term_lower)) * 0.2)
        else:
            # WRatio blends several rapidfuzz strategies and tends to rank
            # genuinely-similar-but-not-substring names (typos, reordered
            # words) more sensibly than a single ratio function alone.
            score = fuzz.WRatio(term_lower, name_lower) * 0.6
        if score >= 40:
            scored.append((score, entry))

    scored.sort(key=lambda t: t[0], reverse=True)
    results = []
    for score, entry in scored[:max_results]:
        results.append({
            "name": entry.name,
            "winget_id": entry.winget_id,
            "choco_id": "",
            "id": entry.winget_id,
            "version": entry.version,
            "description": entry.description,
            "company": entry.company,
            "website": "",
            "tags": entry.tags,
            "match_percent": int(round(score)),
        })
    return results


def fetch_raw_manifest(url: str) -> list[dict]:
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "appcatalog-scraper/1.0"})
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected manifest shape from {url}: expected a JSON array")
    return data


def load_manifest(db_path: str, settings: dict, force_refresh: bool = False) -> ManifestLoadResult:
    """
    Cache-first load: if a cache file exists and is younger than
    scraper_manifest_staleness_hours, use it without touching the network at
    all (force_refresh=True skips this check, for an explicit "Refresh
    manifest now" action). On any network failure, fall back to a stale
    cache if one exists rather than leaving background enrichment entirely
    unable to run just because GitHub was briefly unreachable.
    """
    url = settings.get("scraper_winget_manifest_url", "")
    staleness_seconds = float(settings.get("scraper_manifest_staleness_hours", 4)) * 3600
    cache_path = cache_path_for_db(db_path)

    cache_age = None
    if cache_path.exists():
        cache_age = time.time() - cache_path.stat().st_mtime

    if not force_refresh and cache_age is not None and cache_age < staleness_seconds:
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            lookup = build_lookup(raw)
            return ManifestLoadResult(lookup, "cache", cache_path.stat().st_mtime, len(lookup))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            log.warning("Cached manifest at %s unreadable (%s), re-fetching", cache_path, e)

    if not url:
        return ManifestLoadResult({}, "none", None, 0, "No manifest URL configured")

    try:
        raw = fetch_raw_manifest(url)
        cache_path.write_text(json.dumps(raw), encoding="utf-8")
        lookup = build_lookup(raw)
        log.info("Fetched winget manifest: %d entries from %s", len(lookup), url)
        return ManifestLoadResult(lookup, "network", cache_path.stat().st_mtime, len(lookup))
    except Exception as e:
        log.warning("Manifest fetch from %s failed: %s", url, e)
        if cache_path.exists():
            try:
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                lookup = build_lookup(raw)
                return ManifestLoadResult(
                    lookup, "stale_cache_fallback", cache_path.stat().st_mtime, len(lookup), str(e)
                )
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return ManifestLoadResult({}, "none", None, 0, str(e))


# ======================================================================
# Part 2: winget show CLI fallback -- real per-package metadata
# ======================================================================

# Each entry: (dict key, tuple of label variants winget show has used).
# Matched as "<label>:" at the start of a line (after stripping leading
# whitespace/bullet characters), value is the rest of that line.
_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "publisher": ("Publisher",),
    "description": ("Description",),
    "homepage": ("Homepage", "Publisher Url"),
    "license": ("License",),
    "license_url": ("License Url",),
    "installer_url": ("Installer Url", "Download Url"),
    "version": ("Version",),
    "author": ("Author",),
}


@dataclass
class WingetShowResult:
    winget_id: str
    publisher: str = ""
    description: str = ""
    homepage: str = ""
    license: str = ""
    license_url: str = ""
    installer_url: str = ""
    version: str = ""
    tags: list[str] = field(default_factory=list)
    raw_stdout: str = ""
    error: Optional[str] = None


def is_winget_available() -> bool:
    return shutil.which("winget") is not None


def fetch_winget_show(winget_id: str, timeout: int = 10) -> Optional[WingetShowResult]:
    """
    Returns None (rather than raising) on any failure -- CLI missing,
    timeout, package not found, unparseable output -- since this is always
    used as a best-effort enrichment on top of manifest data that's
    already usable on its own.
    """
    if not winget_id:
        return None
    if not is_winget_available():
        return None

    try:
        proc = subprocess.run(
            ["winget", "show", "--id", winget_id, "--exact",
             "--accept-source-agreements", "--disable-interactivity"],
            capture_output=True, text=True, timeout=timeout,
            # checkpoint 23: winget's own output is UTF-8, but Python's
            # `text=True` with no explicit encoding falls back to the
            # PLATFORM default -- cp1252 on a typical Windows install,
            # not UTF-8 -- so any publisher name, description, or tag
            # containing a curly quote, em-dash, trademark symbol, or any
            # non-English character crashes the decode entirely
            # (UnicodeDecodeError from a background reader thread, which
            # kills the whole scrape run, not just this one lookup).
            # encoding="utf-8" makes this match what winget actually
            # emits; errors="replace" means a genuinely malformed byte
            # degrades to a single "unknown character" glyph instead of
            # crashing the app.
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return WingetShowResult(winget_id, error=f"winget show timed out after {timeout}s")
    except OSError as e:
        return WingetShowResult(winget_id, error=str(e))

    stdout = proc.stdout or ""
    if proc.returncode != 0 or not stdout.strip():
        return WingetShowResult(winget_id, error=(proc.stderr or "winget show returned no output").strip())

    return _parse_winget_show_output(winget_id, stdout)


def _find_label_value(lines: list[str], labels: tuple[str, ...]) -> str:
    """
    Scans for the FIRST label in `labels`, in PRIORITY order (not
    document order) -- e.g. for homepage, ("Homepage", "Publisher Url"),
    a real "Homepage:" line later in the output must still win over an
    earlier "Publisher Url:" line, since they're different things (product
    page vs. the publisher's own site) that just happen to share a
    fallback relationship when one is missing.
    """
    for label in labels:
        prefix = label.lower() + ":"
        for raw_line in lines:
            stripped = raw_line.strip().lstrip("-*• ").strip()
            if stripped.lower().startswith(prefix):
                return stripped[len(label) + 1:].strip()
    return ""


def _parse_winget_show_output(winget_id: str, stdout: str) -> WingetShowResult:
    result = WingetShowResult(winget_id, raw_stdout=stdout)
    lines = stdout.splitlines()

    for key, labels in _FIELD_LABELS.items():
        if key == "description":
            continue  # handled separately below (multi-line block)
        value = _find_label_value(lines, labels)
        if value:
            setattr(result, key, value)

    # Description commonly wraps across several unlabeled indented lines
    # after the "Description:" line itself -- collect until a blank line
    # or the next "Label:" line.
    desc_idx = next(
        (i for i, l in enumerate(lines)
         if l.strip().lstrip("-*• ").strip().lower().startswith("description:")),
        None,
    )
    if desc_idx is not None:
        first = lines[desc_idx].strip().lstrip("-*• ").strip()
        description_lines = [first.split(":", 1)[1].strip()] if ":" in first else []
        for line in lines[desc_idx + 1:]:
            stripped = line.strip().lstrip("-*• ").strip()
            if not stripped or re.match(r"^[A-Za-z][A-Za-z /]*:", stripped):
                break
            description_lines.append(stripped)
        result.description = " ".join(d for d in description_lines if d).strip()

    tags_idx = next(
        (i for i, l in enumerate(lines)
         if l.strip().lstrip("-*• ").strip().lower().startswith("tags:")),
        None,
    )
    if tags_idx is not None:
        first = lines[tags_idx].strip().lstrip("-*• ").strip()
        tags_val = first.split(":", 1)[1].strip() if ":" in first else ""
        collected = [t.strip() for t in re.split(r"[,]", tags_val) if t.strip()]
        for line in lines[tags_idx + 1:]:
            candidate = line.strip().lstrip("-*• ").strip()
            if not candidate or ":" in candidate:
                break
            collected.append(candidate)
        result.tags = collected

    return result


# ======================================================================
# Part 3: enrichment orchestration -- applies manifest/choco/winget-show
# data to the apps table (background batch + manual single-app paths)
# ======================================================================


@dataclass
class ScrapeProgress:
    total: int = 0
    processed: int = 0
    matched: int = 0
    not_found: int = 0
    current_name: str = ""
    status: str = "running"  # running | completed | failed | cancelled


@dataclass
class ScrapeResult:
    total: int
    matched: int
    not_found: int
    manifest_source: str          # "cache" | "network" | "stale_cache_fallback" | "none"
    manifest_error: Optional[str] = None
    status: str = "completed"

def _filter_english_tags(tags: list[str]) -> list[str]:
    """Keep only tags that are ASCII letters, digits, spaces, hyphen, period."""
    if not tags:
        return []
    allowed = re.compile(r'^[A-Za-z0-9\s\-\.]+$')
    return [t for t in tags if allowed.match(t)]


def _apps_to_enrich(db: Database, app_ids: Optional[list[int]], settings: dict) -> list[dict]:
    conn = db.connect()
    if app_ids:
        placeholders = ",".join("?" * len(app_ids))
        rows = conn.execute(f"SELECT * FROM apps WHERE id IN ({placeholders})", app_ids).fetchall()
    else:
        # Background "scrape all" -- restrict to statuses where the name is
        # unlikely to still change (see scraper_auto_enrich_statuses in
        # config.py for why needs_review/ignored are excluded by default).
        statuses = settings.get("scraper_auto_enrich_statuses", ["resolved", "verified"])
        placeholders = ",".join("?" * len(statuses))
        rows = conn.execute(f"SELECT * FROM apps WHERE status IN ({placeholders})", statuses).fetchall()
    return [dict(r) for r in rows]


def _lookup_key_for_app(app_row: dict) -> str:
    """
    Prefer a previously-recorded manifest_name over the app's own current
    name -- if a prior manual match already pinned this app to a specific
    manifest entry, re-scraping should keep hitting that same entry even if
    the app's display name has since been edited, rather than silently
    drifting to a different lookup.
    """
    if app_row.get("manifest_name"):
        return normalize_key(app_row["manifest_name"])
    return app_row.get("normalized_key") or normalize_key(app_row.get("name") or "")


def _build_updates_from_manifest(
    app_row: dict, entry: ManifestEntry, settings: dict,
    show_result: Optional["WingetShowResult"] = None,
) -> dict:
    # `entry.description`/`entry.company` are the generic synthesized
    # placeholders ("<Name> - Windows application.", guessed from the
    # PackageId) since the bare manifest has no real fields for either --
    # `show_result` (a successful winget_show call) has the REAL values and
    # takes priority over both the synthesized placeholder AND whatever was
    # already stored, since it's strictly better data when present.
    description = (show_result.description if show_result and show_result.description else None) \
        or entry.description or app_row.get("description")
    publisher = (show_result.publisher if show_result and show_result.publisher else None) \
        or entry.company or app_row.get("publisher")
    homepage = (show_result.homepage if show_result and show_result.homepage else None) \
        or app_row.get("homepage_url")
    license_ = (show_result.license if show_result and show_result.license else None) \
        or app_row.get("license")

    updates: dict = {
        "winget_id": entry.winget_id,
        "publisher": publisher,
        "description": description,
        "homepage_url": homepage,
        "license": license_,
        "latest_version": (show_result.version if show_result and show_result.version else None)
            or entry.version,
        "scrape_status": "scraped",
        "last_scraped": "__NOW__",  # substituted for datetime('now') by _apply_updates
    }
    if not app_row.get("manifest_name"):
        updates["manifest_name"] = entry.name

    auto_rename = settings.get("scraper_auto_rename", False)
    if (
        auto_rename
        and not app_row.get("name_locked")
        and entry.name
        and entry.name.strip().lower() != (app_row.get("name") or "").strip().lower()
    ):
        updates["alt_source_name"] = app_row.get("name")
        updates["name"] = entry.name
        updates["normalized_key"] = normalize_key(entry.name)

    return updates


def _apply_updates(conn, app_id: int, updates: dict, tags: list[str]):
    updates = dict(updates)
    now_placeholder = updates.pop("last_scraped", None)
    set_parts = [f"{k} = ?" for k in updates]
    values = list(updates.values())
    if now_placeholder is not None:
        set_parts.append("last_scraped = datetime('now')")
    set_parts.append("updated_at = datetime('now')")
    conn.execute(f"UPDATE apps SET {', '.join(set_parts)} WHERE id = ?", (*values, app_id))

    for tag_name in tags:
        if not tag_name:
            continue
        tag_id = _ensure_tag(conn, tag_name)
        conn.execute("INSERT OR IGNORE INTO app_tags (app_id, tag_id) VALUES (?, ?)", (app_id, tag_id))


def run_scrape(
    db: Database,
    app_ids: Optional[list[int]] = None,
    settings: Optional[dict] = None,
    force_manifest_refresh: bool = False,
    on_progress: Optional[Callable[[ScrapeProgress], None]] = None,
    cancel_flag: Optional[Callable[[], bool]] = None,
) -> ScrapeResult:
    """
    Background enrichment:
      - Primary: winutil applications.json (by winget_id)
      - Fallback: svrooij winget manifest (by normalized name)
      - If the description is still the generic placeholder, fetch winget show
        for full metadata (description, publisher, homepage, license, version, tags).
    """
    settings = settings if settings is not None else db.get_all_settings()
    manifest_result = load_manifest(db.path, settings, force_refresh=force_manifest_refresh)
    winutil_lookup = load_winutil_apps(db.path, settings, force_refresh=force_manifest_refresh)

    progress = ScrapeProgress()
    if not manifest_result.lookup and not winutil_lookup:
        progress.status = "failed"
        if on_progress:
            on_progress(progress)
        return ScrapeResult(0, 0, 0, "none", "No data sources available", status="failed")

    apps = _apps_to_enrich(db, app_ids, settings)
    progress.total = len(apps)
    if on_progress:
        on_progress(progress)

    # Global toggle for batch winget show (still respected)
    use_winget_show = settings.get("scraper_winget_show_for_auto_scrape", False)
    show_timeout = int(settings.get("scraper_winget_show_timeout_seconds", 10))
    if use_winget_show and not is_winget_available():
        log.warning("scraper_winget_show_for_auto_scrape is on but winget CLI isn't available – falling back.")
        use_winget_show = False

    conn = db.connect()
    for app_row in apps:
        if cancel_flag and cancel_flag():
            progress.status = "cancelled"
            if on_progress:
                on_progress(progress)
            break

        progress.current_name = app_row.get("name") or ""
        app_id = app_row["id"]

        # 1. Try winutil by winget_id
        winget_id = app_row.get("winget_id")
        winutil_entry = None
        if winget_id:
            winutil_entry = winutil_lookup.get(winget_id.lower())

        updates = {}
        tags = []
        winget_show_attempted = False

        if winutil_entry:
            # Primary: winutil provides description, homepage, choco_id, category
            updates = {
                "description": winutil_entry.get("description") or app_row.get("description"),
                "homepage_url": winutil_entry.get("link") or app_row.get("homepage_url"),
                "choco_id": winutil_entry.get("choco") or app_row.get("choco_id"),
                "winget_id": winutil_entry.get("winget") or app_row.get("winget_id"),
                "scrape_status": "scraped",
                "last_scraped": "__NOW__",
            }
            if winutil_entry.get("category"):
                tags.append(winutil_entry["category"])
            if settings.get("scraper_auto_rename", False) and not app_row.get("name_locked"):
                content = winutil_entry.get("content")
                if content and content.strip().lower() != (app_row.get("name") or "").strip().lower():
                    updates["alt_source_name"] = app_row.get("name")
                    updates["name"] = content
        else:
            # Fallback: svrooij manifest (generic description)
            key = _lookup_key_for_app(app_row)
            entry = manifest_result.lookup.get(key)
            if entry is not None:
                updates = _build_updates_from_manifest(app_row, entry, settings)
                tags = _filter_english_tags(entry.tags)
                winget_id = winget_id or entry.winget_id
            else:
                progress.not_found += 1
                progress.processed += 1
                continue

        # 2. Optionally fetch winget_show if enabled globally (batch mode)
        if use_winget_show and winget_id and is_winget_available():
            show_result = fetch_winget_show(winget_id, timeout=show_timeout)
            winget_show_attempted = True
            if show_result and not show_result.error:
                # Apply all fields from winget show (override any generic placeholder)
                updates["publisher"] = show_result.publisher or updates.get("publisher")
                updates["license"] = show_result.license or updates.get("license")
                if show_result.description:
                    updates["description"] = show_result.description
                if show_result.homepage and not updates.get("homepage_url"):
                    updates["homepage_url"] = show_result.homepage
                if show_result.version and not updates.get("latest_version"):
                    updates["latest_version"] = show_result.version
                if show_result.tags:
                    english_tags = _filter_english_tags(show_result.tags)
                    tags = list(dict.fromkeys(tags + english_tags))

        # 3. FALLBACK: if description is still generic, get full metadata from winget show
        if not winget_show_attempted and winget_id and is_winget_available():
            current_desc = updates.get("description")
            # Generic placeholder check: None, empty, or ends with " - Windows application."
            if not current_desc or current_desc.endswith(" - Windows application."):
                show_result = fetch_winget_show(winget_id, timeout=show_timeout)
                if show_result and not show_result.error:
                    # Replace all fields with real data from winget show
                    updates["publisher"] = show_result.publisher or updates.get("publisher")
                    updates["license"] = show_result.license or updates.get("license")
                    if show_result.description:
                        updates["description"] = show_result.description
                    if show_result.homepage:
                        updates["homepage_url"] = show_result.homepage
                    if show_result.version:
                        updates["latest_version"] = show_result.version
                    if show_result.tags:
                        tags = list(dict.fromkeys(tags + show_result.tags))

        # 4. Apply updates
        try:
            _apply_updates(conn, app_id, updates, tags)
            conn.commit()
            progress.matched += 1
        except Exception:
            log.error("Failed to apply updates to app %s:\n%s", app_id, traceback.format_exc())

        progress.processed += 1
        if on_progress:
            on_progress(progress)

    if progress.status == "running":
        progress.status = "completed"
    if on_progress:
        on_progress(progress)

    return ScrapeResult(
        total=progress.total, matched=progress.matched, not_found=progress.not_found,
        manifest_source=manifest_result.source, manifest_error=manifest_result.error,
        status=progress.status,
    )


def apply_manifest_candidate(db: Database, app_id: int, candidate: dict, choose_name: bool = False,
                              settings: Optional[dict] = None):
    settings = settings if settings is not None else db.get_all_settings()
    conn = db.connect()
    app_row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    if app_row is None:
        raise ValueError(f"No app with id {app_id}")
    app_row = dict(app_row)

    winget_id = candidate.get("winget_id") or candidate.get("id") or ""
    winutil_lookup = load_winutil_apps(db.path, settings)
    winutil_entry = winutil_lookup.get(winget_id.lower()) if winget_id else None

    updates = {}
    tags = []

    if winutil_entry:
        # Use winutil data as primary (better description, homepage, choco)
        updates = {
            "description": winutil_entry.get("description") or app_row.get("description"),
            "homepage_url": winutil_entry.get("link") or app_row.get("homepage_url"),
            "choco_id": winutil_entry.get("choco") or app_row.get("choco_id"),
            "winget_id": winutil_entry.get("winget") or app_row.get("winget_id"),
            "scrape_status": "scraped",
            "last_scraped": "__NOW__",
        }
        if winutil_entry.get("category"):
            tags.append(winutil_entry["category"])
        # Rename using 'content' if requested and not locked
        if choose_name and not app_row.get("name_locked") and winutil_entry.get("content"):
            content = winutil_entry["content"]
            if content.strip().lower() != (app_row.get("name") or "").strip().lower():
                updates["alt_source_name"] = app_row.get("name")
                updates["name"] = content
                updates["normalized_key"] = normalize_key(content)
    else:
        # Fallback: use candidate data from svrooij (or from search result)
        updates = {
            "winget_id": winget_id,
            "description": candidate.get("description") or app_row.get("description"),
            "homepage_url": candidate.get("website") or app_row.get("homepage_url"),
            "scrape_status": "scraped",
            "last_scraped": "__NOW__",
        }
        tags = list(candidate.get("tags") or [])
        if choose_name and not app_row.get("name_locked") and candidate.get("name"):
            updates["alt_source_name"] = app_row.get("name")
            updates["name"] = candidate["name"]
            updates["normalized_key"] = normalize_key(candidate["name"])

    # Always try winget_show for publisher/license (manual mode)
    if winget_id:
        timeout = int(settings.get("scraper_winget_show_timeout_seconds", 10))
        show_result = fetch_winget_show(winget_id, timeout=timeout)
        if show_result and not show_result.error:
            updates["publisher"] = show_result.publisher or updates.get("publisher")
            updates["license"] = show_result.license or updates.get("license")
        if show_result.tags:
            english_tags = _filter_english_tags(show_result.tags)
            tags = list(dict.fromkeys(tags + english_tags))

    _apply_updates(conn, app_id, updates, tags)
    conn.commit()


def apply_choco_candidate(db: Database, app_id: int, candidate: dict, choose_name: bool = False):
    """
    Applies ONE Chocolatey search result (as returned by
    choco_search.search_chocolatey(), a plain dict) to ONE app -- the
    manual, user-picked path. Unlike the manifest path this always
    overwrites description/company/website/version with the chosen
    candidate's values (the user explicitly picked this exact result,
    which is a stronger signal than an automatic name-normalized match),
    and only renames if the caller passes choose_name=True (the simple
    Phase-1 dialog asks the user via a plain confirm, rather than the
    report's full three-way Original/Winget/Choco name-choice dialog,
    which is deferred along with the rest of ManualMatchDialog).
    """
    conn = db.connect()
    app_row = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    if app_row is None:
        raise ValueError(f"No app with id {app_id}")
    app_row = dict(app_row)

    updates: dict = {
        "choco_id": candidate.get("choco_id") or candidate.get("id") or "",
        "publisher": candidate.get("company") or app_row.get("publisher"),
        "description": candidate.get("description") or app_row.get("description"),
        "homepage_url": candidate.get("website") or app_row.get("homepage_url"),
        "latest_version": candidate.get("version") or app_row.get("latest_version"),
        "scrape_status": "scraped",
        "last_scraped": "__NOW__",
    }
    if choose_name and not app_row.get("name_locked") and candidate.get("name"):
        updates["alt_source_name"] = app_row.get("name")
        updates["name"] = candidate["name"]
        updates["normalized_key"] = normalize_key(candidate["name"])

    _apply_updates(conn, app_id, updates, candidate.get("tags") or [])
    conn.commit()