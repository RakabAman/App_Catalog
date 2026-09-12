# choco_search.py
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote, urljoin
from typing import List, Dict, Optional, Tuple

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
BASE_URL = "https://community.chocolatey.org"
SEARCH_URL = BASE_URL + "/packages"
PACKAGE_URL = BASE_URL + "/packages/"
REQUEST_TIMEOUT = 30
DEFAULT_RESULTS = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------
def clean_text(value):
    if not value:
        return ""
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()

def normalize_text(value):
    return clean_text(value).lower()

def compact_text(value, max_length=350):
    value = clean_text(value)
    if len(value) <= max_length:
        return value
    return value[:max_length - 3].rstrip() + "..."

# ----------------------------------------------------------------------
# Version extraction
# ----------------------------------------------------------------------
VERSION_PATTERN = re.compile(
    r"\b(\d+\.\d+(?:\.\d+)*(?:[-+][A-Za-z0-9.-]+)?)\s*$"
)

def extract_version_from_text(text):
    if not text:
        return ""
    text = clean_text(text)
    match = VERSION_PATTERN.search(text)
    return match.group(1) if match else ""

def extract_version_from_heading(soup):
    for tag_name in ("h1", "h2", "h3"):
        for tag in soup.find_all(tag_name):
            text = clean_text(tag.get_text(" ", strip=True))
            if text:
                ver = extract_version_from_text(text)
                if ver:
                    return ver
    return ""

def extract_version_from_page_title(soup):
    if not soup.title:
        return ""
    title = clean_text(soup.title.get_text(" ", strip=True))
    return extract_version_from_text(title)

# ----------------------------------------------------------------------
# Section/field extraction helpers
# ----------------------------------------------------------------------
def find_text_node(soup, target):
    target_norm = normalize_text(target)
    for tag in soup.find_all(["h1","h2","h3","h4","h5","h6","strong","b","dt","div","span"]):
        text = clean_text(tag.get_text(" ", strip=True))
        if normalize_text(text) == target_norm:
            return tag
    return None

def find_section_text(soup, heading_text, max_chars=1000):
    target = normalize_text(heading_text)
    for heading in soup.find_all(["h1","h2","h3","h4","h5","h6"]):
        if normalize_text(heading.get_text(" ", strip=True)) != target:
            continue
        collected = []
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) in ("h1","h2","h3","h4","h5","h6"):
                break
            if hasattr(sibling, "get_text"):
                text = clean_text(sibling.get_text(" ", strip=True))
            else:
                text = clean_text(str(sibling))
            if text:
                collected.append(text)
            if len(" ".join(collected)) >= max_chars:
                return " ".join(collected)[:max_chars]
        result = clean_text(" ".join(collected))
        if result:
            return result[:max_chars]
    return ""

def get_next_meaningful_text(element, limit=5):
    values = []
    if not element:
        return values
    current = element
    for _ in range(limit):
        current = current.find_next()
        if current is None:
            break
        text = clean_text(current.get_text(" ", strip=True))
        if text:
            values.append(text)
    return values

def extract_metadata_value(soup, labels):
    if isinstance(labels, str):
        labels = [labels]
    normalized_labels = {normalize_text(label) for label in labels}
    for tag in soup.find_all(["h1","h2","h3","h4","h5","h6","strong","b","dt"]):
        label = normalize_text(tag.get_text(" ", strip=True))
        if label not in normalized_labels:
            continue
        for sibling in tag.next_siblings:
            if getattr(sibling, "name", None) in ("h1","h2","h3","h4","h5","h6"):
                break
            if hasattr(sibling, "get_text"):
                text = clean_text(sibling.get_text(" ", strip=True))
            else:
                text = clean_text(str(sibling))
            if text:
                return text
        nearby = get_next_meaningful_text(tag, limit=4)
        for value in nearby:
            if normalize_text(value) not in normalized_labels:
                return value
    return ""

# ----------------------------------------------------------------------
# Specific parsers (only what we need)
# ----------------------------------------------------------------------
def extract_description(soup):
    desc = find_section_text(soup, "Description", max_chars=1500)
    if desc:
        desc = re.sub(r"^\s*Description\s*", "", desc, flags=re.I)
        return clean_text(desc)
    meta = soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        return compact_text(meta["content"], 1500)
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return compact_text(meta["content"], 1500)
    return ""

