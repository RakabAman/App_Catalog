"""
Scanner module: filesystem walking, PE metadata reading, archive
inspection, folder classification, and the scan orchestrator --
consolidated from what were previously scanner/pe_metadata.py,
archive_inspect.py, classify.py, fingerprint.py, walker.py, scan_job.py.
"""

from database import Database

# =============================================================
# PE (.exe) version-resource metadata (formerly pe_metadata.py)
# =============================================================

"""
Extract Windows PE version-resource metadata (ProductName, ProductVersion,
FileVersion, CompanyName, OriginalFilename) from an .exe file.

This is often more reliable than parsing the folder/file name, since it's
embedded by the actual installer build process rather than typed by whoever
renamed the download years ago.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import pefile
except ImportError:  # pragma: no cover
    pefile = None


@dataclass
class PeMetadata:
    product_name: Optional[str] = None
    product_version: Optional[str] = None
    file_version: Optional[str] = None
    company_name: Optional[str] = None
    original_filename: Optional[str] = None

    def is_empty(self) -> bool:
        return not any(
            [self.product_name, self.product_version, self.file_version,
             self.company_name, self.original_filename]
        )


# StringFileInfo keys we care about, in the order pefile exposes them
_WANTED_KEYS = {
    b"ProductName": "product_name",
    b"ProductVersion": "product_version",
    b"FileVersion": "file_version",
    b"CompanyName": "company_name",
    b"OriginalFilename": "original_filename",
}


def read_pe_metadata(exe_path: str | Path) -> Optional[PeMetadata]:
    """
    Returns PeMetadata on success, or None if the file isn't a readable PE
    (e.g. a self-extracting archive with a stripped/corrupt resource section,
    a non-Windows binary, or a truncated download).
    """
    if pefile is None:
        return None

    exe_path = str(exe_path)
    try:
        # fast_load + parse only what we need keeps this cheap even when
        # scanning thousands of exes
        pe = pefile.PE(exe_path, fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
    except Exception:
        return None

    meta = PeMetadata()
    try:
        if hasattr(pe, "FileInfo"):
            for file_info_list in pe.FileInfo:
                for entry in file_info_list:
                    if hasattr(entry, "StringTable"):
                        for st in entry.StringTable:
                            for k, v in st.entries.items():
                                if k in _WANTED_KEYS:
                                    value = v.decode(errors="replace").strip() if isinstance(v, bytes) else str(v).strip()
                                    if value:
                                        setattr(meta, _WANTED_KEYS[k], value)
    except Exception:
        # Malformed resource section -- return whatever we got, if anything
        pass
    finally:
        try:
            pe.close()
        except Exception:
            pass

    return None if meta.is_empty() else meta

# =============================================================
# Archive inspection (formerly archive_inspect.py)
# =============================================================

"""
Archive inspection, tiered per project decision:

  1. ALWAYS: list contents cheaply (no extraction) and try to find a
     confident installer name/version match against the folder name.
  2. ONLY IF AMBIGUOUS: escalate to full extraction into a scratch dir,
     inspect any .exe found via PE metadata, then purge the extracted files
     (keep only the metadata, not the unpacked payload).

"Ambiguous" means: no single dominant installer file, OR the installer
file's name carries no usable version, OR the archive's guessed name
doesn't reasonably match the folder name.

Supported formats:
  .zip  - stdlib zipfile, always available
  .7z   - py7zr, pure python, always available
  .iso  - pycdlib, pure python, always available
  .rar  - rarfile, REQUIRES an external unrar/7z/bsdtar binary on PATH.
          If none is found, we record what we can (file listing is not
          possible without the backend either) and flag it for the user
          rather than failing the whole scan.
