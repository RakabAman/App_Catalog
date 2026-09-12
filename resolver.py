"""
Resolver module: name/version/edition/language extraction, fuzzy
clustering, the resolve orchestrator, and direct user-edit operations
-- consolidated from what were previously resolver/extract.py,
cluster.py, resolve_job.py, edit_ops.py.
"""

from database import Database

# =============================================================
# Field extraction (formerly extract.py)
# =============================================================

"""
Turns messy raw text (folder name / file name / PE metadata) into structured
fields: clean app name, version, edition, architecture, language, and a list
of stripped "attribute" tokens (release-group tags, Keygen/Crack/Portable
flags, website/domain tags, etc.) that are captured rather than silently
discarded.

Naming-source selection is a CASCADE, not a fixed rule: try the FILE name
first, then the immediate FOLDER name, then one level up from the folder.
At each step the candidate text is cleaned (website tags, release tags,
ignore words/patterns, build numbers, version, arch, edition, language all
stripped) and the *result* is checked for validity -- non-empty, not purely
generic/numeric/an ignore-listed word. The first candidate that produces a
valid name wins. Whichever candidate is NOT chosen is kept as
`alt_name_candidate` for transparency, so the GUI can show both and let the
user override the resolver's guess with one click.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VERSION_PATTERNS = [
    # v1.2.3.4 / v1.2.3 / v1.2 / 1.2.3.4  etc. Segment length is 1-6 (not
    # 1-4) because build numbers commonly run to 5 digits ("15.0.02200") --
    # a tighter limit truncates the match right before such a segment,
    # since \b can't land after a 5th digit that a 4-digit cap already
    # excluded.
    re.compile(r"\bv?(\d{1,6}(?:\.\d{1,6}){1,3})\b"),
    # bare "v8" / "v8.0"
    re.compile(r"\bv(\d{1,3}(?:\.\d{1,3})?)\b", re.IGNORECASE),
    # product-line versions like "CS3", "CS6", "2019", "2021"
    re.compile(r"\b(CS\d)\b", re.IGNORECASE),
    re.compile(r"\b(20\d{2})\b"),
]

EDITION_KEYWORDS_DEFAULT = [
    "professional", "pro", "enterprise", "ultimate", "premium", "premiere",
    "home", "standard", "business", "server", "community", "ce",
    "lite", "free", "trial", "academic", "student", "edu",
    "platinum", "advanced", "expert", "elite", "essentials", "plus",
    "suite", "hd",
]

LANGUAGE_KEYWORDS_DEFAULT = {
    "multilanguage": "Multilanguage",
    "multi-language": "Multilanguage",
    "multilingual": "Multilanguage",
    "english": "English",
    "french": "French",
    "german": "German",
    "spanish": "Spanish",
    "italian": "Italian",
    "japanese": "Japanese",
    "chinese": "Chinese",
    "russian": "Russian",
}

NAME_NOISE_WORDS = {
    "setup", "install", "installer", "x86", "x64", "win32", "win64",
    "winall", "final", "retail", "cracked", "full", "version",
}


@dataclass
class ExtractedFields:
    clean_name: str
    version: Optional[str] = None
    version_source: Optional[str] = None  # pe | parsed | none
    edition: Optional[str] = None
    architecture: Optional[str] = None
    language: Optional[str] = None
    attributes: list[str] = field(default_factory=list)  # stripped release-tags/flags
    extraction_confidence: float = 0.5  # 0..1, how much we trust this parse
    name_source: str = "folder"          # 'pe' | 'file' | 'folder' | 'parent_folder'
    alt_name_candidate: Optional[str] = None
    alt_name_source: Optional[str] = None
    is_portable: bool = False


def extract_fields(
    folder_name: str,
    primary_file_name: Optional[str],
    pe_product_name: Optional[str],
    pe_product_version: Optional[str],
    pe_file_version: Optional[str],
    settings: dict,
    parent_folder_name: Optional[str] = None,
    prefer_file_name: bool = False,
    folder_depth: Optional[int] = None,
) -> ExtractedFields:
    """
    Genuinely a CASCADE: every candidate (pe / folder / file / parent_folder)
    is cleaned and validated, and the winner is chosen by SCORING the valid
    candidates rather than taking the first in a fixed order -- a candidate
    that yields a recognized version/edition is worth more than a bare name,
    which is what correctly prefers "xplorer2 Pro v2.4.0.0" (parent folder)
    over a bare "Xplorer2" (file name with no version) once the immediate
    "32 - Bit" leaf folder is disqualified by the ignore-folder patterns.

    One candidate gets disqualified outright regardless of how "valid" its
    text looks: a FOLDER candidate where `folder_depth` is <= 2, meaning the
    folder IS the catalog/subcatalog itself (e.g. an installer sitting
    directly inside "BURNERS" or "CONVERTER" with no dedicated app subfolder
    of its own). That folder name is structurally a category label, not an
    app name -- using it produced the "dvd_fab -> Burners" and
    "Able2Extract -> Converter" bugs. Disqualifying it here (rather than via
    a separate prefer_file_name pre-decision) lets the cascade naturally
    fall through to file/parent_folder instead.
    """
    file_stem = Path(primary_file_name).stem if primary_file_name else ""

    def _norm(s):
        return s.replace("_", " ") if s else s

    folder_name = _norm(folder_name)
    file_stem = _norm(file_stem)
    parent_folder_name = _norm(parent_folder_name)
    pe_product_name = _norm(pe_product_name)

    pe_version = pe_product_version or pe_file_version

    candidates: list[tuple[str, str]] = []
    if pe_product_name:
        candidates.append(("pe", pe_product_name))
    if prefer_file_name:
        if file_stem:
            candidates.append(("file", file_stem))
        if folder_name:
            candidates.append(("folder", folder_name))
    else:
        if folder_name:
            candidates.append(("folder", folder_name))
        if file_stem:
            candidates.append(("file", file_stem))
    if parent_folder_name:
        candidates.append(("parent_folder", parent_folder_name))

    if not candidates:
        candidates = [("folder", folder_name or "")]

    results = []
    for source, text in candidates:
        cleaned = _process_candidate(text, file_stem, pe_version, settings)
        is_shallow_folder = source == "folder" and folder_depth is not None and folder_depth <= 2
        results.append((source, text, cleaned, is_shallow_folder))

    # Score every valid, non-shallow-folder candidate; richer results (a
    # recognized version, an edition, a longer/more descriptive name) score
    # higher, so the cascade doesn't just stop at the first technically-valid
    # candidate in source order.
    source_bonus = {"pe": 8, "file": 3, "folder": 2, "parent_folder": 1}
    scored = []
    for source, text, cleaned, is_shallow_folder in results:
        if is_shallow_folder or not _is_valid_name(cleaned.clean_name, settings):
            continue
        score = source_bonus.get(source, 0)
        if cleaned.version_source in ("pe", "parsed"):
            score += 10
        if cleaned.edition:
            score += 3
        # Length is only a mild tie-breaker (a longer, more descriptive
        # name is *slightly* preferred), not a dominant factor -- it was
        # previously weighted heavily enough that a long, noisy filename
        # with an undigested release/build code stuck in it (e.g.
        # "DAEMONToolsPro4400312-0224") could outscore a short, clean
        # folder name ("Deamon Tools") purely by being longer. Now that
        # build codes/glued digit-runs are cleaned up earlier in the
        # pipeline, length is capped lower and weighted much less here.
        score += min(len(cleaned.clean_name), 20) * 0.05
        scored.append((score, source, text, cleaned))

    if scored:
        scored.sort(key=lambda r: -r[0])
        _score, win_source, _win_text, win_fields = scored[0]

        # A "parent_folder" win means every candidate AT this install unit's
        # own level was either disqualified (shallow folder) or invalid (a
        # generic installer filename like "setup.exe"/"install.exe" -- see
        # ignore_filename_words). parent_folder is deliberately the weakest
        # source (source_bonus=1) precisely because it's one level further
        # UP than the install unit itself -- typically the catalog/
        # subcatalog folder, not an app name at all (e.g. a bare "GRAPHICS"
        # winning as the app name for every 2-level-deep
        # "Catalog/AppName/setup.exe" folder with no separate subcatalog
        # layer, which is a very common real-world layout). If the
        # shallow-disqualified folder candidate at THIS level has a
        # genuinely valid name of its own -- it was only excluded for
        # sitting at catalog/subcatalog depth, not for being empty/noise --
        # that's virtually always a better app name than the bare folder
        # one level up, so prefer it instead. This is a no-op for the
        # original "Burners" case (installer sitting directly IN the
        # catalog folder itself, with no dedicated app subfolder at all),
        # since there the shallow folder's cleaned text is identical to the
        # parent's and no swap happens.
        if win_source == "parent_folder":
            shallow_folder = next(
                (
                    (source, text, cleaned)
                    for source, text, cleaned, is_shallow in results
                    if source == "folder" and is_shallow
                    and _is_valid_name(cleaned.clean_name, settings)
                    and cleaned.clean_name.lower() != win_fields.clean_name.lower()
                ),
                None,
            )
            if shallow_folder is not None:
                win_source, _win_text, win_fields = shallow_folder
    else:
        # Nothing both valid and non-shallow -- better to surface an
        # imperfect name than nothing. Prefer non-shallow candidates with
        # any clean_name at all, then fall back to the shallow folder as an
        # absolute last resort.
        non_shallow_nonempty = [
            (s, t, c) for s, t, c, shallow in results if c.clean_name and not shallow
        ]
        if non_shallow_nonempty:
            win_source, _win_text, win_fields = max(
                non_shallow_nonempty, key=lambda r: len(r[2].clean_name)
            )
        else:
            win_source, _win_text, win_fields, _ = results[0]

    # The winning candidate for the NAME isn't necessarily the best source
    # for the VERSION -- e.g. an install unit at ".../WinGlobe/1.1/isdel.exe"
    # has its version sitting in a leaf folder ("1.1") that's correctly
    # disqualified as a NAME source (it's just a bare version number, not a
    # product name) but still genuinely IS the version. Rather than losing
    # that version entirely because its folder lost the naming cascade,
    # fall back to the first other candidate that found one.
    if not win_fields.version:
        for source, text, cleaned, _shallow in results:
            if source == win_source:
                continue
            if cleaned.version:
                win_fields = _CleanResult(
                    clean_name=win_fields.clean_name,
                    version=cleaned.version,
                    version_source=cleaned.version_source,
                    edition=win_fields.edition or cleaned.edition,
                    architecture=win_fields.architecture or cleaned.architecture,
                    language=win_fields.language or cleaned.language,
                    attributes=win_fields.attributes,
                    is_portable=win_fields.is_portable,
                )
                break

    win_fields = _CleanResult(
        clean_name=_apply_name_synonyms(win_fields.clean_name, settings),
        version=win_fields.version, version_source=win_fields.version_source,
        edition=win_fields.edition, architecture=win_fields.architecture,
        language=win_fields.language, attributes=win_fields.attributes,
        is_portable=win_fields.is_portable,
    )

    alt_source, alt_name = None, None
    for source, text, cleaned, _shallow in results:
        if source == win_source:
            continue
        if cleaned.clean_name and cleaned.clean_name.lower() != win_fields.clean_name.lower():
            alt_source, alt_name = source, cleaned.clean_name
            break

    confidence = 0.5
    if win_source == "pe":
        confidence += 0.25
    elif win_source == "file":
        confidence += 0.15
    elif win_source == "parent_folder":
        confidence -= 0.05
    if win_fields.version_source == "pe":
        confidence += 0.15
    elif win_fields.version_source == "parsed":
        confidence += 0.05
    if len(win_fields.clean_name) >= 3:
        confidence += 0.05
    if not _is_valid_name(win_fields.clean_name, settings):
        confidence -= 0.15
    confidence = max(0.0, min(confidence, 1.0))

    return ExtractedFields(
        clean_name=win_fields.clean_name,
        version=win_fields.version,
        version_source=win_fields.version_source,
        edition=win_fields.edition,
        architecture=win_fields.architecture,
        language=win_fields.language,
        attributes=win_fields.attributes,
        extraction_confidence=round(confidence, 3),
        name_source=win_source,
        alt_name_candidate=alt_name,
        alt_name_source=alt_source,
        is_portable=win_fields.is_portable,
    )


@dataclass
class _CleanResult:
    clean_name: str
    version: Optional[str]
    version_source: str
    edition: Optional[str]
    architecture: Optional[str]
    language: Optional[str]
    attributes: list
    is_portable: bool = False


def _process_candidate(base_text: str, file_stem: str, pe_version: Optional[str], settings: dict) -> "_CleanResult":
    if not base_text:
        return _CleanResult("", None, "none", None, None, None, [], False)

    working, attributes = _strip_noise(base_text, settings, capture_attributes=True)

    # -- portable/paf detection (regex, not token-split, so it correctly
    # matches "paf" even while still dot-attached like "...6.2.paf" before
    # _tidy_name later converts dots to spaces) -- captured explicitly
    # rather than silently discarded via ignore_filename_words, since the
    # user wants it surfaced as "(Portable)" in the name and as a tag.
    is_portable = False
    portable_words = settings.get("portable_indicator_words", ["portable", "paf"])
    if portable_words:
        portable_pattern = r"\b(" + "|".join(re.escape(w) for w in portable_words) + r")\b"
        if re.search(portable_pattern, working, flags=re.IGNORECASE):
            is_portable = True
            if "portable" not in [a.lower() for a in attributes]:
                attributes.append("Portable")
            working = re.sub(portable_pattern, " ", working, flags=re.IGNORECASE)

    build_num = None
    build_pattern = settings.get("build_number_pattern", r"\bbuild\s*#?\s*(\d+)\b")
    if build_pattern:
        m = re.search(build_pattern, working, flags=re.IGNORECASE)
        if m:
            build_num = m.group(1)
            working = re.sub(build_pattern, " ", working, flags=re.IGNORECASE)

    version = None
    version_source = "none"
    prefer_pe = settings.get("prefer_pe_version_over_parsed", True)

    if prefer_pe and pe_version:
        version = _clean_version_string(pe_version)
        version_source = "pe"
    else:
        version = _parse_version_from_text(working) or _parse_version_from_text(file_stem)
        if version:
            version_source = "parsed"
        elif pe_version:
            version = _clean_version_string(pe_version)
            version_source = "pe"

    if version:
        working = _strip_version_mentions(working, version)

    # Redundant short-form major-version number left over from patterns like
    # "FileMaker Pro 13 Advanced 13.0.1.194" -- the bare "13" duplicates the
    # major segment of the full version already captured and would
    # otherwise stay stuck in the name. Only strip it when it actually
    # matches the version's leading segment, so we don't eat an unrelated
    # number that's part of the real name.
    if version:
        major_segment = version.split(".")[0]
        if major_segment.isdigit():
            tokens = working.split()
            tokens = [
                t for t in tokens
                if t.strip(",.-_()[]") != major_segment
            ]
            working = " ".join(tokens)

    if build_num:
        version = f"{version} Build {build_num}" if version else f"Build {build_num}"
        version_source = version_source if version_source != "none" else "parsed"

    # Dots/underscores are deliberately preserved up to this point because
    # the version regex above needs them (e.g. "1.3.1"). But that means
    # anything glued by a dot -- "CubeDesktop.Pro", "FileMaker.Pro.13.Advanced"
    # -- was never split into separate tokens, so the token-based edition/
    # language matching below would silently miss "Pro"/"Advanced" entirely.
    # Converting them to spaces now (version already extracted) plus a
    # second camelCase pass and a second ignore-word pass (words that were
    # dot-glued, like "App.Setup", only become isolatable tokens now) fixes
    # both without breaking version detection.
    working = re.sub(r"[._]+", " ", working)
    working = _split_camel_case(working)
    # A 4-digit year (19xx/20xx) glued directly to a preceding word with NO
    # separator at all -- "Nero2014", "Office2016" -- can't be split by
    # camelCase (no case transition) or by dot-conversion (no dot there).
    # Deliberately narrow: ONLY a recognizable year, not any trailing
    # digits, since short glued numeric suffixes are frequently part of the
    # real brand name itself (Windows7, GTA5, Office365) and splitting
    # those would be wrong.
    working = re.sub(r"([A-Za-z])((?:19|20)\d{2})\b", r"\1 \2", working)
    # A longer digit run (4+ digits -- a release/build code, e.g.
    # "Pro4400312") glued directly to a preceding word with no separator at
    # all also can't be split by camelCase or dot-conversion. Deliberately
    # requires 4+ digits (not any trailing digit) so real glued brand
    # suffixes (Windows7, GTA5, Office365) are left alone -- only inserts a
    # space, doesn't discard anything, so a real word ending in digits is
    # merely split rather than damaged.
    working = re.sub(r"([A-Za-z])(\d{4,})", r"\1 \2", working)
    ignore_words = {w.lower() for w in settings.get("ignore_filename_words", [])}
    if ignore_words:
        tokens = working.split()
        working = " ".join(
            t for t in tokens if t.strip(",.-_()[]+") .lower() not in ignore_words
        )

    architecture = None
    arch_map = settings.get("architecture_keywords", {})
    combined_lower = f"{working} {file_stem}".lower()
    for arch_name, keywords in arch_map.items():
        if any(kw.lower() in combined_lower for kw in keywords):
            architecture = arch_name
            for kw in keywords:
                working = re.sub(re.escape(kw), " ", working, flags=re.IGNORECASE)
            break

    # Strip EVERY edition-keyword token found, not just the first -- a name
    # like "DAEMON Tools Pro Advanced" mentions two edition words, and
    # leaving the second ("Advanced") stuck in the name after only "Pro" is
    # removed prevents it from clustering with plain "DAEMON Tools" /
    # "DAEMON Tools Ultra". The FIRST match found is kept as the `edition`
    # field value (further matches are still removed from the name but not
    # separately recorded -- the app's edition field is single-valued).
    edition = None
    tokens = working.split()
    edition_keywords = {w.lower() for w in settings.get("edition_keywords", EDITION_KEYWORDS_DEFAULT)}
    for idx, tok in enumerate(tokens):
        bare = tok.strip(",.-_()[]+")
        if bare.lower() in edition_keywords:
            if edition is None:
                edition = bare.upper() if bare.lower() == "ce" else bare.capitalize()
            tokens[idx] = ""
    working = " ".join(t for t in tokens if t)

    language = None
    tokens = working.split()
    language_keywords = settings.get("language_keywords", LANGUAGE_KEYWORDS_DEFAULT)
    for idx, tok in enumerate(tokens):
        bare = tok.strip(",.-_()[]+").lower()
        if bare in language_keywords:
            language = language_keywords[bare]
            tokens[idx] = ""
            break
    working = " ".join(t for t in tokens if t)

    # Any bare, standalone numeric token still left over at this point
    # (2-8 digits, no letters attached) is almost always a version/build/
    # year fragment that got separated from the real version by other
    # noise words rather than a real part of the app's name -- e.g. "Nero
    # 2014 Platinum 15.0.02200" leaves a stray "2014" once "Platinum"
    # (edition) is removed, and "DAEMONToolsPro 4400312" (after the digit-
    # run splitter above separates the glued release/build code) leaves a
    # stray 7-digit "4400312". Widened from a 2-4 digit cap to 2-8 so
    # longer scene-release build codes are caught too, not just short
    # year-like numbers. Fold into the version instead of leaving it stuck
    # in the name.
    # Also catches bare numeric "build code" tokens with an internal hyphen
    # and no letters at all (e.g. "4400312-0224", common in scene-release
    # version stamps) -- these are release/build identifiers, not part of
    # the app name, and previously stayed stuck in the name verbatim since
    # they don't match the plain all-digit leftover check above.
    leftover_numbers = []
    tokens = working.split()
    kept = []
    for tok in tokens:
        bare = tok.strip(",.-_()[]+")
        if bare.isdigit() and 2 <= len(bare) <= 8:
            leftover_numbers.append(bare)
        elif re.fullmatch(r"\d{2,8}-\d{2,8}", bare):
            leftover_numbers.append(bare)
        else:
            kept.append(tok)
    if leftover_numbers:
        working = " ".join(kept)
        prefix = " ".join(leftover_numbers)
        version = f"{prefix} {version}" if version else prefix
        if version_source == "none":
            version_source = "parsed"

    if not version and settings.get("allow_bare_trailing_number_as_version", True):
        tokens = working.split()
        if tokens and re.fullmatch(r"\d{1,3}", tokens[-1]):
            version = tokens[-1]
            version_source = "parsed"
            tokens = tokens[:-1]
            working = " ".join(tokens)

    clean_name = _tidy_name(working)
    if not clean_name or len(clean_name) < 2:
        clean_name = _tidy_name(base_text) or base_text.strip()
    clean_name = _smart_case(clean_name)
    if is_portable and clean_name:
        clean_name = f"{clean_name} (Portable)"

    return _CleanResult(clean_name, version, version_source, edition, architecture, language, attributes, is_portable)


def _is_valid_name(name: str, settings: dict) -> bool:
    """
    A name is "invalid" (the cascade should fall through to the next
    candidate) if it's empty, too short, purely numeric, or -- after
    normalizing -- exactly matches an ignore-listed word/pattern. This is
    what makes "Setup", "32", "32-bit", "New folder" etc. correctly fall
    through to the next naming source instead of becoming the app name.

    Minimum length is 3, not 2: a 2-character leftover is almost always
    what's left of a real name after its version/edition/build-code was
    stripped out from around it (e.g. "DTLite4413-0173" -> edition "Lite"
    and build code "4413-0173" removed -> bare "DT", not a real standalone
    product name) rather than a genuine short brand name. Falling through
    to the next candidate (usually a fuller folder name) in that case is
    the better default; true two-letter brand names are rare enough that
    this tradeoff favors correctness on the far more common noisy-filename
    case.
    """
    if not name or len(name.strip()) < 3:
        return False
    bare = name.strip().lower()
    if bare.replace(" ", "").isdigit():
        return False

    ignore_words = {w.lower() for w in settings.get("ignore_folder_names", [])} | \
                   {w.lower() for w in settings.get("ignore_filename_words", [])}
    if bare in ignore_words:
        return False

    patterns = settings.get("ignore_folder_name_patterns", []) + settings.get("ignore_filename_patterns", [])
    for pattern in patterns:
        try:
            if re.fullmatch(pattern, bare, flags=re.IGNORECASE):
                return False
        except re.error:
            continue
    return True


def _strip_noise(text: str, settings: dict, capture_attributes: bool) -> tuple[str, list[str]]:
    attributes: list[str] = []
    working = text

    # Step 0/1: anything wrapped in {}, [], or () is release/uploader/tag
    # noise (site names, uploader handles, "(with SPTD 1.83)", crack/keygen
    # mentions, etc.) -- strip the wrapper AND its contents entirely, before
    # any other cleaning runs, since leftover bracket junk can otherwise
    # confuse later steps.
    for pattern in settings.get("bracket_content_patterns", []):
        if capture_attributes:
            for m in re.finditer(pattern, working):
                tag = m.group(0).strip("{}[]() ")
                if tag and tag.lower() not in [a.lower() for a in attributes]:
                    attributes.append(tag)
        working = re.sub(pattern, " ", working)

    for pattern in settings.get("website_tag_patterns", []):
        if capture_attributes:
            for m in re.finditer(pattern, working, flags=re.IGNORECASE):
                tag = m.group(0).strip("-[] ")
                if tag and tag.lower() not in [a.lower() for a in attributes]:
                    attributes.append(tag)
        working = re.sub(pattern, " ", working, flags=re.IGNORECASE)

    for pattern in settings.get("release_tag_patterns", []):
        if capture_attributes:
            for m in re.finditer(pattern, working, flags=re.IGNORECASE):
                tag = m.group(0).strip("-[] ")
                if tag and tag.lower() not in [a.lower() for a in attributes]:
                    attributes.append(tag)
        working = re.sub(pattern, " ", working, flags=re.IGNORECASE)

    working = _split_camel_case(working)

    # Words glued directly to a following digit with no separator at all --
    # "AirtableSetup1.3.2", "NexusFontSetup2.5.8" -- must NOT be handled by
    # a "\bsetup\d*\b"-style pattern that consumes the leading digit too
    # (that was a real bug: it turned "Setup1.3.2" into removing "Setup1"
    # and leaving ".3.2", which then misparsed as version "3.2" instead of
    # the real "1.3.2"). This strips only the word itself via a lookahead,
    # never touching the digits that follow, so the version regex downstream
    # sees the complete, undamaged number.
    glued_words = settings.get("ignore_filename_words", [])
    if glued_words:
        glued_pattern = r"(?i)\b(" + "|".join(re.escape(w) for w in glued_words) + r")(?=\d)"
        working = re.sub(glued_pattern, " ", working)

    ignore_words = {w.lower() for w in settings.get("ignore_filename_words", [])}
    if ignore_words:
        tokens = working.split()
        kept = []
        for tok in tokens:
            bare = tok.strip(",.-_()[]").lower()
            if bare in ignore_words:
                if capture_attributes and tok not in attributes:
                    attributes.append(tok)
                continue
            kept.append(tok)
        working = " ".join(kept)

    return working, attributes


_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _split_camel_case(text: str) -> str:
    if not text:
        return text
    return _CAMEL_SPLIT_RE.sub(" ", text)


def _apply_name_synonyms(name: str, settings: dict) -> str:
    """
    Last step of name cleaning: rewrite a KNOWN misspelling/rebrand to its
    canonical form (e.g. "Deamon Tools" -> "DAEMON Tools", checkpoint 11's
    unresolved case) via user-maintained regex rules (app_name_synonyms in
    Settings), applied after all other cleaning so it's matching against
    the final tidied name, not raw folder/file noise. Deliberately a
    separate, explicit mechanism from fuzzy clustering -- a typo can land
    on either side of fuzzy_match_threshold depending on exact spelling,
    so this guarantees the fix for a specific KNOWN case rather than
    hoping the fuzzy ratio happens to be high enough.
    """
    if not name:
        return name
    for rule in settings.get("app_name_synonyms", []):
        pattern = rule.get("pattern")
        replacement = rule.get("replacement")
        if not pattern or replacement is None:
            continue
        try:
            new_name = re.sub(pattern, replacement, name)
        except re.error:
            continue
        if new_name != name:
            return new_name
    return name


def _clean_version_string(v: str) -> str:
    return v.strip().rstrip(".")


def _parse_version_from_text(text: str) -> Optional[str]:
    for pattern in VERSION_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


def _strip_version_mentions(text: str, version: str) -> str:
    escaped = re.escape(version)
    text = re.sub(rf"\bv?{escaped}\b", " ", text, flags=re.IGNORECASE)
    return text


def _tidy_name(text: str) -> str:
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"[\[\](){}]", " ", text)
    words = [
        w for w in text.split()
        if w.lower().strip("-,") not in NAME_NOISE_WORDS and w.strip("-, ")
    ]
    cleaned = " ".join(words)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_.")
    return cleaned


def _smart_case(name: str) -> str:
    letters = [c for c in name if c.isalpha()]
    if not letters:
        return name
    if all(c.isupper() for c in letters) or all(c.islower() for c in letters):
        return name.title()
    return name


def normalize_key(name: str) -> str:
    """Key used for fuzzy clustering -- aggressively normalized."""
    key = name.lower()
    key = re.sub(r"[^a-z0-9]+", "", key)
    return key

# =============================================================
# Fuzzy clustering (formerly cluster.py)
# =============================================================

"""
Groups ExtractedFields (one per raw_candidate) into clusters representing a
single canonical app, using fuzzy name matching -- NOT restricted to the same
catalog/subcatalog, since the user's own files can be "mistakenly stored
randomly" and still need to collapse into one app.

