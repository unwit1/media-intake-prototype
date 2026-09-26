import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from media_intake.core import connect, download, enqueue
from media_intake.rights_evidence import (ensure_rights_evidence,
                                          load_rights_evidence,
                                          set_work_rights,
                                          source_download_rights)


class RightsEvidenceTest(unittest.TestCase):
    def test_download_sources_start_with_explicit_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ensure_rights_evidence(Path(tmp) / 'rights-evidence.json')
            document = load_rights_evidence(path)
            for source in ('getcomics.org', 'lnori.com', 'novelarchive.cc'):
                self.assertIsNone(source_download_rights(document, source))
                self.assertIs(document['sources'][source]['approved_for_download'], False)

    def test_only_json_boolean_yes_approves_a_source(self):
        document = {'schema_version': 1, 'sources': {
            'lnori.com': {'approved_for_download': 'true'}}, 'works': {}}
        self.assertIsNone(source_download_rights(document, 'lnori.com'))
        document['sources']['lnori.com']['approved_for_download'] = True
        self.assertIsNotNone(source_download_rights(document, 'lnori.com'))

    def test_work_yes_and_no_are_saved_in_the_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ensure_rights_evidence(Path(tmp) / 'rights-evidence.json')
            watch = {'id': 8, 'kind': 'comic', 'title': 'Sample Comic',
                     'source_url': 'https://example.org/comic'}
            yes = set_work_rights(path, watch, 'permission_granted',
                                  'Permission email from publisher')
            self.assertTrue(yes['approved_for_download'])
            no = set_work_rights(path, watch, 'restricted')
            self.assertFalse(no['approved_for_download'])
            document = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(document['works']['url:https://example.org/comic']['approved_for_download'])
            self.assertEqual(document['works']['url:https://example.org/comic']['status'], 'restricted')

    def test_malformed_local_file_is_reported_and_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rights-evidence.json'
            path.write_text('{broken', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Could not read rights evidence'):
                load_rights_evidence(path)
            self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    def test_explicit_but_missing_rights_file_fails_closed_before_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / 'missing-rights.json'
            with closing(connect(root / 'data')) as db:
                row = enqueue(db, {'title': 'Sample', 'author': 'Writer',
                                   'url': 'https://files.example.test/sample.pdf',
                                   'format': '.pdf', 'source': 'fixture',
                                   'source_key': 'lnori.com',
                                   'rights_evidence_path': str(missing)})
                with patch('urllib.request.urlopen') as request:
                    with self.assertRaisesRegex(ValueError, 'rights evidence file is missing'):
                        download(db, row['id'], root)
                request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