"""

import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


try:
    import py7zr
except ImportError:  # pragma: no cover
    py7zr = None

try:
    import rarfile
except ImportError:  # pragma: no cover
    rarfile = None

try:
    import pycdlib
except ImportError:  # pragma: no cover
    pycdlib = None


INSTALLER_EXT = {".exe", ".msi"}


@dataclass
class ArchiveEntry:
    name: str
    size: int


@dataclass
class ArchiveInspectionResult:
    archive_type: str                      # zip | 7z | rar | iso | unsupported
    entries: list[ArchiveEntry] = field(default_factory=list)
    best_installer_name: Optional[str] = None
    ambiguous: bool = True
    ambiguity_reason: Optional[str] = None
    extraction_level: str = "listed_only"  # listed_only | extracted_ambiguous | backend_unavailable
    pe_metadata: Optional[PeMetadata] = None
    error: Optional[str] = None


def _archive_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".zip": "zip", ".7z": "7z", ".rar": "rar", ".iso": "iso",
    }.get(ext, "unsupported")


def _list_zip(path: str) -> list[ArchiveEntry]:
    with zipfile.ZipFile(path) as z:
        return [ArchiveEntry(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]


def _list_7z(path: str) -> list[ArchiveEntry]:
    with py7zr.SevenZipFile(path, mode="r") as z:
        return [ArchiveEntry(n, info.uncompressed if (info := z.list()) else 0)
                for n in z.getnames()] if False else [
            ArchiveEntry(f.filename, f.uncompressed) for f in z.list() if not f.is_directory
        ]


def _list_rar(path: str) -> list[ArchiveEntry]:
    if rarfile is None or not rarfile.is_rarfile(path):
        raise RuntimeError("rar backend unavailable or not a valid rar")
    with rarfile.RarFile(path) as z:
        return [ArchiveEntry(i.filename, i.file_size) for i in z.infolist() if not i.isdir()]


def _list_iso(path: str) -> list[ArchiveEntry]:
    entries = []
    iso = pycdlib.PyCdlib()
    iso.open(path)
    try:
        for dirpath, _dirlist, filelist in iso.walk(iso_path="/"):
            for f in filelist:
                # pycdlib ISO9660 names look like "FILE.EXE;1"
                clean_name = f.split(";")[0]
                full = (dirpath.rstrip("/") + "/" + clean_name).lstrip("/")
                entries.append(ArchiveEntry(full, 0))
    finally:
        iso.close()
    return entries


def list_archive(path: str) -> ArchiveInspectionResult:
    """Cheap, non-extracting listing. This is always attempted first."""
    atype = _archive_type(path)
    result = ArchiveInspectionResult(archive_type=atype)

    if atype == "unsupported":
        result.error = f"Unsupported archive extension: {Path(path).suffix}"
        return result

    try:
        if atype == "zip":
            result.entries = _list_zip(path)
        elif atype == "7z":
            result.entries = _list_7z(path)
        elif atype == "rar":
            result.entries = _list_rar(path)
        elif atype == "iso":
            result.entries = _list_iso(path)
    except Exception as e:
        result.error = str(e)
        if atype == "rar":
            result.extraction_level = "backend_unavailable"
            result.ambiguity_reason = (
                "RAR backend (unrar/7z/bsdtar) not found on PATH -- "
                "install one to enable RAR inspection."
            )
        return result

    _evaluate_ambiguity(result, folder_hint=Path(path).parent.name)
    return result


def _evaluate_ambiguity(result: ArchiveInspectionResult, folder_hint: str):
    installer_entries = [
        e for e in result.entries
        if Path(e.name).suffix.lower() in INSTALLER_EXT
        and "/" not in e.name.strip("/")  # prefer top-level installers over nested payload junk
    ]
    if not installer_entries:
        # fall back to any installer anywhere in the archive
        installer_entries = [e for e in result.entries if Path(e.name).suffix.lower() in INSTALLER_EXT]

    if len(installer_entries) == 1:
        result.best_installer_name = installer_entries[0].name
        # crude confidence check: does the installer filename share tokens
        # with the folder name? If yes, not ambiguous.
        stem = Path(installer_entries[0].name).stem.lower()
        hint = folder_hint.lower()
        shared = any(tok in hint for tok in stem.split() if len(tok) > 2) or any(
            tok in stem for tok in hint.split() if len(tok) > 2
        )
        result.ambiguous = not shared
        result.ambiguity_reason = None if shared else "installer filename doesn't clearly match folder name"
    elif len(installer_entries) > 1:
        result.ambiguous = True
        result.ambiguity_reason = f"{len(installer_entries)} candidate installers found, no single clear match"
        result.best_installer_name = max(installer_entries, key=lambda e: e.size).name
    else:
        result.ambiguous = True
        result.ambiguity_reason = "no .exe/.msi found in top-level listing"


def extract_and_inspect(
    path: str,
    result: ArchiveInspectionResult,
    scratch_dir: Optional[str],
    max_extract_mb: int,
    purge_after: bool = True,
) -> ArchiveInspectionResult:
    """
    Escalation step: only called when list_archive() came back ambiguous.
    Fully extracts to a scratch dir, finds the most likely installer .exe,
    reads its PE metadata, then purges the extraction (metadata is kept,
    the unpacked files are not) unless purge_after=False.
    """
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > max_extract_mb:
        result.ambiguity_reason = (
            f"{result.ambiguity_reason or ''} (skipped extraction: "
            f"{size_mb:.0f}MB exceeds archive_max_full_extract_mb={max_extract_mb})"
        ).strip()
        return result

    tmp_root = tempfile.mkdtemp(prefix="appcatalog_extract_", dir=scratch_dir)
    try:
        if result.archive_type == "zip":
            with zipfile.ZipFile(path) as z:
                z.extractall(tmp_root)
        elif result.archive_type == "7z":
            with py7zr.SevenZipFile(path, mode="r") as z:
                z.extractall(path=tmp_root)
        elif result.archive_type == "rar":
            if rarfile is None:
                result.extraction_level = "backend_unavailable"
                return result
            with rarfile.RarFile(path) as z:
                z.extractall(tmp_root)
        elif result.archive_type == "iso":
            _extract_iso(path, tmp_root)
        else:
            return result

        result.extraction_level = "extracted_ambiguous"

        # find the largest .exe in the extracted tree and read its PE info
        best_exe = None
        best_size = -1
        for root, _dirs, files in os.walk(tmp_root):
            for fname in files:
                if Path(fname).suffix.lower() == ".exe":
                    fpath = os.path.join(root, fname)
                    fsize = os.path.getsize(fpath)
                    if fsize > best_size:
                        best_exe, best_size = fpath, fsize

        if best_exe:
            meta = read_pe_metadata(best_exe)
            if meta:
                result.pe_metadata = meta
                result.best_installer_name = os.path.basename(best_exe)
                result.ambiguous = False
                result.ambiguity_reason = None

    except Exception as e:
        result.error = f"{result.error or ''} extraction_error: {e}".strip()
    finally:
        if purge_after:
            shutil.rmtree(tmp_root, ignore_errors=True)

    return result


def _extract_iso(path: str, dest: str):
    iso = pycdlib.PyCdlib()
    iso.open(path)
    try:
        for dirpath, _dirlist, filelist in iso.walk(iso_path="/"):
            rel_dir = dirpath.lstrip("/")
            out_dir = os.path.join(dest, rel_dir)
            os.makedirs(out_dir, exist_ok=True)
            for f in filelist:
                clean_name = f.split(";")[0]
                iso_full_path = (dirpath.rstrip("/") + "/" + f)
                out_path = os.path.join(out_dir, clean_name)
                with open(out_path, "wb") as out:
                    iso.get_file_from_iso_fp(out, iso_path=iso_full_path)
    finally:
        iso.close()

# =============================================================
# Folder classification (formerly classify.py)
# =============================================================

"""
Classifies a folder as one of:

  install_unit   - contains installer file(s) directly (exe/msi/archive) --
                    this is a leaf we want to resolve into an App.
  container       - holds only subfolders that are themselves install units
                    or further containers (e.g. "ADOBE", "CONVERTERS").
                    Not resolved as an app itself.
  noise           - matches known noise patterns (payloads, redist, _files,
                    tutorial folders, etc.) -- recorded for transparency but
                    excluded from resolution.
  unresolved      - has files but nothing installer-like and doesn't match
                     noise patterns either; flagged for manual review rather
                     than silently dropped.

Uses the live-editable keyword lists from settings, so behavior can be
tuned in the GUI without touching code.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

INSTALLER_EXTS = {".exe", ".msi", ".msix", ".msixbundle"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".iso"}


@dataclass
class Classification:
    unit_type: str      # install_unit | container | noise | unresolved
    reason: str