def extract_tags(soup):
    target = None
    for tag in soup.find_all(["h1","h2","h3","h4","h5","h6","strong","b","dt"]):
        if normalize_text(tag.get_text(" ", strip=True)) == "tags:":
            target = tag
            break
    if not target:
        for tag in soup.find_all(["h1","h2","h3","h4","h5","h6"]):
            if normalize_text(tag.get_text(" ", strip=True)) == "tags":
                target = tag
                break
    if not target:
        return []
    tags = []
    for link in target.find_all_next("a", limit=30):
        text = clean_text(link.get_text(" ", strip=True))
        if not text:
            continue
        href = link.get("href", "")
        if "#" in text or "tag" in href.lower():
            text = text.lstrip("#").strip()
            if text and text not in tags:
                tags.append(text)
        parent_text = clean_text(link.parent.get_text(" ", strip=True)) if link.parent else ""
        if normalize_text(parent_text) in ("software specific:", "package specific:"):
            break
    if not tags:
        parent = target.parent
        if parent:
            for link in parent.find_all("a"):
                text = clean_text(link.get_text(" ", strip=True)).lstrip("#").strip()
                if text and len(text) <= 80 and text.lower() not in (
                    "software site","software source","software license",
                    "software docs","software mailing list","software issues",
                    "package source","download"
                ) and text not in tags:
                    tags.append(text)
    return tags[:50]

def extract_author(soup):
    return extract_metadata_value(soup, ["Software Author(s):", "Software Author(s)"])

def extract_special_links(soup):
    # We only want the "Software Site" (project URL)
    for link in soup.find_all("a"):
        text = normalize_text(link.get_text(" ", strip=True))
        if text == "software site":
            href = link.get("href")
            if href:
                return urljoin(BASE_URL, href)
    return ""

def detect_deprecated(soup, display_name=""):
    text = normalize_text(soup.get_text(" ", strip=True))
    name = normalize_text(display_name)
    return ("[deprecated]" in name or "deprecated" in name or "[deprecated]" in text)

# ----------------------------------------------------------------------
# Package page parser – returns only the fields we need
# ----------------------------------------------------------------------
def parse_package_page(url: str, fallback_id: str = "") -> Optional[Dict]:
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None
        html = response.text
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Package ID
    package_id = fallback_id
    if not package_id:
        match = re.search(r"/packages/([^/?#]+)", url)
        if match:
            package_id = match.group(1)
    package_id = clean_text(package_id)

    # Display name
    display_name = package_id
    for tag_name in ("h1", "h2", "h3"):
        for tag in soup.find_all(tag_name):
            text = clean_text(tag.get_text(" ", strip=True))
            if text and extract_version_from_text(text):
                display_name = text
                break
        if display_name != package_id:
            break
    if display_name == package_id and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))
        title = re.sub(r"^Chocolatey Software\s*\|\s*", "", title, flags=re.I)
        if title:
            display_name = title

    # Version
    version = extract_version_from_heading(soup)
    if not version:
        version = extract_version_from_page_title(soup)
    if not version:
        page_text = soup.get_text("\n", strip=True)
        match = re.search(r"\b(\d+\.\d+(?:\.\d+)*(?:[-+][A-Za-z0-9.-]+)?)\s*\|\s*Updated:", page_text, re.I)
        if match:
            version = match.group(1)

    # Description (kept for compatibility with Manual Match dialog)
    description = extract_description(soup)

    # Author → company
    company = extract_author(soup)

    # Project URL → website
    website = extract_special_links(soup)

    # Tags (English only)
    tags = extract_tags(soup)

    # Deprecated flag (used for scoring)
    deprecated = detect_deprecated(soup, display_name)

    return {
        "name": display_name,
        "choco_id": package_id,          # for the dialog
        "id": package_id,                # also keep the raw id
        "version": version,
        "description": description,
        "company": company,
        "website": website,
        "tags": tags,
        "deprecated": deprecated,
        "package_url": url,
    }

# ----------------------------------------------------------------------
# Search & scoring
# ----------------------------------------------------------------------
def get_exact_package_ids(search_term: str) -> List[str]:
    value = search_term.strip().lower()
    candidates = [value]
    normalized = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if normalized and normalized not in candidates:
        candidates.append(normalized)
    compact = re.sub(r"[^a-z0-9]", "", value)
    if compact and compact not in candidates:
        candidates.append(compact)
    return candidates

def search_exact_package(package_id: str) -> Optional[Dict]:
    url = PACKAGE_URL + quote(package_id)
    pkg = parse_package_page(url, fallback_id=package_id)
    if pkg:
        pkg["exact_lookup"] = True
    return pkg