Approach: greedy union over a similarity graph, with blocking by the first
two characters of the normalized key to keep comparisons tractable at scale
(two genuinely-the-same app names essentially never differ in their first
two alphanumeric characters -- typos aside, which is an accepted tradeoff
documented here rather than silently assumed).
"""

from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz



@dataclass
class ClusterMember:
    candidate_id: int          # raw_candidates.id
    clean_name: str
    normalized_key: str
    catalog: Optional[str]
    subcatalog: Optional[str]
    version: Optional[str]
    extraction_confidence: float
    alt_name_candidate: Optional[str] = None
    alt_name_source: Optional[str] = None
    is_portable: bool = False


@dataclass
class Cluster:
    members: list[ClusterMember]
    canonical_name: str
    normalized_key: str
    catalog: Optional[str]
    subcatalog: Optional[str]
    cluster_confidence: float  # how tight the fuzzy match was, 0..1
    alt_name_candidate: Optional[str] = None
    alt_name_source: Optional[str] = None
    has_portable_variant: bool = False


def cluster_candidates(members: list[ClusterMember], settings: dict) -> list[Cluster]:
    threshold = settings.get("fuzzy_match_threshold", 88)

    # blocking: group by first 2 chars of normalized key to avoid O(n^2)
    # over the whole dataset
    blocks: dict[str, list[ClusterMember]] = {}
    for m in members:
        block_key = m.normalized_key[:2] if len(m.normalized_key) >= 2 else m.normalized_key
        blocks.setdefault(block_key, []).append(m)

    clusters: list[Cluster] = []

    for block_members in blocks.values():
        # union-find within this block
        parent = list(range(len(block_members)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        best_scores: dict[tuple, float] = {}

        for i in range(len(block_members)):
            for j in range(i + 1, len(block_members)):
                key_i, key_j = block_members[i].normalized_key, block_members[j].normalized_key
                score = fuzz.token_sort_ratio(key_i, key_j)

                # Prefix-match rule: catches "Able2Extract" vs
                # "Able2ExtractPdfConverter", "KingsoftOffice" vs
                # "KingsoftOffice2012Professional..." -- cases where one
                # name is a strict prefix of the other plus descriptive/
                # version suffix, which token_sort_ratio scores poorly on
                # (extra words drag the ratio down) even though these are
                # clearly the same product. Guarded by a minimum length so
                # short generic prefixes ("win", "power") don't over-merge.
                shorter, longer = (key_i, key_j) if len(key_i) <= len(key_j) else (key_j, key_i)
                is_prefix_match = len(shorter) >= 6 and longer.startswith(shorter)

                if score >= threshold or is_prefix_match:
                    union(i, j)
                    best_scores[(find(i), find(j))] = max(
                        best_scores.get((find(i), find(j)), 0), score
                    )

        groups: dict[int, list[ClusterMember]] = {}
        for idx, m in enumerate(block_members):
            root = find(idx)
            groups.setdefault(root, []).append(m)

        for group_members in groups.values():
            clusters.append(_build_cluster(group_members))

    return clusters


def _build_cluster(members: list[ClusterMember]) -> Cluster:
    # canonical name: prefer the member with the highest extraction
    # confidence; ties broken by longest name (usually the most descriptive)
    best = max(members, key=lambda m: (m.extraction_confidence, len(m.clean_name)))

    # catalog/subcatalog: majority vote across members, falling back to the
    # best member's if there's no clear majority (handles the "mistakenly
    # stored in the wrong folder" case without letting one stray file change
    # the group's home catalog)
    catalog = _majority([m.catalog for m in members if m.catalog]) or best.catalog
    subcatalog = _majority([m.subcatalog for m in members if m.subcatalog]) or best.subcatalog

    # cluster confidence: average pairwise similarity as a rough tightness
    # signal, folded together with the average extraction confidence
    if len(members) > 1:
        from rapidfuzz import fuzz as _fuzz
        pair_scores = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pair_scores.append(
                    _fuzz.token_sort_ratio(members[i].normalized_key, members[j].normalized_key) / 100
                )
        tightness = sum(pair_scores) / len(pair_scores)
    else:
        tightness = 1.0

    avg_extract_conf = sum(m.extraction_confidence for m in members) / len(members)
    cluster_confidence = round((tightness * 0.5) + (avg_extract_conf * 0.5), 3)

    return Cluster(
        members=members,
        canonical_name=best.clean_name,
        normalized_key=best.normalized_key,
        catalog=catalog,
        subcatalog=subcatalog,
        cluster_confidence=cluster_confidence,
        alt_name_candidate=best.alt_name_candidate,
        alt_name_source=best.alt_name_source,
        has_portable_variant=any(m.is_portable for m in members),
    )


def _majority(values: list[str]) -> Optional[str]:
    if not values:
        return None
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]

# =============================================================
# Resolve orchestrator (formerly resolve_job.py)
# =============================================================

"""
Resolve step: reads raw_candidates (never mutates them), produces/updates
apps + variants. Designed to be safely re-runnable:
  - Existing apps are matched by normalized_key rather than re-created, so
    running resolve twice on unchanged data doesn't duplicate anything.
  - Any app field the user manually locked is left untouched.
  - Variants are upserted by raw_candidate_id (one variant per raw candidate).
  - Every touched app/variant is stamped with the current settings_version.