def classify_folder(
    folder_path: str,
    file_names: list[str],
    subfolder_names: list[str],
    settings: dict,
    depth: int = 99,
) -> Classification:
    folder_name_lower = Path(folder_path).name.lower()

    # Catalog (depth 1) and subcatalog (depth 2) levels are structural,
    # defined by the user's own folder scheme -- they must never be
    # classified as noise/container-by-keyword, even if their name
    # happens to match a noise/container keyword (e.g. a catalog folder
    # literally named "TUTORIALS" must not be treated as junk).
    # Keyword-based noise/container detection only applies from depth 3
    # downward, where we're distinguishing real install units from
    # incidental junk *within* a catalog/subcatalog.
    apply_keyword_rules = depth > 2

    noise_keywords = [k.lower() for k in settings.get("noise_folder_keywords", [])]
    container_keywords = [k.lower() for k in settings.get("container_folder_keywords", [])]
    noise_short_only_keywords = {
        w.lower() for w in settings.get(
            "noise_short_only_keywords",
            ["crack", "cracked", "patch", "patched", "keygen", "keygens",
             "serial", "serials", "update", "updates", "hotfix", "license"],
        )
    }
    # length threshold below which a keyword match counts as "this folder IS
    # basically just <keyword>" (a genuine Crack/Patch/Keygen subfolder).
    # Above it, the keyword is very likely just one release-tag descriptor
    # among a much longer, substantive release name -- e.g.
    # "Adobe.Captivate.v2.0.1177.WinALL.Keygen.Only-ViRiLiTY" mentions
    # "Keygen" but IS the real install folder, not a bare keygen dump.
    # These specific words (crack/patch/keygen/etc) are the ones that
    # legitimately show up as release-tag mentions inside real names, so
    # the length guard applies only to them, not the whole noise list.
    noise_short_only_max_len = settings.get("noise_short_only_max_len", 25)

    def _word_match(keywords: list[str], short_only: set[str] = frozenset(), max_len: int = 999) -> bool:
        # word-boundary match rather than naive substring, so "crack"
        # doesn't false-positive on a real folder like "CrackFree Software"
        import re as _re
        for k in keywords:
            if not _re.search(rf"\b{_re.escape(k)}\b", folder_name_lower):
                continue
            if k in short_only and len(folder_name_lower) > max_len:
                continue  # keyword present, but folder name is too
                          # substantive to be "just" that keyword
            return True
        return False

    if apply_keyword_rules and _word_match(noise_keywords, noise_short_only_keywords, noise_short_only_max_len):
        return Classification("noise", f"folder name matches noise keyword")

    installer_files = [f for f in file_names if Path(f).suffix.lower() in INSTALLER_EXTS]
    archive_files = [f for f in file_names if Path(f).suffix.lower() in ARCHIVE_EXTS]

    if installer_files or archive_files:
        return Classification(
            "install_unit",
            f"contains {len(installer_files)} installer file(s), {len(archive_files)} archive(s)",
        )

    if apply_keyword_rules and _word_match(container_keywords):
        return Classification("container", "folder name matches known container keyword")

    if subfolder_names and not file_names:
        return Classification("container", "no files directly here, only subfolders")

    if file_names:
        # has files, none of them installer/archive -- could be a driver dump,
        # a readme-only folder, loose DLLs, etc. Don't guess: flag it.
        return Classification("unresolved", "has files but none are recognizable installer/archive types")

    return Classification("noise", "empty folder")

# =============================================================
# Folder fingerprinting (formerly fingerprint.py)
# =============================================================

"""
Cheap fingerprint of a folder's contents, used to skip re-processing
unchanged folders on subsequent scans (critical at 800GB scale).

Deliberately NOT hashing file contents (too slow at this scale) -- uses
path + size + mtime of direct children, which is enough to detect adds/
removes/replacements without reading file bytes.
"""

import hashlib
import os


def compute_folder_fingerprint(folder_path: str, entries: list[os.DirEntry]) -> str:
    parts = []
    for e in sorted(entries, key=lambda x: x.name.lower()):
        try:
            stat = e.stat(follow_symlinks=False)
            parts.append(f"{e.name}|{stat.st_size}|{int(stat.st_mtime)}")
        except OSError:
            parts.append(f"{e.name}|ERR")
    blob = "\n".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha1(blob).hexdigest()

# =============================================================
# Filesystem walker (formerly walker.py)
# =============================================================

