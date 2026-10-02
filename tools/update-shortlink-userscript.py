"""Re-pin the vendored "Bypass All Shortlinks Debloated" userscript.

Usage:  python tools/update-shortlink-userscript.py [COMMIT]

Downloads the userscript and MonkeyConfig-Mod.js at COMMIT (default: the current
main branch head, resolved to a commit ID) from Codeberg, then rewrites
media_intake/data/shortlinks/SOURCE.json with the commit and SHA-256 checksums.
Review the diff before committing: this code runs inside the shortlink helper.
"""
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.request

REPOSITORY = 'https://codeberg.org/gongchandang49/bypass-all-shortlinks-debloated'
API = 'https://codeberg.org/api/v1/repos/gongchandang49/bypass-all-shortlinks-debloated'
FILES = ('Bypass_All_Shortlinks.user.js', 'MonkeyConfig-Mod.js', 'LICENSE')
DATA = Path(__file__).resolve().parents[1] / 'media_intake' / 'data' / 'shortlinks'


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'MediaArchiveImporter shortlink updater'})
    with urllib.request.urlopen(request, timeout=60) as reply:
        return reply.read(20_000_000)


def main(commit=None):
    if not commit:
        commit = json.loads(fetch(f'{API}/branches/main'))['commit']['id']
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise SystemExit('Give a full 40-character commit ID')
    checksums = {}
    for name in FILES:
        body = fetch(f'{REPOSITORY}/raw/commit/{commit}/{name}')
        (DATA / name).write_bytes(body)
        checksums[name] = hashlib.sha256(body).hexdigest()
    script = (DATA / FILES[0]).read_text(encoding='utf-8')
    version = re.search(r'@version\s+(\S+)', script).group(1)
    source = {'repository': REPOSITORY, 'commit': commit, 'userscript_version': version,
              'license': 'Unlicense (public domain)', 'sha256': dict(sorted(checksums.items()))}
    (DATA / 'SOURCE.json').write_text(json.dumps(source, indent=2) + '\n', encoding='utf-8')
    print(f'Pinned {version} at {commit}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else None)