This does NOT decide auto vs manual scan triggering -- that's orchestration
the GUI/job layer does. This module is a pure "given current raw_candidates
and current settings, what should apps/variants look like" function, callable
either for the full unattended first pass or for a single app's re-resolve.
"""

import json
import logging
from typing import Optional


log = logging.getLogger("appcatalog.resolver")


class ResolveProgress:
    def __init__(self):
        self.candidates_processed = 0
        self.clusters_formed = 0
        self.apps_created = 0
        self.apps_updated = 0
        self.apps_skipped_locked = 0
        self.variants_written = 0
        self.status = "running"


def run_resolve(db: Database, scan_root_id: Optional[int] = None) -> ResolveProgress:
    """
    Full (re-)resolve pass over all install_unit raw_candidates.
    Safe to call repeatedly -- matches existing apps by normalized_key.
    """
    conn = db.connect()
    settings = db.get_all_settings()
    progress = ResolveProgress()

    query = "SELECT * FROM raw_candidates WHERE unit_type = 'install_unit'"
    params = ()
    if scan_root_id is not None:
        query += " AND scan_root_id = ?"
        params = (scan_root_id,)
    rows = conn.execute(query, params).fetchall()
    log.info("RESOLVE STARTING: %d install-unit candidates to process", len(rows))

    members: list[ClusterMember] = []
    extracted_by_id = {}

    for row in rows:
        leaf_name = _folder_display_name(row["folder_path"])
        parent_name = _parent_folder_display_name(row["folder_path"])

        # extract_fields runs its own file -> folder -> parent-folder
        # cascade internally (see resolver/extract.py), trying each in turn,
        # validating the result against ignore_filename_words/patterns and
        # ignore_folder_names/patterns, AND disqualifying the leaf folder
        # candidate outright when depth<=2 (the folder IS the catalog/
        # subcatalog itself, e.g. an installer sitting directly in
        # "BURNERS"/"CONVERTER") -- so we just hand it the raw leaf/parent
        # folder names plus depth and let it decide.
        fields = extract_fields(
            folder_name=leaf_name,
            primary_file_name=row["primary_file_name"],
            pe_product_name=row["pe_product_name"],
            pe_product_version=row["pe_product_version"],
            pe_file_version=row["pe_file_version"],
            settings=settings,
            parent_folder_name=parent_name,
            folder_depth=row["depth"],
        )
        extracted_by_id[row["id"]] = (row, fields)
        members.append(
            ClusterMember(
                candidate_id=row["id"],
                clean_name=fields.clean_name,
                normalized_key=normalize_key(fields.clean_name),
                catalog=_apply_alias(row["catalog"], settings),
                subcatalog=_apply_alias(row["subcatalog"], settings),
                version=fields.version,
                extraction_confidence=fields.extraction_confidence,
                alt_name_candidate=fields.alt_name_candidate,
                alt_name_source=fields.alt_name_source,
                is_portable=fields.is_portable,
            )
        )
        progress.candidates_processed += 1

    clusters = cluster_candidates(members, settings)
    progress.clusters_formed = len(clusters)
    log.info("Clustering complete: %d candidates -> %d apps", len(members), len(clusters))

    settings_version = db.current_settings_version()
    auto_accept = settings.get("confidence_auto_accept", 0.85)
    needs_review = settings.get("confidence_needs_review", 0.60)

    for cluster in clusters:
        app_id, was_created, locked_fields = _upsert_app(
            conn, cluster, settings_version, auto_accept, needs_review
        )
        if was_created:
            progress.apps_created += 1
        else:
            progress.apps_updated += 1
        if locked_fields:
            progress.apps_skipped_locked += 1

        for member in cluster.members:
            row, fields = extracted_by_id[member.candidate_id]
            _upsert_variant(conn, app_id, row, fields)
            progress.variants_written += 1

        _sync_app_tags(conn, app_id, cluster)

    conn.commit()
    progress.status = "completed"
    log.info("RESOLVE FINISHED: %d apps created, %d updated, %d skipped (locked), %d variants written",
              progress.apps_created, progress.apps_updated, progress.apps_skipped_locked,
              progress.variants_written)
    return progress


def _folder_display_name(folder_path: str) -> str:
    return folder_path.rstrip("/\\").split("/")[-1].split("\\")[-1]


def _parent_folder_display_name(folder_path: str) -> Optional[str]:
    normalized = folder_path.replace("\\", "/").rstrip("/")
    parts = normalized.split("/")
    return parts[-2] if len(parts) >= 2 else None


def _sync_app_tags(conn, app_id: int, cluster):
    """
    Tags are comma-separated, many-to-many, and combine catalog + subcatalog
    + detected type markers (currently just "Portable"; scraped categories
    will add more here later) -- one place the GUI/CSV can read a single
    combined tag list from, per the user's request that catalog/subcatalog/
    future-scraped-categories all be tags on the same app.
    """
    tag_names = []
    if cluster.catalog:
        tag_names.append(cluster.catalog)
    if cluster.subcatalog:
        tag_names.append(cluster.subcatalog)
    if cluster.has_portable_variant:
        tag_names.append("Portable")

    for name in tag_names:
        tag_id = _ensure_tag(conn, name)
        conn.execute(
            "INSERT OR IGNORE INTO app_tags (app_id, tag_id) VALUES (?, ?)",
            (app_id, tag_id),
        )


def _ensure_tag(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO tags (name) VALUES (?)", (name,))
    return cur.lastrowid


def _apply_alias(name: Optional[str], settings: dict) -> Optional[str]:
    """Folder/catalog/subcatalog name -> preferred display conversion,
    e.g. {"burners": "CD/DVD Burner"}. Case-insensitive key match. Falls
    back to the same smart-case title-casing used for app names when no
    explicit alias is defined, so catalog/subcatalog/tags read as "Cd Dvd
    Recorder" instead of shouty "CD DVD RECORDER" -- explicit aliases still
    take priority for cases that need exact wording (e.g. "CD/DVD Burner")."""
    if not name:
        return name
    aliases = settings.get("folder_name_aliases", {})
    alias = aliases.get(name.strip().lower())
    if alias:
        return alias
    return _smart_case(name)


def _upsert_app(
    conn, cluster: Cluster, settings_version: int, auto_accept: float, needs_review: float
) -> tuple[int, bool, list[str]]:
    existing = conn.execute(
        "SELECT * FROM apps WHERE normalized_key = ?", (cluster.normalized_key,)
    ).fetchone()

    confidence = cluster.cluster_confidence
    status = "resolved" if confidence >= needs_review else "needs_review"

    if existing is None:
        cur = conn.execute(
            """INSERT INTO apps (name, catalog, subcatalog, normalized_key, confidence,
                                  status, resolved_with_settings_version, updated_at,
                                  alt_name_candidate, alt_name_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
            (
                cluster.canonical_name, cluster.catalog, cluster.subcatalog,
                cluster.normalized_key, confidence, status, settings_version,
                cluster.alt_name_candidate, cluster.alt_name_source,
            ),
        )
        return cur.lastrowid, True, []

    locked_fields = []
    updates = {}
    if not existing["name_locked"]:
        updates["name"] = cluster.canonical_name
    else:
        locked_fields.append("name")
    if not existing["catalog_locked"]:
        updates["catalog"] = cluster.catalog
    else:
        locked_fields.append("catalog")
    if not existing["subcatalog_locked"]:
        updates["subcatalog"] = cluster.subcatalog
    else:
        locked_fields.append("subcatalog")

    # confidence/status/settings_version always update -- these aren't
    # user-editable fields, they reflect the resolver's own assessment
    updates["confidence"] = confidence
    updates["alt_name_candidate"] = cluster.alt_name_candidate
    updates["alt_name_source"] = cluster.alt_name_source
    # never downgrade a user-verified app back to needs_review automatically
    if existing["status"] != "verified":
        updates["status"] = status
    updates["resolved_with_settings_version"] = settings_version
    updates["updated_at"] = "CURRENT_TIMESTAMP"  # placeholder, replaced below

    set_clause = ", ".join(
        f"{k} = ?" if k != "updated_at" else "updated_at = datetime('now')"
        for k in updates
    )
    values = [v for k, v in updates.items() if k != "updated_at"]
    conn.execute(f"UPDATE apps SET {set_clause} WHERE id = ?", (*values, existing["id"]))

    return existing["id"], False, locked_fields