"""
Walks a scan root and yields one dict per folder that needs a raw_candidates
row. This module does NOT touch the database -- it's a pure generator so it
can be unit-tested against any folder tree, and so the orchestrator can
decide what to do with each item (insert/update/skip-unchanged).
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional


log = logging.getLogger("appcatalog.scanner")


@dataclass
class ScanCandidate:
    folder_path: str
    catalog: Optional[str]
    subcatalog: Optional[str]
    depth: int

    unit_type: str
    unit_type_reason: str

    primary_file_name: Optional[str] = None
    primary_file_type: Optional[str] = None
    primary_file_size: Optional[int] = None
    all_files: list = field(default_factory=list)  # [{name, size, type}]

    pe_product_name: Optional[str] = None
    pe_product_version: Optional[str] = None
    pe_file_version: Optional[str] = None
    pe_company_name: Optional[str] = None
    pe_original_filename: Optional[str] = None
    pe_source: Optional[str] = None

    archive_inspected: bool = False
    archive_extraction_level: Optional[str] = None
    archive_extract_reason: Optional[str] = None

    fingerprint: Optional[str] = None

    # Single App/Variant role (see resolve_scan_root_layout /
    # walk_scan_root): a manual rename on the marked folder, used verbatim
    # as the app/variant name by the resolver, bypassing its normal
    # file/folder/parent-folder naming cascade. None for every ordinary
    # candidate.
    forced_name: Optional[str] = None


def _apply_taxonomy_rules(rel_path_str: str, rules: list) -> Optional[str]:
    """
    First-match-wins regex rules mapping a raw relative path to a clean
    catalog/subcatalog value, e.g. {"pattern": ".*ADOBE.*", "value":
    "Graphics"}. Idea taken from a parallel build of this app (DeepSeek's
    category_rules/subcategory_rules) after a side-by-side review: it's a
    genuinely more flexible mechanism than plain folder-depth-based
    derivation for a messy real-world collection where the same logical
    category is spelled a dozen different ways across different backup
    sources ("GRAPHICS", "Graphic Design", "IMG TOOLS", ...) -- one rule
    set can normalize all of them without renaming anything on disk.
    Matched against the FULL relative path (not just one path segment) so
    a rule can key off any part of the folder structure, case-insensitive.
    Runs BEFORE the plain depth-based fallback; returns None (falls
    through to the old behavior) if no rule matches or the rule list is
    empty, so existing scans/behavior are unaffected until rules are
    actually added.
    """
    for rule in rules or []:
        pattern = rule.get("pattern") if isinstance(rule, dict) else None
        value = rule.get("value") if isinstance(rule, dict) else None
        if not pattern or not value:
            continue
        try:
            if re.search(pattern, rel_path_str, re.IGNORECASE):
                return value
        except re.error:
            log.warning("Invalid taxonomy rule regex, skipping: %r", pattern)
    return None


def _derive_catalog_subcatalog(
    root: str, folder_path: str, settings: Optional[dict] = None
) -> tuple[Optional[str], Optional[str], int]:
    """
    Legacy fixed-2-tier derivation (catalog=part[0], subcatalog=part[1],
    always, regardless of actual nesting depth). Kept only as the
    no-layout-configured fallback path; real scans go through
    resolve_scan_root_layout() below, which generalizes this to an
    arbitrary chain of subcategory tiers, per-folder skip, and per-folder
    rename. See Part 2 of the Feature B design doc.
    """
    rel = os.path.relpath(folder_path, root)
    if rel == ".":
        return None, None, 0
    parts = Path(rel).parts
    rel_str = rel.replace(os.sep, "/")

    settings = settings or {}
    catalog = _apply_taxonomy_rules(rel_str, settings.get("category_rules", []))
    if not catalog:
        catalog = parts[0] if len(parts) >= 1 else None

    subcatalog = _apply_taxonomy_rules(rel_str, settings.get("subcategory_rules", []))
    if not subcatalog:
        subcatalog = parts[1] if len(parts) >= 2 else None

    return catalog, subcatalog, len(parts)


# ---------------------------------------------------------------------
# Feature B / checkpoint 28: per-scan-root folder layout. Every folder in
# a scan root has one of four explicit ROLES -- "catalog" | "subcatalog"
# | "app" | "skip" -- picked from the same dropdown at any depth in
# gui_main.FolderLayoutDialog. resolver.py and classify_folder() are
# unchanged -- this still just feeds them (catalog, subcatalog, depth).
# ---------------------------------------------------------------------

VALID_ROLES = ("catalog", "subcatalog", "app", "single_app", "skip")


def _layout_role_of(entry, default=None):
    """A layout entry is either a bare role string or a dict
    {"role": "...", "name": "Custom Label"}. Returns just the role."""
    if entry is None:
        return default
    if isinstance(entry, dict):
        return entry.get("role", default)
    if entry in VALID_ROLES:
        return entry
    return default


def _layout_label_for(raw_name: Optional[str], key: Optional[str], layout: dict,
                       taxonomy_value: Optional[str] = None) -> Optional[str]:
    """
    Resolves the display label for one folder position: an explicit rename
    in the layout config wins (this is what "revert to raw for this
    folder" and free-text renaming in FolderLayoutDialog write), then a
    matching category_rules/subcategory_rules regex, then the raw folder
    name itself.
    """
    if raw_name is None:
        return None
    entry = layout.get(key) if key else None
    if isinstance(entry, dict) and entry.get("name"):
        return entry["name"]
    if taxonomy_value:
        return taxonomy_value
    return raw_name


def _resolve_role_chain(keys: list[str], layout: dict, unconfigured_toplevel_role: str) -> list[str]:
    """
    Walks the chain TOP-DOWN (unlike the old mode system, roles cascade
    forward rather than needing a deepest-match backward search), one
    role per folder in the chain. A folder with no explicit layout entry
    defaults to: `unconfigured_toplevel_role` if it's the top-level
    folder (no parent in the chain), else cascaded from its parent's
    role -- catalog's children default to subcatalog, subcatalog's
    children default to app, and an app's children stay app (they're
    just internal/version folders at that point, not further category
    tiers). A `skip` role also cascades down (redundant with the
    resolve function's early skip-check, kept here for consistency).
    """
    roles = []
    for i, key in enumerate(keys):
        explicit = _layout_role_of(layout.get(key))
        if explicit is not None:
            role = explicit
        elif i == 0:
            role = unconfigured_toplevel_role
        else:
            parent_role = roles[i - 1]
            # single_app is terminal like app -- its children never get an
            # independent role at all (the whole subtree is swallowed into
            # one candidate by walk_scan_root), so this mapping is really
            # only reachable in the FolderLayoutDialog preview, which calls
            # resolve_scan_root_layout() on individual folders regardless
            # of what's below them.
            role = {"catalog": "subcatalog", "subcatalog": "app"}.get(parent_role, parent_role)
        roles.append(role)
    return roles


def resolve_scan_root_layout(
    root: str,
    folder_path: str,
    layout: dict,
    unconfigured_toplevel_role: str = "catalog",
    settings: Optional[dict] = None,
) -> tuple[Optional[str], Optional[str], int, bool, bool, Optional[str]]:
    """
    Computes (catalog, subcatalog, depth, skip, is_single_app, forced_name)
    for one folder, driven by the user's declared per-folder layout (an
    explicit role -- catalog / subcatalog / app / single_app / skip -- at
    any depth, with cascading defaults where unconfigured; see
    _resolve_role_chain() above).

    Returns (catalog, subcatalog, depth, skip, is_single_app, forced_name).
    When skip is True, catalog/subcatalog/depth are meaningless (None,
    None, 0) -- the caller must not yield a raw_candidates row for this
    folder or descend into it.

    is_single_app is True when THIS folder's own role is "single_app" --
    a whole-subtree "this is one app/variant" marker (see walk_scan_root(),
    which stops normal per-file/per-subfolder classification and instead
    treats everything below this folder as belonging to one install unit).
    forced_name is the manual rename the user typed on that same folder in
    FolderLayoutDialog, if any -- the resolver uses it verbatim as the
    app/variant name instead of running its usual naming cascade. It is
    only ever populated when is_single_app is True; renames on catalog/
    subcatalog/app folders continue to only affect the catalog/subcatalog
    label, not this field.

    catalog = the label of the DEEPEST folder in the chain with role
    "catalog" (normally there's exactly one, at the top; marking a
    deeper folder "catalog" too restarts categorization from there,
    discarding anything shallower -- an intentional escape hatch for an
    unusual nested structure, not the common case).
    subcatalog = the label of the deepest "subcatalog"-role folder that
    sits BELOW the chosen catalog; None if there isn't one.
    depth: resolver.extract_fields() expects "depth <= 2 = category
    label, not an app name" -- boosted to max(raw_depth, 3) whenever the
    folder ITSELF has role "app", so a direct child of a catalog (no
    subcatalog tier at all, e.g. ADOBE/Photoshop) is still trusted as a
    real name; left unchanged otherwise since a folder still mid-chain
    is never treated as an app-name candidate anyway.

    Walkthrough:
      layout={"adobe": "app"}  (ADOBE's children are apps directly)
        ADOBE/Photoshop                        -> catalog=ADOBE, subcatalog=None, depth=3
      layout={}  (nothing configured -- the all-default case)
        GRAPHICS/Converters/Acme                -> catalog=GRAPHICS, subcatalog=Converters, depth=3
      layout={"graphics/converters/video": "app"}   (Video treated as just another app-territory folder, not a tier)
        GRAPHICS/Converters/appname1            -> subcatalog=Converters (default cascade)
        GRAPHICS/Converters/Video/AcmeConvert   -> subcatalog=Converters too (Video adds no tier since it's not marked "subcatalog")
      layout={"graphics/converters/video": "subcatalog"}   (Video IS a further tier)
        GRAPHICS/Converters/Video/AcmeConvert   -> catalog=GRAPHICS, subcatalog=Video, depth=4
      layout={"tutorials": "skip"}
        TUTORIALS/anything -> skip=True, never yielded, walk doesn't descend
    """
    rel = os.path.relpath(folder_path, root)
    if rel == ".":
        return None, None, 0, False

    settings = settings or {}
    layout = layout or {}
    parts = Path(rel).parts
    rel_str = rel.replace(os.sep, "/")

    # cumulative lowercase keys: keys[i] = "a/b/c" for parts[0..i]
    keys = []
    acc = []
    for p in parts:
        acc.append(p.lower())
        keys.append("/".join(acc))

    roles = _resolve_role_chain(keys, layout, unconfigured_toplevel_role)
    if any(r == "skip" for r in roles):
        return None, None, 0, True, False, None

    is_single_app = roles[-1] == "single_app"
    forced_name = None
    if is_single_app:
        this_entry = layout.get(keys[-1])
        if isinstance(this_entry, dict) and this_entry.get("name"):
            forced_name = this_entry["name"]

    taxonomy_catalog = _apply_taxonomy_rules(rel_str, settings.get("category_rules", []))
    taxonomy_subcatalog = _apply_taxonomy_rules(rel_str, settings.get("subcategory_rules", []))

    catalog_idx = max((i for i, r in enumerate(roles) if r == "catalog"), default=None)
    if catalog_idx is None:
        # No catalog anywhere in the chain (e.g. unconfigured_toplevel_role
        # is "skip" and nothing further down was explicitly marked
        # "catalog" either) -- nothing to name this folder under.
        return None, None, 0, False, is_single_app, forced_name
    catalog = _layout_label_for(parts[catalog_idx], keys[catalog_idx], layout, taxonomy_catalog)

    subcatalog_idx = max(
        (i for i in range(catalog_idx + 1, len(roles)) if roles[i] == "subcatalog"),
        default=None,
    )
    subcatalog = (
        _layout_label_for(parts[subcatalog_idx], keys[subcatalog_idx], layout, taxonomy_subcatalog)
        if subcatalog_idx is not None else None
    )

    depth = max(len(parts), 3) if roles[-1] in ("app", "single_app") else len(parts)
    return catalog, subcatalog, depth, False, is_single_app, forced_name


def list_top_level_folders(root: str) -> list[str]:
    """
    Fast, shallow (single os.scandir, no recursion) listing of a scan
    root's immediate subfolders -- used by the pre-scan FolderLayoutDialog
    to know what rows to show without doing a full filesystem walk.
    """
    try:
        with os.scandir(root) as it:
            return sorted((e.name for e in it if e.is_dir(follow_symlinks=False)), key=str.lower)
    except OSError:
        return []


def list_child_folders(root: str, rel_parent: str) -> list[str]:
    """
    Same idea as list_top_level_folders() but for one specific folder
    under the root, addressed by its relative path -- used when the
    FolderLayoutDialog's tree is expanded below the pre-populated first
    two levels (lazy, on-demand resolution, any depth).
    """
    try:
        with os.scandir(os.path.join(root, rel_parent)) as it:
            return sorted((e.name for e in it if e.is_dir(follow_symlinks=False)), key=str.lower)
    except OSError:
        return []


_MULTIPART_PATTERNS = [
    # WinRAR modern: name.part1.rar, name.part02.rar, ... -> group as name.rar
    (re.compile(r"\.part\d+(?=\.\w+$)", re.IGNORECASE), ".rar"),
    # WinRAR old-style: name.rar + name.r00, name.r01, ... -> group as name.rar
    (re.compile(r"\.r\d{2,3}$", re.IGNORECASE), ".rar"),
    # 7z/zip split archives: name.7z.001, name.zip.002, ... -> group as name.7z / name.zip
    (re.compile(r"(\.(?:7z|zip))\.\d{3}$", re.IGNORECASE), None),
]


def _installer_group_key(filename: str) -> str:
    """
    Multi-part archive segments (name.part1.rar/name.part2.rar, or
    name.rar/name.r00/name.r01, or name.7z.001/name.7z.002) must collapse
    into ONE candidate, not one per segment. Everything else -- including
    two genuinely different installers sitting in the same folder, like an
    installer .exe/.rar alongside a separate portable .rar -- must stay
    distinct. This computes a grouping key so segments of the same archive
    share a key while unrelated files don't.
    """
    key = filename
    for pattern, replacement in _MULTIPART_PATTERNS:
        m = pattern.search(key)
        if m:
            if replacement is None:
                key = pattern.sub(m.group(1), key)
            else:
                key = pattern.sub(replacement, key)
            break
    return key.lower()


def _group_installer_files(file_names: list[str]) -> list[list[str]]:
    """
    Groups installer/archive files in a folder by _installer_group_key,
    so multi-part segments of the same archive collapse into one group
    while genuinely distinct installers (e.g. a portable .rar AND a
    separate setup .rar in the same folder) remain separate groups --
    each group becomes its own raw_candidates row / variant downstream.
    """
    installer_like = [
        f for f in file_names
        if Path(f).suffix.lower() in INSTALLER_EXTS or Path(f).suffix.lower() in ARCHIVE_EXTS
    ]
    groups: dict[str, list[str]] = {}
    for f in installer_like:
        groups.setdefault(_installer_group_key(f), []).append(f)
    return list(groups.values())


def _installer_name_rank(path: str, settings: dict) -> int:
    """
    Ranks a filename against the preferred-installer-name hierarchy
    (config.py's preferred_installer_exact_names / _prefixes / _contains):
    tier 1 (exact match) beats tier 2 (prefix) beats tier 3 (contains)
    beats no match at all. This is checked BEFORE the older generic
    setup*/install* prefix bonus in _pick_representative(), and replaces
    it -- a real installer sitting next to a differently-named component
    (e.g. "7z2408-x64.exe" next to "7-Zip Help.pdf") gets no bonus from
    any tier, which is correct: nothing here claims to BE the installer by
    name, so the surrounding pool/depth/part rules alone decide.
    """
    n = Path(path).name.lower()
    exact = {v.lower() for v in settings.get("preferred_installer_exact_names", [])}
    prefixes = [v.lower() for v in settings.get("preferred_installer_prefixes", [])]
    contains = [v.lower() for v in settings.get("preferred_installer_contains", [])]

    if n in exact:
        return 3
    if any(n.startswith(p) for p in prefixes):
        return 2
    if any(c in n for c in contains):
        return 1
    return 0


def _pick_representative(group: list[str], settings: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
    """Within one group (usually a single file, occasionally multi-part
    segments, or -- for a single_app folder -- every installer-like file
    anywhere in the subtree), pick the file that best represents the whole
    group: prefer a real installer over a bare archive, then the
    preferred-installer-name hierarchy (see _installer_name_rank -- exact
    "setup.exe"-style name > starts-with > contains > no match at all),
    then the SHALLOWEST path (fewest subfolders -- a top-level setup.exe
    beats a nested Redist/vcredist.exe or a component installer one folder
    down), then the first part over later parts/segments.

    Entries may be bare filenames (normal per-folder grouping) or
    relative paths with subfolders (single_app's recursive collection,
    e.g. "Setup/setup.exe") -- the depth tiebreak is a no-op for the
    bare-filename case since every entry there sits at depth 0 anyway."""
    settings = settings or {}
    installers = [f for f in group if Path(f).suffix.lower() in INSTALLER_EXTS]
    archives = [f for f in group if Path(f).suffix.lower() in ARCHIVE_EXTS]

    def _score(path: str) -> tuple:
        n = Path(path).name.lower()
        name_rank = _installer_name_rank(path, settings)
        depth = len(Path(path).parts) - 1  # subfolder count; 0 = folder root
        # prefer earlier parts: "part1"/"r00" before "part2"/"r01" etc, and
        # a bare "name.rar" (no part suffix at all) is the best representative
        part_num = 0
        pm = re.search(r"\.part(\d+)", n)
        if pm:
            part_num = int(pm.group(1))
        rm = re.search(r"\.r(\d{2,3})$", n)
        if rm:
            part_num = int(rm.group(1)) + 1  # r00 comes after the base .rar
        return (name_rank, -depth, -part_num)

    pool = installers or archives
    if not pool:
        return None, None
    best = max(pool, key=_score)
    return best, Path(best).suffix.lower().lstrip(".")


def _pick_primary_file(file_names: list[str], settings: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
    """Back-compat single-result helper, kept for anything still calling it
    directly -- prefer a real installer over a bare archive."""
    groups = _group_installer_files(file_names)
    if not groups:
        return None, None
    return _pick_representative(groups[0], settings)


def _enrich_install_candidate(
    base_candidate: "ScanCandidate", dirpath: str, group_files: list[str], settings: dict
) -> "ScanCandidate":
    """
    Clones base_candidate (shared folder-level fields: path/catalog/
    subcatalog/depth/all_files/fingerprint) and fills in per-file fields
    (primary_file_name, size, PE metadata / archive inspection) for ONE
    installer group. Called once per group when a folder contains multiple
    distinct installers.
    """
    import copy
    candidate = copy.copy(base_candidate)

    primary_name, primary_type = _pick_representative(group_files, settings)
    candidate.primary_file_name = primary_name
    candidate.primary_file_type = primary_type

    read_pe = settings.get("read_exe_metadata_enabled", False)
    inspect_archives = settings.get("inspect_archive_contents_enabled", False)

    if not primary_name:
        return candidate

    full_path = os.path.join(dirpath, primary_name)
    try:
        candidate.primary_file_size = os.path.getsize(full_path)
    except OSError as e:
        log.warning("Could not stat %s :: %s", full_path, e)
        candidate.primary_file_size = None

    ext = Path(primary_name).suffix.lower()
    if ext == ".exe" and read_pe:
        meta = read_pe_metadata(full_path)
        if meta:
            candidate.pe_product_name = meta.product_name
            candidate.pe_product_version = meta.product_version
            candidate.pe_file_version = meta.file_version
            candidate.pe_company_name = meta.company_name
            candidate.pe_original_filename = meta.original_filename
            candidate.pe_source = "direct"
    elif ext in {".zip", ".rar", ".7z", ".iso"} and inspect_archives:
        candidate.archive_inspected = True
        try:
            result = list_archive(full_path)
        except Exception as e:
            log.warning("Archive listing failed for %s :: %s", full_path, e)
            result = None

        if result is not None:
            candidate.archive_extraction_level = result.extraction_level

            if (
                result.ambiguous
                and settings.get("archive_ambiguity_escalates_to_extraction", True)
                and result.extraction_level != "backend_unavailable"
                and read_pe  # extraction's whole point is to then read PE metadata
            ):
                log.info("Ambiguous archive, extracting to inspect: %s (%s)",
                          full_path, result.ambiguity_reason)
                try:
                    result = extract_and_inspect(
                        full_path,
                        result,
                        scratch_dir=settings.get("archive_scratch_dir"),
                        max_extract_mb=settings.get("archive_max_full_extract_mb", 2048),
                        purge_after=settings.get("archive_purge_scratch_after_use", True),
                    )
                except Exception as e:
                    log.warning("Archive extraction failed for %s :: %s", full_path, e)
                candidate.archive_extraction_level = result.extraction_level

            candidate.archive_extract_reason = result.ambiguity_reason or result.error
            if result.pe_metadata:
                m = result.pe_metadata
                candidate.pe_product_name = m.product_name
                candidate.pe_product_version = m.product_version
                candidate.pe_file_version = m.file_version
                candidate.pe_company_name = m.company_name
                candidate.pe_original_filename = m.original_filename
                candidate.pe_source = "extracted_from_archive"
            if result.best_installer_name:
                candidate.primary_file_name = result.best_installer_name
        else:
            candidate.archive_extract_reason = "listing failed (see log)"

    return candidate


def _collect_single_app_files(subtree_root: str, error_sink: Optional[list],
                                display_dirpath: str) -> list[dict]:
    """
    Recursively collects every file anywhere under a single_app-role
    folder, as {"relpath": ..., "size": ..., "mtime": ...}. A single_app
    folder means the WHOLE subtree is one app/variant (e.g.
    Photoshop/cs10/ with installers/components scattered across its own
    subfolders) -- so unlike the normal per-folder walk, this needs every
    file below the marked folder in one flat list: to pick the single best
    installer across the whole subtree (see _pick_representative's
    shallow-depth tiebreak) and to build a fingerprint that reflects
    changes anywhere below it, since none of these subfolders are ever
    visited/classified independently by the outer os.walk loop.
    """
    collected = []
    for dirpath, _dirnames, filenames in os.walk(subtree_root):
        for name in filenames:
            full_path = os.path.join(dirpath, name)
            relpath = os.path.relpath(full_path, subtree_root)
            try:
                stat = os.stat(full_path)
                collected.append({"relpath": relpath, "size": stat.st_size, "mtime": stat.st_mtime})
            except OSError as e:
                msg = f"{type(e).__name__}: {e}"
                log.warning("PARTIAL READ ERROR in %s :: %s", full_path, msg)
                if error_sink is not None:
                    error_sink.append((display_dirpath, msg))
    return collected


def _build_single_app_candidate(
    dirpath: str, display_dirpath: str, catalog: Optional[str], subcatalog: Optional[str],
    depth: int, forced_name: Optional[str], settings: dict, error_sink: Optional[list],
) -> ScanCandidate:
    """
    Builds the ONE ScanCandidate for an entire single_app-role subtree.
    Reuses _enrich_install_candidate() unchanged for the actual file-
    picking/PE-reading/archive-inspection work -- it already accepts a
    dirpath + a list of candidate filenames and internally calls
    _pick_representative() over them, so handing it every installer-like
    RELATIVE PATH found anywhere in the subtree (instead of bare filenames
    from one folder) needs no changes there at all.
    """
    files = _collect_single_app_files(dirpath, error_sink, display_dirpath)

    candidate = ScanCandidate(
        folder_path=display_dirpath,
        catalog=catalog,
        subcatalog=subcatalog,
        depth=depth,
        unit_type="install_unit",
        unit_type_reason="folder layout: single app/variant",
        forced_name=forced_name,
    )
    candidate.all_files = [
        {"name": f["relpath"], "size": f["size"], "type": Path(f["relpath"]).suffix.lower().lstrip(".")}
        for f in files
    ]
    fp_blob = "\n".join(
        f"{f['relpath']}|{f['size']}|{int(f['mtime'])}"
        for f in sorted(files, key=lambda x: x["relpath"].lower())
    ).encode("utf-8", errors="replace")
    candidate.fingerprint = hashlib.sha1(fp_blob).hexdigest()

    group_files = [f["relpath"] for f in files]
    candidate = _enrich_install_candidate(candidate, dirpath, group_files, settings)

    if not candidate.primary_file_name:
        # classify_folder() is never consulted for a single_app folder
        # (it's forced to install_unit above), so a genuinely empty/
        # non-installer subtree needs its own fallback here instead of
        # silently posing as a found install unit with no file.
        candidate.unit_type = "unresolved"
        candidate.unit_type_reason = (
            "single app/variant folder: no installer/archive file found anywhere in subtree"
        )
    return candidate


def walk_scan_root(
    root: str,
    settings: dict,
    follow_symlinks: bool = False,
    error_sink: Optional[list] = None,
    folder_layout: Optional[dict] = None,
    unconfigured_toplevel_role: str = "catalog",
) -> Iterator[ScanCandidate]:
    root = os.path.abspath(root)
    walk_root = _apply_long_path_prefix(root)
    log.info("Scan starting at: %s", root)
    if walk_root != root:
        log.info("Windows long-path prefix applied for filesystem calls (paths >260 chars supported).")

    folder_count = 0

    for dirpath, dirnames, filenames in os.walk(walk_root, followlinks=follow_symlinks):
        display_dirpath = _strip_long_path_prefix(dirpath)

        # skip the scan root itself as a candidate (it's not an install unit)
        if os.path.abspath(dirpath) == os.path.abspath(walk_root):
            continue

        folder_count += 1
        if folder_count % 500 == 0:
            log.info("...%d folders walked so far, currently in: %s", folder_count, display_dirpath)

        try:
            with os.scandir(dirpath) as it:
                entries = list(it)
        except OSError as e:
            msg = f"{type(e).__name__}: {e}"
            log.warning("SKIPPED (cannot read folder) %s :: %s", display_dirpath, msg)
            if error_sink is not None:
                error_sink.append((display_dirpath, msg))
            continue

        file_entries = [e for e in entries if e.is_file(follow_symlinks=False)]
        subfolder_names = [e.name for e in entries if e.is_dir(follow_symlinks=False)]
        file_names = [e.name for e in file_entries]

        catalog, subcatalog, depth, skip, is_single_app, forced_name = resolve_scan_root_layout(
            root, display_dirpath, folder_layout or {},
            unconfigured_toplevel_role=unconfigured_toplevel_role,
            settings=settings,
        )
        if skip:
            log.info("SKIPPED (folder layout: skip mode) %s", display_dirpath)
            # Do not descend into a skipped subtree at all -- no candidate
            # row, and os.walk must not visit anything below it either.
            dirnames[:] = []
            continue

        if is_single_app:
            candidate = _build_single_app_candidate(
                dirpath, display_dirpath, catalog, subcatalog, depth,
                forced_name, settings, error_sink,
            )
            log.info("SINGLE APP/VARIANT: [%s/%s] %s  (file: %s)",
                      candidate.catalog, candidate.subcatalog,
                      display_dirpath, candidate.primary_file_name)
            yield candidate
            # The whole subtree is already consumed as one unit -- never
            # walk into it as separate folders/candidates of its own.
            dirnames[:] = []
            continue

        classification = classify_folder(display_dirpath, file_names, subfolder_names, settings, depth=depth)

        candidate = ScanCandidate(
            folder_path=display_dirpath,
            catalog=catalog,
            subcatalog=subcatalog,
            depth=depth,
            unit_type=classification.unit_type,
            unit_type_reason=classification.reason,
        )

        try:
            candidate.all_files = [
                {
                    "name": e.name,
                    "size": (e.stat(follow_symlinks=False).st_size if e.is_file() else 0),
                    "type": Path(e.name).suffix.lower().lstrip("."),
                }
                for e in file_entries
            ]
            candidate.fingerprint = compute_folder_fingerprint(dirpath, entries)
        except OSError as e:
            msg = f"{type(e).__name__}: {e}"
            log.warning("PARTIAL READ ERROR in %s :: %s", display_dirpath, msg)
            if error_sink is not None:
                error_sink.append((display_dirpath, msg))

        if classification.unit_type == "install_unit":
            groups = _group_installer_files(file_names)
            if not groups:
                # classify_folder said install_unit (it saw installer/archive
                # extensions) but grouping found nothing -- shouldn't happen,
                # but yield the bare candidate rather than silently dropping it
                yield candidate
            else:
                # One raw_candidates row PER distinct installer group -- a
                # folder holding both a portable build and a regular
                # installer (two real, different files) must produce two
                # candidates, not one. Multi-part archive segments of the
                # SAME file were already collapsed into one group above.
                for group in groups:
                    group_candidate = _enrich_install_candidate(
                        candidate, dirpath, group, settings
                    )
                    log.info("INSTALL UNIT: [%s/%s] %s  (file: %s)",
                              group_candidate.catalog, group_candidate.subcatalog,
                              display_dirpath, group_candidate.primary_file_name)
                    yield group_candidate
        else:
            yield candidate

        # Prevent os.walk from descending into folders we've already
        # classified as noise (e.g. "_files" web-page-asset dumps) --
        # saves real time at 800GB scale.
        if classification.unit_type == "noise":
            dirnames[:] = []

    log.info("Scan walk finished. %d folders visited.", folder_count)


def _apply_long_path_prefix(path: str) -> str:
    """
    On Windows, prefixing an absolute path with \\\\?\\ tells the Win32 API
    to bypass the traditional 260-character MAX_PATH limit. This matters a
    lot for this dataset: deeply nested folders with long scene-release-style
    names easily exceed 260 characters, and without this, os.scandir/os.stat
    calls on those paths fail with OSError and the folder gets silently
    skipped -- which can make an entire scan come back with zero results
    despite files genuinely being there. No-op on non-Windows.
    """
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):  # UNC path, e.g. \\server\share\...
        return "\\\\?\\UNC\\" + path.lstrip("\\")
    return "\\\\?\\" + path


def _strip_long_path_prefix(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\"):]
    if path.startswith("\\\\?\\"):
        return path[len("\\\\?\\"):]
    return path

# =============================================================
# Scan orchestrator (formerly scan_job.py)
# =============================================================

"""
Orchestrates a full unattended scan of a root folder:
  - registers/updates the scan_roots row
  - walks the tree (scanner.walker)
  - upserts raw_candidates, skipping unchanged folders when incremental
    scanning is enabled (fingerprint match)
  - reports progress via a callback so the GUI can show a live progress bar
    without this module knowing anything about Qt

This module intentionally does NOT call the resolver -- per the agreed
pipeline, resolve is a distinct step that reads raw_candidates back out.
A convenience `run_scan_and_resolve()` wrapper is provided for the "fully
unattended, scan then resolve automatically" flow, but scan and resolve
remain separately callable/re-runnable.
"""

import json
import logging
import time
from typing import Callable, Optional


log = logging.getLogger("appcatalog.scanner.job")


class ScanProgress:
    def __init__(self):
        self.folders_seen = 0
        self.install_units_found = 0
        self.skipped_unchanged = 0
        self.errors_count = 0
        self.started_at = time.time()
        self.current_path = ""
        self.status = "running"  # running | completed | failed | cancelled


def run_scan(
    db: Database,
    root_path: str,
    on_progress: Optional[Callable[[ScanProgress], None]] = None,
    cancel_flag: Optional[Callable[[], bool]] = None,
) -> ScanProgress:
    conn = db.connect()
    settings = db.get_all_settings()
    progress = ScanProgress()
    scan_errors: list = []

    log.info("=" * 70)
    log.info("SCAN STARTING: %s", root_path)
    log.info("=" * 70)

    scan_root_id = _upsert_scan_root(db, root_path, status="running")

    scan_root_row = db.get_scan_root_by_id(scan_root_id)
    folder_layout = db.get_folder_layout(scan_root_id)
    unconfigured_toplevel_role = (
        (scan_root_row["unconfigured_toplevel_role"] or "catalog") if scan_root_row else "catalog"
    )

    incremental = settings.get("incremental_scan_by_default", True)
    existing_fingerprints = {}
    if incremental:
        rows = conn.execute(
            "SELECT folder_path, fingerprint FROM raw_candidates WHERE scan_root_id = ?",
            (scan_root_id,),
        ).fetchall()
        existing_fingerprints = {r["folder_path"]: r["fingerprint"] for r in rows}
        log.info("Incremental scan: %d previously-scanned folders loaded for comparison.",
                  len(existing_fingerprints))

    try:
        for candidate in walk_scan_root(
            root_path, settings,
            follow_symlinks=settings.get("scan_follow_symlinks", False),
            error_sink=scan_errors,
            folder_layout=folder_layout,
            unconfigured_toplevel_role=unconfigured_toplevel_role,
        ):
            if cancel_flag and cancel_flag():
                log.warning("Scan cancelled by user.")
                progress.status = "cancelled"
                break

            progress.folders_seen += 1
            progress.current_path = candidate.folder_path

            if (
                incremental
                and existing_fingerprints.get(candidate.folder_path) == candidate.fingerprint
            ):
                progress.skipped_unchanged += 1
                # still touch last_seen_at so we know it's still present on disk
                conn.execute(
                    "UPDATE raw_candidates SET last_seen_at = datetime('now') "
                    "WHERE scan_root_id = ? AND folder_path = ?",
                    (scan_root_id, candidate.folder_path),
                )
            else:
                _upsert_candidate(conn, scan_root_id, candidate)
                if candidate.unit_type == "install_unit":
                    progress.install_units_found += 1

            if progress.folders_seen % 200 == 0:
                conn.commit()
                log.info("Progress: %d folders seen, %d install units found, %d errors so far",
                          progress.folders_seen, progress.install_units_found, len(scan_errors))
                if on_progress:
                    progress.errors_count = len(scan_errors)
                    on_progress(progress)

        conn.commit()
        if progress.status == "running":
            progress.status = "completed"

    except Exception:
        log.exception("SCAN FAILED with an unhandled exception")
        progress.status = "failed"
        conn.commit()
        _upsert_scan_root(db, root_path, status="failed")
        _write_scan_errors(conn, scan_root_id, scan_errors)
        raise
    else:
        _upsert_scan_root(db, root_path, status=progress.status)
        _write_scan_errors(conn, scan_root_id, scan_errors)

    progress.errors_count = len(scan_errors)

    log.info("=" * 70)
    log.info("SCAN FINISHED: status=%s folders_seen=%d install_units_found=%d "
              "skipped_unchanged=%d errors=%d",
              progress.status, progress.folders_seen, progress.install_units_found,
              progress.skipped_unchanged, progress.errors_count)
    if scan_errors:
        log.warning("First few errors encountered (see scan_errors table for all %d):", len(scan_errors))
        for path, msg in scan_errors[:10]:
            log.warning("  %s :: %s", path, msg)
    log.info("=" * 70)

    if on_progress:
        on_progress(progress)

    return progress


def _write_scan_errors(conn, scan_root_id: int, scan_errors: list):
    if not scan_errors:
        return
    conn.executemany(
        "INSERT INTO scan_errors (scan_root_id, path, error_message) VALUES (?, ?, ?)",
        [(scan_root_id, path, msg) for path, msg in scan_errors],
    )
    conn.commit()


def _upsert_scan_root(db: Database, root_path: str, status: str) -> int:
    conn = db.connect()
    row = conn.execute("SELECT id FROM scan_roots WHERE path = ?", (root_path,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO scan_roots (path, last_scan_started_at, last_scan_status) "
            "VALUES (?, datetime('now'), ?)",
            (root_path, status),
        )
        conn.commit()
        return cur.lastrowid

    if status == "running":
        conn.execute(
            "UPDATE scan_roots SET last_scan_started_at = datetime('now'), "
            "last_scan_status = ? WHERE id = ?",
            (status, row["id"]),
        )
    else:
        conn.execute(
            "UPDATE scan_roots SET last_scan_finished_at = datetime('now'), "
            "last_scan_status = ? WHERE id = ?",
            (status, row["id"]),
        )
    conn.commit()
    return row["id"]


def _upsert_candidate(conn, scan_root_id: int, c: ScanCandidate):
    conn.execute(
        """
        INSERT INTO raw_candidates (
            scan_root_id, folder_path, catalog, subcatalog, depth,
            primary_file_name, primary_file_type, primary_file_size, all_files_json,
            pe_product_name, pe_product_version, pe_file_version, pe_company_name,
            pe_original_filename, pe_source,
            archive_inspected, archive_extraction_level, archive_extract_reason,
            unit_type, unit_type_reason, fingerprint, forced_name,
            first_seen_at, last_seen_at
        ) VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?, ?,?,?, ?,?,?,?, datetime('now'), datetime('now'))
        ON CONFLICT(scan_root_id, folder_path, primary_file_name) DO UPDATE SET
            catalog=excluded.catalog, subcatalog=excluded.subcatalog, depth=excluded.depth,
            primary_file_name=excluded.primary_file_name, primary_file_type=excluded.primary_file_type,
            primary_file_size=excluded.primary_file_size, all_files_json=excluded.all_files_json,
            pe_product_name=excluded.pe_product_name, pe_product_version=excluded.pe_product_version,
            pe_file_version=excluded.pe_file_version, pe_company_name=excluded.pe_company_name,
            pe_original_filename=excluded.pe_original_filename, pe_source=excluded.pe_source,
            archive_inspected=excluded.archive_inspected,
            archive_extraction_level=excluded.archive_extraction_level,
            archive_extract_reason=excluded.archive_extract_reason,
            unit_type=excluded.unit_type, unit_type_reason=excluded.unit_type_reason,
            fingerprint=excluded.fingerprint, forced_name=excluded.forced_name,
            last_seen_at=datetime('now')
        """,
        (
            scan_root_id, c.folder_path, c.catalog, c.subcatalog, c.depth,
            c.primary_file_name, c.primary_file_type, c.primary_file_size,
            json.dumps(c.all_files),
            c.pe_product_name, c.pe_product_version, c.pe_file_version, c.pe_company_name,
            c.pe_original_filename, c.pe_source,
            int(c.archive_inspected), c.archive_extraction_level, c.archive_extract_reason,
            c.unit_type, c.unit_type_reason, c.fingerprint, c.forced_name,
        ),
    )
