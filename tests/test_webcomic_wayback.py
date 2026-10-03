"""Wayback completeness report: bounded lookups, distinct capture vs publication dates, gaps with evidence."""
import json
import tempfile
import unittest
import urllib.parse

from media_intake.core import connect
from media_intake.webcomics import wayback
from media_intake.webcomics.http import Response


class CdxClient:
    def __init__(self, captured):
        self.captured, self.calls = captured, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        page = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['url'][0]
        rows = [['timestamp', 'original', 'statuscode']]
        for timestamp in self.captured.get(page, []):
            rows.append([timestamp, page, '200'])
        return Response(json.dumps(rows if len(rows) > 1 else []).encode(), url, {})


class WaybackTests(unittest.TestCase):
    def test_report_maps_captures_and_reports_gaps_within_sample(self):
        with tempfile.TemporaryDirectory() as root:
            db = connect(root)
            try:
                db.execute("INSERT INTO watched_items(kind,title,state,metadata) VALUES('webcomic','Old Comic','watching','{}')")
                db.execute("INSERT INTO webcomic_series(id,metadata) VALUES('s','{}')")
                db.execute("INSERT INTO webcomic_sources(series_id,watch_id,url,adapter,adapter_version) VALUES('s',1,'https://old.example/','static','1')")
                for n in range(1, 8):
                    db.execute("""INSERT INTO webcomic_entries(source_id,entry_key,url,ordinal,published_at,metadata,fingerprint)
                        VALUES(1,?,?,?,?,'{}','f')""", (f'p{n}', f'https://old.example/p/{n}', n, f'2009-0{n}-01'))
                db.commit()
                client = CdxClient({'https://old.example/p/1': ['20100105120000', '20150101000000'],
                                    'https://old.example/p/2': ['20100210000000']})
                report = wayback.completeness_report(db, 1, client, sample=3)
                self.assertEqual(report['checked'], 3)
                self.assertEqual(len(client.calls), 3)  # bounded: no crawl beyond the sample
                first = report['captured'][0]
                self.assertEqual((first['entry'], first['published_at'], first['captured_at'][:10]),
                                 ('p1', '2009-01-01', '2015-01-01'))  # publication and capture dates stay distinct
                self.assertEqual(first['capture_url'], 'https://web.archive.org/web/20150101000000/https://old.example/p/1')
                self.assertEqual([gap['entry'] for gap in report['gaps']], ['p3'])
                self.assertIn('No successful Wayback capture', report['gaps'][0]['evidence'])
                report = wayback.completeness_report(db, 1, client, sample=10)  # continues with unchecked entries
                self.assertEqual(report['checked'], 4)
                self.assertEqual(len(report['gaps']), 5)
                self.assertEqual(wayback.completeness_report(db, 1, client)['checked'], 0)  # nothing stale yet
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
