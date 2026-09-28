"""
Update ring resolution against the GitHub Releases feed.

Ring resolution against the GitHub Releases feed. Platforms that cannot
auto-install (Linux — the installer is a GUI wizard; macOS — no release asset
is published) still deserve to know a new version exists, so the check is
always possible and only the INSTALL step is capability-gated.

Ring semantics:
  stable  — newest release that is NOT a prerelease
  preview — newest release of any kind

Everything tag/URL/ring related is pure and fed plain data structures, so it
is testable without a network.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Callable, Dict, List, Optional

REPO_SLUG = "manconsultingltd/pos-api-printer-service"
RELEASES_API = f"https://api.github.com/repos/{REPO_SLUG}/releases"

# Asset name → the release page is the only URL that ever leaves the box, so
# pin what a release page looks like and drop anything that does not match.
RELEASE_URL_HOST = "github.com"
_RELEASE_URL_PREFIX = f"/{REPO_SLUG}/releases/"

# Strict: `v` + three numeric components, nothing else. Anything looser lets
# a stray tag in the repo masquerade as a shipped build.
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# Paging bound. Generous but finite so a pathological repo cannot make the
# check run forever.
RELEASES_PER_PAGE = 30
MAX_PAGES = 3

# Platform → asset name published by release.yml
PLATFORM_ASSETS = {
    "windows": "api-printer-setup-windows.exe",
    "linux": "api-printer-installer-linux",
}


def version_from_tag(tag: Any) -> Optional[str]:
    """Extract 1.1.50 from v1.1.50; None for any other tag shape."""
    if not isinstance(tag, str):
        return None
    m = _TAG_RE.match(tag)
    return tag[1:] if m else None


def is_release_page_url(url: Any) -> bool:
    """Is `url` a release page of THIS repo?

    html_url arrives as untrusted API JSON and is the value later opened in
    the user's browser, so it is pinned here rather than merely
    scheme-checked: https, host exactly github.com, path inside this repo's
    /releases/. A release failing this is dropped entirely — there is nothing
    safe to offer the user for it.
    """
    if not isinstance(url, str):
        return False
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.netloc == RELEASE_URL_HOST
        and parts.path.startswith(_RELEASE_URL_PREFIX)
        and len(parts.path) > len(_RELEASE_URL_PREFIX)
    )


def parse_release(raw: Any) -> Optional[Dict[str, Any]]:
    """Validate one GitHub Releases API entry. Accepts only well-formed
    data; anything else is dropped so a stray entry cannot be promoted."""
    if not isinstance(raw, dict):
        return None
    version = version_from_tag(raw.get("tag_name"))
    if version is None:
        return None
    if not isinstance(raw.get("prerelease"), bool):
        return None
    if not isinstance(raw.get("draft"), bool):
        return None
    url = raw.get("html_url")
    if not is_release_page_url(url):
        return None
    assets = raw.get("assets")
    if not isinstance(assets, list):
        return None
    names = [
        a.get("name") for a in assets
        if isinstance(a, dict) and isinstance(a.get("name"), str)
    ]
    return {
        "version": version,
        "url": url,
        "draft": raw["draft"],
        "prerelease": raw["prerelease"],
        "assets": names,
    }


def collect_releases(
    channel: str,
    fetch_page: Optional[Callable[[int], List[Any]]] = None,
) -> List[Dict[str, Any]]:
    """Fetch and validate releases for a channel. Pure over `fetch_page`,
    which returns already-parsed JSON lists (raises on network/HTTP errors)."""
    if fetch_page is None:
        fetch_page = _default_fetch_page
    releases: List[Dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        for raw in fetch_page(page):
            parsed = parse_release(raw)
            if parsed is None:
                continue
            if parsed["draft"]:
                continue
            if channel == "stable" and parsed["prerelease"]:
                continue
            releases.append(parsed)
    return releases


def pick_release_for_channel(
    releases: List[Any],
    channel: str,
) -> Optional[Dict[str, Any]]:
    """Newest valid release for the ring, by semver order. Accepts raw API
    entries or raw JSON lists — every entry is re-validated here; the API
    response is untrusted input and this is the one place that decides what
    counts as a release."""
    candidates = []
    for raw in releases:
        # collect_releases() output (already validated, keyed by "version")
        # passes through; anything else is re-parsed/re-validated here.
        if isinstance(raw, dict) and "version" in raw and "assets" in raw:
            r = raw
        else:
            r = parse_release(raw)
        if r is None:
            continue
        if r.get("draft") is True:
            continue
        if channel == "stable" and r.get("prerelease") is True:
            continue
        candidates.append(r)
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: tuple(int(x) for x in r["version"].split(".")),
        reverse=True,
    )
    return candidates[0]


def asset_for_platform(release: Dict[str, Any], platform: str) -> Optional[str]:
    """Asset file name to download for this platform, or None when the
    release does not ship one."""
    wanted = PLATFORM_ASSETS.get(platform)
    if not wanted:
        return None
    for name in release.get("assets", []):
        if name == wanted:
            return name
    return None


def is_notifiable_update(current_version: str, picked: Optional[Dict[str, Any]]) -> bool:
    """True when `picked` is strictly NEWER than what is running. Never
    compares equal or lower — a downgrade is a reinstall, not an update."""
    if picked is None:
        return False
    from update_settings import parse_semver
    cur, new = parse_semver(current_version), parse_semver(picked["version"])
    if cur is None or new is None:
        return False
    return new > cur


def _default_fetch_page(page: int) -> List[Any]:
    req = urllib.request.Request(
        f"{RELEASES_API}?per_page={RELEASES_PER_PAGE}&page={page}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"api-printer-service/{_current_version_or_unknown()}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"GitHub returned {resp.status}")
        body = json.load(resp)
    if not isinstance(body, list):
        raise RuntimeError("unexpected releases payload")
    return body


def _current_version_or_unknown() -> str:
    try:
        from config import Config  # noqa: circular-safe: only for UA string
        v = getattr(Config, "VERSION", None)  # type: ignore[attr-defined]
        return v if isinstance(v, str) and v else "unknown"
    except Exception:
        return "unknown"