def _upsert_variant(conn, app_id: int, raw_row, fields):
    existing = conn.execute(
        "SELECT * FROM variants WHERE raw_candidate_id = ?", (raw_row["id"],)
    ).fetchone()

    if existing is None:
        conn.execute(
            """INSERT INTO variants (app_id, raw_candidate_id, version, edition,
                                      architecture, language, source_path, file_type,
                                      file_size, confidence, updated_at,
                                      file_name, alt_name_candidate, name_source)
               VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'), ?,?,?)""",
            (
                app_id, raw_row["id"], fields.version, fields.edition,
                fields.architecture, fields.language, raw_row["folder_path"],
                raw_row["primary_file_type"], raw_row["primary_file_size"],
                fields.extraction_confidence,
                raw_row["primary_file_name"], fields.alt_name_candidate, fields.name_source,
            ),
        )
        return

    updates = {"app_id": app_id, "updated_at": "CURRENT_TIMESTAMP"}
    if not existing["version_locked"]:
        updates["version"] = fields.version
    updates["edition"] = fields.edition
    updates["architecture"] = fields.architecture
    updates["language"] = fields.language
    updates["confidence"] = fields.extraction_confidence
    updates["file_name"] = raw_row["primary_file_name"]
    updates["alt_name_candidate"] = fields.alt_name_candidate
    updates["name_source"] = fields.name_source

    set_clause = ", ".join(
        f"{k} = ?" if k != "updated_at" else "updated_at = datetime('now')"
        for k in updates
    )
    values = [v for k, v in updates.items() if k != "updated_at"]
    conn.execute(f"UPDATE variants SET {set_clause} WHERE id = ?", (*values, existing["id"]))


