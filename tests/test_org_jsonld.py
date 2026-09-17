"""Automated verification test suite for Schema.org linked graph topology."""

import json
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "static"


def test_schema_graph_topology():
    """Validates Schema.org 6-entity topology and cross-entity relational integrity."""
    landing_html = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
    json_ld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', landing_html, re.DOTALL)
    assert json_ld_match is not None, "JSON-LD script missing in landing.html"

    graph = json.loads(json_ld_match.group(1)).get("@graph", [])
    nodes_by_id = {node.get("@id"): node for node in graph if "@id" in node}

    # Verify presence of all required entity nodes
    assert "https://televault.madhudadi.in/#organization" in nodes_by_id
    assert "https://madhudadi.in/#person" in nodes_by_id
    assert "https://televault.madhudadi.in/#website" in nodes_by_id
    assert "https://televault.madhudadi.in/#webapp" in nodes_by_id
    assert "https://televault.madhudadi.in/#breadcrumb" in nodes_by_id
    assert "https://televault.madhudadi.in/#faq" in nodes_by_id

    org = nodes_by_id["https://televault.madhudadi.in/#organization"]
    person = nodes_by_id["https://madhudadi.in/#person"]
    website = nodes_by_id["https://televault.madhudadi.in/#website"]
    webapp = nodes_by_id["https://televault.madhudadi.in/#webapp"]

    # 1. Organization links
    parent_org = org.get("parentOrganization", {})
    assert parent_org.get("@id") == "https://madhudadi.in/#organization"
    founder = org.get("founder", {})
    assert founder.get("@id") == "https://madhudadi.in/#person"

    # 2. Person links
    assert person.get("url") == "https://madhudadi.in/profile/"
    assert len(person.get("sameAs", [])) == 8

    # 3. WebSite links
    assert website.get("isPartOf", {}).get("@id") == "https://madhudadi.in/#website"
    assert website.get("publisher", {}).get("@id") == "https://televault.madhudadi.in/#organization"

    # 4. WebApplication links
    assert webapp.get("url") == "https://televault.madhudadi.in/app"
    assert webapp.get("author", {}).get("@id") == "https://madhudadi.in/#person"
    assert webapp.get("creator", {}).get("@id") == "https://madhudadi.in/#person"
    assert webapp.get("publisher", {}).get("@id") == "https://televault.madhudadi.in/#organization"
    assert webapp.get("isPartOf", {}).get("@id") == "https://televault.madhudadi.in/#website"


def test_app_schema_webapp_markup():
    """Validates Schema.org WebApplication markup in /app (index.html)."""
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    json_ld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', index_html, re.DOTALL)
    assert json_ld_match is not None, "JSON-LD script missing in index.html"

    data = json.loads(json_ld_match.group(1))
    assert data.get("@context") == "https://schema.org"
    assert data.get("@type") == "WebApplication"
    assert data.get("@id") == "https://televault.madhudadi.in/#webapp"
    assert data.get("name") == "TeleVault"
    assert data.get("url") == "https://televault.madhudadi.in/app"
    assert data.get("applicationCategory") == "MultimediaApplication"
    assert data.get("author", {}).get("@id") == "https://madhudadi.in/#person"
