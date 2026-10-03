import unittest

from media_intake.book_identifiers import (book_work_title_key, collect_isbns,
                                           normalize_isbn)


class BookIdentifiersTest(unittest.TestCase):
    def test_title_key_removes_only_final_common_format_labels(self):
        self.assertEqual(book_work_title_key('Example Novel'),
                         book_work_title_key('Example Novel (Paperback)'))
        self.assertEqual(book_work_title_key('Example Novel'),
                         book_work_title_key('Example Novel Kindle Edition'))
        self.assertNotEqual(book_work_title_key('The Paperback Writer'),
                            book_work_title_key('The Writer'))
        self.assertNotEqual(book_work_title_key('Example Novel'),
                            book_work_title_key('Example Novel Revised Edition'))

    def test_title_key_normalizes_catalog_inverted_articles(self):
        self.assertEqual(book_work_title_key('The Hobbit'),
                         book_work_title_key('Hobbit, The'))
        self.assertEqual(book_work_title_key('The Hobbit (Paperback)'),
                         book_work_title_key('Hobbit, The (Paperback)'))
        self.assertEqual(book_work_title_key('A Wizard of Earthsea Kindle Edition'),
                         book_work_title_key('Wizard of Earthsea, A Kindle Edition'))
        self.assertNotEqual(book_work_title_key('The Hobbit Revised Edition'),
                            book_work_title_key('The Hobbit'))

    def test_normalizes_valid_isbn_10_and_isbn_13_to_one_key(self):
        self.assertEqual(normalize_isbn('0-306-40615-2'), '9780306406157')
        self.assertEqual(normalize_isbn('978-0-306-40615-7'), '9780306406157')

    def test_rejects_bad_checksums_and_non_isbn_numbers(self):
        self.assertIsNone(normalize_isbn('9780306406158'))
        self.assertIsNone(normalize_isbn('1234567890'))
        self.assertIsNone(normalize_isbn('978030640615X'))

    def test_collects_schema_property_value_isbns_and_deduplicates(self):
        values = collect_isbns([
            '0-306-40615-2',
            {'@type': 'PropertyValue', 'propertyID': 'ISBN-13',
             'value': '9780306406157'},
            {'@type': 'PropertyValue', 'propertyID': 'Goodreads ID',
             'value': '1234567890'},
        ])
        self.assertEqual(values, ['9780306406157'])


if __name__ == '__main__':
    unittest.main()