# ---------------------------------------------------------------------------
# Per-app re-resolve: manual "re-evaluate this one app" action from the GUI.
# Scoped to a single app's existing variants -- re-extracts fields with
# current settings but does NOT re-run corpus-wide clustering (that could
# unpredictably split/merge across other apps from a single-app action).
# Returns a proposal for the GUI to show as a diff before applying.
# ---------------------------------------------------------------------------

def propose_reresolve_app(db: Database, app_id: int) -> dict:
    conn = db.connect()
    settings = db.get_all_settings()

    app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    if app is None:
        raise ValueError(f"No app with id {app_id}")

    variants = conn.execute(
        """SELECT v.*, r.folder_path, r.primary_file_name, r.pe_product_name,
                  r.pe_product_version, r.pe_file_version, r.catalog, r.subcatalog, r.depth
           FROM variants v JOIN raw_candidates r ON v.raw_candidate_id = r.id
           WHERE v.app_id = ?""",
        (app_id,),
    ).fetchall()

    variant_proposals = []
    best_fields = None
    catalogs, subcatalogs = [], []

    for v in variants:
        leaf_name = _folder_display_name(v["folder_path"])
        parent_name = _parent_folder_display_name(v["folder_path"])
        fields = extract_fields(
            folder_name=leaf_name,
            primary_file_name=v["primary_file_name"],
            pe_product_name=v["pe_product_name"],
            pe_product_version=v["pe_product_version"],
            pe_file_version=v["pe_file_version"],
            settings=settings,
            parent_folder_name=parent_name,
            folder_depth=v["depth"],
        )
        variant_proposals.append({
            "variant_id": v["id"],
            "source_path": v["folder_path"],
            "current_version": v["version"],
            "proposed_version": fields.version,
            "current_edition": v["edition"],
            "proposed_edition": fields.edition,
            "current_architecture": v["architecture"],
            "proposed_architecture": fields.architecture,
            "current_language": v["language"],
            "proposed_language": fields.language,
            "version_locked": bool(v["version_locked"]),
        })
        if v["catalog"]:
            catalogs.append(_apply_alias(v["catalog"], settings))
        if v["subcatalog"]:
            subcatalogs.append(_apply_alias(v["subcatalog"], settings))
        if best_fields is None or fields.extraction_confidence > best_fields.extraction_confidence:
            best_fields = fields

    proposed_name = best_fields.clean_name if best_fields else app["name"]
    proposed_catalog = _majority_local(catalogs) or app["catalog"]
    proposed_subcatalog = _majority_local(subcatalogs) or app["subcatalog"]

    return {
        "app_id": app_id,
        "current_name": app["name"],
        "proposed_name": proposed_name,
        "name_locked": bool(app["name_locked"]),
        "current_catalog": app["catalog"],
        "proposed_catalog": proposed_catalog,
        "catalog_locked": bool(app["catalog_locked"]),
        "current_subcatalog": app["subcatalog"],
        "proposed_subcatalog": proposed_subcatalog,
        "subcatalog_locked": bool(app["subcatalog_locked"]),
        "variants": variant_proposals,
    }


