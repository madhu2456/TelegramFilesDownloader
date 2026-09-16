"""Automated verification test suite for Canonical Identity Anchors and rel="me" attribution."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

CANONICAL_SAME_AS = {
    "https://www.wikidata.org/wiki/Q139807441",
    "https://github.com/madhu2456",
    "https://www.linkedin.com/in/madhu-dadi-54684531",
    "https://x.com/madhu245",
    "https://medium.com/@madhu.kumar245",
    "https://dev.to/madhudadi",
    "https://www.youtube.com/@madhukumar245",
    "https://maps.google.com/?cid=CXaUijPkQhVkEBM",
}

STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "static"


def test_canonical_eight_anchors_exact_match():
    """Assert exact set match with CANONICAL_SAME_AS in landing.html and ai-profile.json."""
    # 1. Check ai-profile.json
    ai_profile_path = STATIC_DIR / "ai-profile.json"
    assert ai_profile_path.is_file(), "ai-profile.json must exist"
    ai_data = json.loads(ai_profile_path.read_text(encoding="utf-8"))
    author_same_as = set(ai_data.get("author", {}).get("sameAs", []))
    assert author_same_as == CANONICAL_SAME_AS, (
        f"ai-profile.json author sameAs mismatch:\nExpected: {CANONICAL_SAME_AS}\nFound: {author_same_as}"
    )

    # 2. Check landing.html JSON-LD
    landing_html = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
    json_ld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', landing_html, re.DOTALL)
    assert json_ld_match is not None, "JSON-LD missing in landing.html"
    landing_graph = json.loads(json_ld_match.group(1)).get("@graph", [])

    person_node = next((node for node in landing_graph if node.get("@type") == "Person"), None)
    assert person_node is not None, "Person node missing in landing.html @graph"
    person_same_as = set(person_node.get("sameAs", []))
    assert person_same_as == CANONICAL_SAME_AS, (
        f"landing.html Person sameAs mismatch:\nExpected: {CANONICAL_SAME_AS}\nFound: {person_same_as}"
    )


def test_zero_product_domains_in_same_as():
    """Assert zero product domains (televault, adticks, etc.) pollute Person sameAs."""
    ai_profile_path = STATIC_DIR / "ai-profile.json"
    ai_data = json.loads(ai_profile_path.read_text(encoding="utf-8"))
    ai_same_as = ai_data.get("author", {}).get("sameAs", [])

    landing_html = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
    json_ld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', landing_html, re.DOTALL)
    assert json_ld_match is not None
    landing_graph = json.loads(json_ld_match.group(1)).get("@graph", [])
    person_node = next((node for node in landing_graph if node.get("@type") == "Person"), {})
    dom_same_as = person_node.get("sameAs", [])

    forbidden_patterns = ["televault", "adticks", "deals", "enroller", "udemy"]
    for url in ai_same_as + dom_same_as:
        for forbidden in forbidden_patterns:
            assert forbidden not in url.lower(), f"Forbidden product domain '{forbidden}' found in sameAs URL: {url}"


def test_footer_rel_me_links():
    """Validate that links to madhudadi.in and madhudadi.in/profile/ have rel containing 'me'."""
    landing_html = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")

    class LinkParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.links: list[tuple[str, str]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "a":
                attr_dict = dict(attrs)
                href = attr_dict.get("href") or ""
                rel = attr_dict.get("rel") or ""
                self.links.append((href, rel))

    parser = LinkParser()
    parser.feed(landing_html)

    ecosystem_links_found = 0
    profile_links_found = 0

    for href, rel in parser.links:
        if href == "https://madhudadi.in/":
            ecosystem_links_found += 1
            rel_tokens = rel.split()
            assert "me" in rel_tokens, f"Expected 'me' in rel for {href}, found '{rel}'"
            assert "noopener" in rel_tokens
            assert "noreferrer" in rel_tokens
        elif href == "https://madhudadi.in/profile/":
            profile_links_found += 1
            rel_tokens = rel.split()
            assert "me" in rel_tokens, f"Expected 'me' in rel for {href}, found '{rel}'"
            assert "noopener" in rel_tokens
            assert "noreferrer" in rel_tokens

    assert ecosystem_links_found >= 1, "Expected at least 1 link to https://madhudadi.in/"
    assert profile_links_found >= 1, "Expected at least 1 link to https://madhudadi.in/profile/"
