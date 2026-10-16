"""Read the PirateBot-derived source candidates and how each became a connector.

The registry records provenance and promotion state only. It provides no URLs,
search adapters or download authorization and must never be used to construct
queue items: the working connectors live in media_intake/connectors, and every
file-capable one still needs a YES in the user's rights-evidence.json.
"""
import json
from pathlib import Path

REVIEW_STATES = {"research_only", "implemented_connector"}
_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "docs" / "piratebot_source_candidates_2026-10-16.json"


def load_research_source_candidates(path=None):
    """Return source research records, without making any network requests."""
    filename = Path(path) if path is not None else _REGISTRY_PATH
    document = json.loads(filename.read_text(encoding="utf-8"))
    if document.get("schema_version") != 2 or document.get("candidate_count") != 15:
        raise ValueError("Unexpected research source catalog schema or record count")
    candidates = document.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 15:
        raise ValueError("Incomplete research source catalog")
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("review_state") not in REVIEW_STATES:
            raise ValueError("Unknown review state in source catalog")
        if candidate.get("default_approved_for_download") is not False:
            raise ValueError("A cataloged source must never default to download approval")
        if candidate.get("review_state") == "research_only" and (
                candidate.get("live_search_implemented") is not False or
                candidate.get("direct_download_implemented") is not False):
            raise ValueError("Research-only source must not claim runtime eligibility")
    return candidates


def find_research_source_candidates(query="", category=None, path=None):
    """Find named research leads; this is NOT a search for media files."""
    query = str(query or "").strip().casefold()
    results = []
    for candidate in load_research_source_candidates(path):
        if category and category not in candidate["proposed_media_intake_categories"]:
            continue
        if query and query not in candidate["name"].casefold() and query not in candidate["site_type"].casefold():
            continue
        results.append(dict(candidate))
    return results


def metadata_preview(candidate):
    """Build an explicitly non-downloadable reference card for review screens."""
    if candidate.get("review_state") not in REVIEW_STATES:
        raise ValueError("Expected a cataloged source candidate")
    return {
        "title": candidate["name"],
        "provider": "PirateBot research",
        "source": "PirateBot research",
        "source_key": "research:" + candidate["piratebot_id"],
        "source_role": "metadata",
        "format": "catalog",
        "discovery_only": True,
        "downloadable": False,
        "url": "",
        "source_url": "",
        "description": candidate["coverage_hypothesis"],
    }