def apply_reresolve_app(db: Database, proposal: dict, accept_app_fields: set, accept_variant_ids: set):
    """
    Applies a subset of a propose_reresolve_app() proposal. accept_app_fields
    is a subset of {"name","catalog","subcatalog"}; accept_variant_ids is the
    set of variant_id whose proposed version/edition/architecture/language
    should be applied. Locked fields are refused even if included, unless the
    caller has already cleared the lock (the GUI is expected to surface that
    choice explicitly rather than this function silently overriding it).
    """
    conn = db.connect()
    app_id = proposal["app_id"]

    app_updates = {}
    if "name" in accept_app_fields and not proposal["name_locked"]:
        app_updates["name"] = proposal["proposed_name"]
        app_updates["normalized_key"] = normalize_key(proposal["proposed_name"])
    if "catalog" in accept_app_fields and not proposal["catalog_locked"]:
        app_updates["catalog"] = proposal["proposed_catalog"]
    if "subcatalog" in accept_app_fields and not proposal["subcatalog_locked"]:
        app_updates["subcatalog"] = proposal["proposed_subcatalog"]

    if app_updates:
        set_clause = ", ".join(f"{k} = ?" for k in app_updates) + ", updated_at = datetime('now')"
        conn.execute(f"UPDATE apps SET {set_clause} WHERE id = ?", (*app_updates.values(), app_id))
        conn.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
            ("app", app_id, "re-resolve", json.dumps(app_updates)),
        )

    for vp in proposal["variants"]:
        if vp["variant_id"] not in accept_variant_ids:
            continue
        if vp["version_locked"]:
            continue
        conn.execute(
            """UPDATE variants SET version=?, edition=?, architecture=?, language=?,
                                    updated_at=datetime('now') WHERE id = ?""",
            (vp["proposed_version"], vp["proposed_edition"], vp["proposed_architecture"],
             vp["proposed_language"], vp["variant_id"]),
        )

    conn.commit()


