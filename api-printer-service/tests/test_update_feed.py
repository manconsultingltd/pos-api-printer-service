"""Pure-logic tests for the GitHub Releases ring resolution.

Philosophy: the API response is
untrusted input, so the parsers must only promote well-formed data.
"""

from update_feed import (
    REPO_SLUG,
    collect_releases,
    is_notifiable_update,
    is_release_page_url,
    parse_release,
    pick_release_for_channel,
    version_from_tag,
)


def make_release(version, *, prerelease=False, draft=False, asset="api-printer-setup-windows.exe"):
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/{REPO_SLUG}/releases/tag/v{version}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": [{"name": asset, "size": 1}],
    }


class TestVersionFromTag:
    def test_valid(self):
        assert version_from_tag("v1.1.50") == "1.1.50"

    def test_strict(self):
        for tag in ("1.1.50", "v1.1", "v1.1.50-beta", "vbanana", "v1.1.50.1", "", "v", None):
            assert version_from_tag(tag) is None


class TestReleaseUrl:
    def test_release_page_ok(self):
        assert is_release_page_url(f"https://github.com/{REPO_SLUG}/releases/tag/v1.1.5") is True

    def test_foreign_repo_rejected(self):
        assert is_release_page_url("https://github.com/evil/repo/releases/tag/v1.0.0") is False

    def test_non_https_rejected(self):
        assert is_release_page_url(f"http://github.com/{REPO_SLUG}/releases/tag/v1.0.0") is False

    def test_non_url_rejected(self):
        assert is_release_page_url(None) is False
        assert is_release_page_url(f"https://github.com/{REPO_SLUG}/releases/") is False


class TestParseRelease:
    def test_valid(self):
        r = parse_release(make_release("1.1.5"))
        assert r is not None and r["version"] == "1.1.5"

    def test_stray_tag_dropped(self):
        raw = make_release("1.1.5")
        raw["tag_name"] = "nightly-build"
        assert parse_release(raw) is None

    def test_bad_url_dropped(self):
        raw = make_release("1.1.5")
        raw["html_url"] = "https://evil.example/releases/tag/v1.1.5"
        assert parse_release(raw) is None

    def test_missing_assets_dropped(self):
        raw = make_release("1.1.5")
        del raw["assets"]
        assert parse_release(raw) is None


class TestPickRelease:
    def test_stable_skips_prerelease(self):
        releases = [make_release("1.1.50"), make_release("1.1.51", prerelease=True)]
        picked = pick_release_for_channel(releases, "stable")
        assert picked["version"] == "1.1.50"

    def test_preview_takes_prerelease(self):
        releases = [make_release("1.1.50"), make_release("1.1.51", prerelease=True)]
        picked = pick_release_for_channel(releases, "preview")
        assert picked["version"] == "1.1.51"

    def test_newest_wins_not_first(self):
        releases = [make_release("1.1.9"), make_release("1.1.50")]
        assert pick_release_for_channel(releases, "stable")["version"] == "1.1.50"

    def test_draft_never_promoted(self):
        releases = [make_release("1.1.99", draft=True)]
        assert pick_release_for_channel(releases, "preview") is None

    def test_empty(self):
        assert pick_release_for_channel([], "stable") is None

    def test_unvalidated_raw_entries_accepted(self):
        # pick re-validates raw API dicts, not just pre-parsed ones
        releases = [make_release("1.1.7"), {"garbage": True}, None]
        assert pick_release_for_channel(releases, "stable")["version"] == "1.1.7"


class TestCollectReleases:
    def test_pages_fetched_and_filtered(self):
        pages = {
            1: [make_release("1.1.50"), make_release("1.1.51", prerelease=True)],
            2: [make_release("1.1.30")],
            3: [],
        }
        picked = pick_release_for_channel(
            collect_releases("stable", fetch_page=lambda p: pages[p]), "stable"
        )
        assert picked["version"] == "1.1.50"


class TestNotifiable:
    def test_only_strictly_newer(self):
        def rel(v):
            return {"version": v, "url": f"https://github.com/{REPO_SLUG}/releases/tag/v{v}",
                    "draft": False, "prerelease": False, "assets": []}

        assert is_notifiable_update("1.1.50", rel("1.1.51")) is True
        assert is_notifiable_update("1.1.51", rel("1.1.50")) is False
        assert is_notifiable_update("1.1.50", rel("1.1.50")) is False
        assert is_notifiable_update("1.1.50", None) is False
