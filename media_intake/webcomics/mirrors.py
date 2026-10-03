"""Mirror reconciliation: one webcomic followed from several sources (creator site, WEBTOON, Tapas...).

- ``find_candidates`` scores every pair of series with the existing identity
  rules: the same canonical URL or a shared catalog ID may link automatically;
  a matching title and creator is only proposed for review, and a title alone
  scores too low to propose.
- Confirming a pair makes both sources share one series record while each keeps
  its own entries and numbering; a language or edition note can be stored.
- ``split`` reverses a confirmation, restoring the source's original series.
"""
import json

from .metadata import match_entities
from .models import WebcomicSeries
from .storage import event

PROPOSE_AT = 0.8


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS webcomic_mirror_links(
        id INTEGER PRIMARY KEY, series_a TEXT NOT NULL, series_b TEXT NOT NULL, confidence REAL NOT NULL,
        evidence TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', language TEXT, edition TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(series_a, series_b))''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(webcomic_sources)')}
    if 'original_series_id' not in columns:
        db.execute('ALTER TABLE webcomic_sources ADD COLUMN original_series_id TEXT')


def _series(db, series_id):
    row = db.execute('SELECT metadata FROM webcomic_series WHERE id=?', (series_id,)).fetchone()
    data = json.loads(row['metadata']) if row else {}
    fields = WebcomicSeries.__dataclass_fields__
    return WebcomicSeries(**{key: value for key, value in data.items() if key in fields} |
                          {'canonical_id': series_id, 'title': data.get('title') or '',
                           'canonical_source_url': data.get('canonical_source_url') or '', 'platform': data.get('platform') or ''})


def find_candidates(db):
    """Store new mirror proposals (or automatic links) between distinct series; returns them."""
    ids = [row[0] for row in db.execute('SELECT DISTINCT series_id FROM webcomic_sources ORDER BY series_id')]
    found = []
    for index, left_id in enumerate(ids):
        left = _series(db, left_id)
        for right_id in ids[index + 1:]:
            if db.execute('SELECT 1 FROM webcomic_mirror_links WHERE series_a=? AND series_b=?', (left_id, right_id)).fetchone():
                continue
            match = match_entities(left, _series(db, right_id))
            if match['confidence'] < PROPOSE_AT and not match['auto_merge']:
                continue
            db.execute('INSERT INTO webcomic_mirror_links(series_a, series_b, confidence, evidence, status) VALUES(?,?,?,?,?)',
                       (left_id, right_id, match['confidence'], json.dumps(match['evidence']), 'proposed'))
            link_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            if match['auto_merge']:
                confirm(db, link_id)
            found.append(link_id)
    db.commit()
    return [link(db, link_id) for link_id in found]


def link(db, link_id):
    row = db.execute('SELECT * FROM webcomic_mirror_links WHERE id=?', (link_id,)).fetchone()
    return {**dict(row), 'evidence': json.loads(row['evidence'])} if row else None


def links(db, status=None):
    sql, params = 'SELECT id FROM webcomic_mirror_links', ()
    if status:
        sql, params = sql + ' WHERE status=?', (status,)
    return [link(db, row[0]) for row in db.execute(sql + ' ORDER BY confidence DESC, id', params)]


def confirm(db, link_id, language=None, edition=None):
    """Let both sources share series_a; each keeps its entries and numbering. Reversible with split()."""
    row = link(db, link_id)
    if row is None:
        raise ValueError('Unknown mirror link')
    for source in db.execute('SELECT id, series_id, original_series_id FROM webcomic_sources WHERE series_id=?',
                             (row['series_b'],)).fetchall():
        db.execute('UPDATE webcomic_sources SET series_id=?, original_series_id=COALESCE(original_series_id, ?) WHERE id=?',
                   (row['series_a'], source['series_id'], source['id']))
        event(db, source['id'], 'MIRROR_CONFIRMED', detail={'shared_series': row['series_a'], 'language': language,
                                                            'edition': edition})
    db.execute("UPDATE webcomic_mirror_links SET status='confirmed', language=?, edition=? WHERE id=?",
               (language, edition, link_id))
    db.commit()


def reject(db, link_id):
    db.execute("UPDATE webcomic_mirror_links SET status='rejected' WHERE id=?", (link_id,))
    db.commit()


def split(db, source_id):
    """Undo a confirmed mirror for one source: it gets its original series back."""
    source = db.execute('SELECT series_id, original_series_id FROM webcomic_sources WHERE id=?', (source_id,)).fetchone()
    if source is None or not source['original_series_id']:
        return False
    db.execute('UPDATE webcomic_sources SET series_id=original_series_id, original_series_id=NULL WHERE id=?', (source_id,))
    db.execute("""UPDATE webcomic_mirror_links SET status='split' WHERE status='confirmed'
        AND series_a=? AND series_b=?""", (source['series_id'], source['original_series_id']))
    event(db, source_id, 'MIRROR_SPLIT', detail={'restored_series': source['original_series_id']})
    db.commit()
    return True


def mirrors_of(db, series_id):
    """Every source that shares this series, with its own entry count and numbering intact."""
    return [dict(row) for row in db.execute('''SELECT s.id, s.url, s.adapter, s.original_series_id,
        (SELECT COUNT(*) FROM webcomic_entries e WHERE e.source_id=s.id) AS entries
        FROM webcomic_sources s WHERE s.series_id=? ORDER BY s.id''', (series_id,))]