def _majority_local(values: list) -> Optional[str]:
    if not values:
        return None
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]

# =============================================================
# Direct edit operations (formerly edit_ops.py)
# =============================================================

"""
Direct user-editing operations on apps/variants, used by the GUI. Every
mutation here is audit-logged and applies the "editing a field locks it"
rule from the design discussion, so the resolver never silently overwrites
a manual correction on a later re-resolve.
"""

import json
from typing import Optional



def edit_app_field(db: Database, app_id: int, field: str, value: str):
    """Generic editable-field setter for name/catalog/subcatalog/description/
    publisher/homepage_url -- locks the field (for the three resolver-owned
    ones) so re-resolve won't overwrite it."""
    conn = db.connect()
    lockable = {"name", "catalog", "subcatalog"}

    updates = {field: value}
    if field == "name":
        updates["normalized_key"] = normalize_key(value)
    if field in lockable:
        updates[f"{field}_locked"] = 1

    set_clause = ", ".join(f"{k} = ?" for k in updates) + ", updated_at = datetime('now')"
    conn.execute(f"UPDATE apps SET {set_clause} WHERE id = ?", (*updates.values(), app_id))
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
        ("app", app_id, "edit", json.dumps({field: value})),
    )
    conn.commit()


def unlock_app_field(db: Database, app_id: int, field: str):
    conn = db.connect()
    if field not in {"name", "catalog", "subcatalog"}:
        raise ValueError(f"Field {field} is not lockable")
    conn.execute(f"UPDATE apps SET {field}_locked = 0 WHERE id = ?", (app_id,))
    conn.commit()