def extract_package_links(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href.startswith("/packages/"):
            continue
        path = href[len("/packages/"):].strip("/")
        if not path:
            continue
        parts = path.split("/")
        package_id = parts[0]
        if not package_id or " " in package_id:
            continue
        package_id = package_id.split("?")[0].split("#")[0]
        if not package_id:
            continue
        url = urljoin(BASE_URL, "/packages/" + package_id)
        if url not in results:
            results.append(url)
    return results

def search_website(search_term: str) -> List[str]:
    encoded = quote(search_term)
    url = f"{SEARCH_URL}?q={encoded}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []
        if "just a moment" in resp.text.lower() or "cf-chl-" in resp.text.lower():
            return []
        return extract_package_links(resp.text)
    except Exception:
        return []

def calculate_score(pkg: Dict, search_term: str) -> Tuple[int, List[str]]:
    query = normalize_text(search_term)
    pkg_id = normalize_text(pkg.get("id", ""))
    name = normalize_text(pkg.get("name", ""))
    name_without_version = re.sub(VERSION_PATTERN, "", name).strip()
    tags = [normalize_text(t) for t in pkg.get("tags", [])]
    desc = normalize_text(pkg.get("description", ""))

    score = 0
    reasons = []

    if pkg_id == query:
        score += 1000
        reasons.append("exact ID")
    if name_without_version == query:
        score += 900
        reasons.append("exact name")
    if pkg_id.startswith(query) and pkg_id != query:
        score += 700
        reasons.append("ID starts with search")
    if name_without_version.startswith(query) and name_without_version != query:
        score += 650
        reasons.append("name starts with search")
    if query in pkg_id and pkg_id != query:
        score += 500
        reasons.append("ID contains search")
    if query in name_without_version and name_without_version != query:
        score += 450
        reasons.append("name contains search")
    if query in tags:
        score += 350
        reasons.append("tag exact")
    elif any(query in tag for tag in tags):
        score += 250
        reasons.append("tag contains search")
    if query in desc:
        score += 100
        reasons.append("description contains search")

    # penalties
    if pkg_id.endswith(".install"):
        score -= 25
    if pkg_id.endswith(".portable"):
        score -= 25
    if pkg_id.endswith(".tools"):
        score -= 25
    if pkg_id.endswith("-nightly") or pkg_id.endswith(".nightly"):
        score -= 40
    if pkg.get("deprecated"):
        score -= 500

    return score, reasons

def get_match_percent(pkg: Dict, search_term: str) -> int:
    query = normalize_text(search_term)
    pkg_id = normalize_text(pkg.get("id", ""))
    name = normalize_text(pkg.get("name", ""))
    name_without_version = re.sub(VERSION_PATTERN, "", name).strip()
    if pkg_id == query:
        return 100
    if name_without_version == query:
        return 98
    if pkg_id.startswith(query):
        return 90
    if name_without_version.startswith(query):
        return 88
    if query in pkg_id:
        return 80
    if query in name_without_version:
        return 78
    tags = [normalize_text(t) for t in pkg.get("tags", [])]
    if query in tags:
        return 70
    if any(query in tag for tag in tags):
        return 60
    if query in normalize_text(pkg.get("description", "")):
        return 40
    return 20

def merge_packages(packages: List[Dict]) -> List[Dict]:
    merged = {}
    for pkg in packages:
        pkg_id = normalize_text(pkg.get("id", ""))
        if not pkg_id:
            continue
        if pkg_id not in merged:
            merged[pkg_id] = pkg
        else:
            existing = merged[pkg_id]
            if pkg.get("exact_lookup"):
                merged[pkg_id] = pkg
            else:
                for key, value in pkg.items():
                    if not existing.get(key) and value:
                        existing[key] = value
    return list(merged.values())

# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def search_chocolatey(search_term: str, max_results: int = DEFAULT_RESULTS) -> List[Dict]:
    """
    Search Chocolatey and return a list of dicts with fields:
        name, version, company, website, choco_id, description, tags
    (description is kept for compatibility with the Manual Match dialog).
    """
    search_term = search_term.strip()
    if not search_term:
        return []

    packages = []

    # 1. Exact lookup
    exact_ids = get_exact_package_ids(search_term)
    for pkg_id in exact_ids:
        pkg = search_exact_package(pkg_id)
        if pkg:
            packages.append(pkg)
            if normalize_text(pkg.get("id", "")) == normalize_text(search_term):
                break

    # 2. Website search
    links = search_website(search_term)
    max_fetch = max(max_results * 2, 30)
    for url in links[:max_fetch]:
        match = re.search(r"/packages/([^/?#]+)", url)
        if not match:
            continue
        pkg_id = match.group(1)
        if any(normalize_text(p.get("id", "")) == normalize_text(pkg_id) for p in packages):
            continue
        pkg = parse_package_page(url, fallback_id=pkg_id)
        if pkg:
            packages.append(pkg)

    # Merge duplicates
    packages = merge_packages(packages)

    # Score and sort
    scored = []
    for pkg in packages:
        score, reasons = calculate_score(pkg, search_term)
        pkg["score"] = score
        pkg["match_percent"] = get_match_percent(pkg, search_term)
        pkg["match_reason"] = reasons
        scored.append(pkg)

    scored.sort(key=lambda p: (p.get("deprecated", False), -p.get("score", 0), -p.get("downloads", 0) if p.get("downloads") else 0))
    return scored[:max_results]