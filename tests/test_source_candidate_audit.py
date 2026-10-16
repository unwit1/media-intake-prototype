"""Offline, negative-control regression tests for PirateBot candidate intake."""
import unittest

from media_intake.discovery import CATEGORIES, DOWNLOAD_CONNECTORS, _catalog_rows
from media_intake.rights_evidence import DOWNLOAD_SOURCES, initial_document, source_download_rights
from media_intake.source_candidate_audit import (
    find_research_source_candidates, load_research_source_candidates, metadata_preview,
)


class PiratebotCandidateAuditTests(unittest.TestCase):
    def test_fifteen_distinct_prioritized_sources_have_original_provenance(self):
        records = load_research_source_candidates()
        self.assertEqual(len(records), 15)
        self.assertEqual([r["rank"] for r in records], list(range(1, 16)))
        self.assertEqual(len({r["piratebot_id"] for r in records}), 15)
        self.assertEqual(len({r["name"] for r in records}), 15)
        for row in records:
            with self.subTest(source=row["name"]):
                self.assertTrue(row["source_ids"])
                self.assertIn(row["observational_state"], {
                    "historical_reference", "named_in_source_not_live_checked"})
                self.assertTrue(set(row["proposed_media_intake_categories"]) <= set(CATEGORIES))
                self.assertEqual(row["review_state"], "research_only")

    def test_no_candidate_silently_becomes_an_enabled_connector(self):
        approved = initial_document()
        for row in load_research_source_candidates():
            with self.subTest(source=row["name"]):
                self.assertFalse(row["live_search_implemented"])
                self.assertFalse(row["direct_download_implemented"])
                self.assertFalse(row["default_approved_for_download"])
                self.assertNotIn(row["piratebot_id"], DOWNLOAD_SOURCES)
                self.assertIsNone(source_download_rights(approved, "research:" + row["piratebot_id"]))
                for connectors in DOWNLOAD_CONNECTORS.values():
                    self.assertNotIn(row["name"], connectors)

    def test_candidate_lookup_is_local_and_only_returns_metadata(self):
        matches = find_research_source_candidates("nyaa", "Anime")
        self.assertEqual([row["piratebot_id"] for row in matches], ["nyaa"])
        self.assertEqual(find_research_source_candidates("nyaa", "Books"), [])
        results = _catalog_rows([metadata_preview(matches[0])], "Anime")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["format"], "catalog")
        self.assertEqual(results[0]["source_role"], "metadata")
        self.assertFalse(results[0]["downloadable"])
        self.assertTrue(results[0]["discovery_only"])
        self.assertEqual(results[0]["url"], "")

    def test_existing_source_rights_are_unmodified(self):
        document = initial_document()
        self.assertIsNone(source_download_rights(document, "jackett"))
        self.assertIsNone(source_download_rights(document, "internet_archive"))
        self.assertIsNotNone(source_download_rights(document, "podcast_rss"))

    def test_research_catalog_rejects_promoted_or_malformed_records(self):
        with self.assertRaises(ValueError):
            metadata_preview({"name": "Forged", "review_state": "approved"})


if __name__ == "__main__":
    unittest.main()