def set_app_status(db: Database, app_id: int, status: str):
    """status: resolved | needs_review | verified | ignored"""
    conn = db.connect()
    conn.execute(
        "UPDATE apps SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, app_id),
    )
    conn.commit()


def merge_apps(db: Database, source_app_id: int, target_app_id: int):
    """Moves all variants from source_app to target_app, then deletes source_app."""
    if source_app_id == target_app_id:
        return
    conn = db.connect()
    conn.execute(
        "UPDATE variants SET app_id = ?, updated_at = datetime('now') WHERE app_id = ?",
        (target_app_id, source_app_id),
    )
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
        ("app", target_app_id, "merge", json.dumps({"absorbed_app_id": source_app_id})),
    )
    conn.execute("DELETE FROM apps WHERE id = ?", (source_app_id,))
    conn.commit()


def move_variant_to_app(db: Database, variant_id: int, target_app_id: int):
    """Split: move a single mis-clustered variant to a different (existing) app."""
    conn = db.connect()
    conn.execute(
        "UPDATE variants SET app_id = ?, updated_at = datetime('now') WHERE id = ?",
        (target_app_id, variant_id),
    )
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
        ("variant", variant_id, "move", json.dumps({"target_app_id": target_app_id})),
    )
    conn.commit()


def split_variant_to_new_app(db: Database, variant_id: int, new_name: str) -> int:
    """Split: pull a variant out into a brand-new app it doesn't belong under."""
    conn = db.connect()
    variant = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    if variant is None:
        raise ValueError(f"No variant with id {variant_id}")

    old_app = conn.execute("SELECT * FROM apps WHERE id = ?", (variant["app_id"],)).fetchone()

    cur = conn.execute(
        """INSERT INTO apps (name, catalog, subcatalog, normalized_key, confidence,
                              status, name_locked)
           VALUES (?, ?, ?, ?, 1.0, 'verified', 1)""",
        (new_name, old_app["catalog"] if old_app else None,
         old_app["subcatalog"] if old_app else None, normalize_key(new_name)),
    )
    new_app_id = cur.lastrowid

    conn.execute(
        "UPDATE variants SET app_id = ?, updated_at = datetime('now') WHERE id = ?",
        (new_app_id, variant_id),
    )
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, detail_json) VALUES (?,?,?,?)",
        ("variant", variant_id, "split", json.dumps({"new_app_id": new_app_id, "new_name": new_name})),
    )
    conn.commit()
    return new_app_id


def set_variant_ignored(db: Database, variant_id: int, ignored: bool = True):
    conn = db.connect()
    conn.execute(
        "UPDATE variants SET is_ignored = ?, updated_at = datetime('now') WHERE id = ?",
        (int(ignored), variant_id),
    )
    conn.commit()


def delete_variant(db: Database, variant_id: int):
    """Hard-delete a variant (e.g. a false-positive install unit). The
    underlying raw_candidates row is left untouched -- if it's still
    unit_type='install_unit' on the next full resolve, it will simply be
    re-clustered; this only removes the human-facing variant record."""
    conn = db.connect()
    conn.execute("DELETE FROM variants WHERE id = ?", (variant_id,))
    conn.commit()
