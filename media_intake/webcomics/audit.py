"""Sampled, bounded audits of archived comic images.

Each audit revalidates the images of a few archived entries (oldest-audited
first) with conditional requests. Unchanged images cost a 304. When the bytes at
an unchanged URL differ, the entry gets a new revision and is archived again;
the earlier archive file is kept, so old art is never lost.
"""
import hashlib
import json
import time

from .downloads import fetch_asset
from .http import SourceUnavailable
from .storage import event

DEFAULT_SAMPLE = 5


def audit_source(db, source_id, client, sample=DEFAULT_SAMPLE):
    """Audit up to `sample` archived entries; returns {'checked', 'changed', 'unchanged', 'errors'}."""
    outcome = {'checked': 0, 'changed': 0, 'unchanged': 0, 'errors': 0}
    entries = db.execute("""SELECT e.* FROM webcomic_entries e WHERE e.source_id=? AND e.state='archived' AND e.removed=0
        ORDER BY COALESCE(e.audited_at, 0), e.ordinal LIMIT ?""",
                         (source_id, max(1, int(sample)))).fetchall()
    for entry in entries:
        assets = [json.loads(row['metadata']) for row in db.execute(
            'SELECT metadata FROM webcomic_assets WHERE entry_id=? AND revision=? ORDER BY ordinal',
            (entry['id'], entry['fingerprint']))]
        changed = []
        for asset in assets:
            outcome['checked'] += 1
            try:
                response = fetch_asset(client, asset['source_url'], json.loads(entry['metadata']).get('source_url', ''), asset)
            except SourceUnavailable:
                outcome['errors'] += 1
                continue
            if response.status == 304:
                outcome['unchanged'] += 1
            else:
                changed.append({'url': asset['source_url'], 'old': asset.get('checksum'),
                                'new': hashlib.sha256(response.body).hexdigest()})
        if changed:
            outcome['changed'] += len(changed)
            # A new revision re-archives the entry; the old archive file stays on disk.
            revision = entry['fingerprint'].split(':asset:')[0] + ':asset:' + changed[0]['new'][:12]
            db.execute("UPDATE webcomic_entries SET fingerprint=?, state='pending', queue_id=NULL, audited_at=? WHERE id=?",
                       (revision, time.time(), entry['id']))
            event(db, source_id, 'ASSET_CHANGED', entry['entry_key'], {'assets': changed, 'revision': revision})
        else:
            db.execute('UPDATE webcomic_entries SET audited_at=? WHERE id=?', (time.time(), entry['id']))
    db.commit()
    return outcome
