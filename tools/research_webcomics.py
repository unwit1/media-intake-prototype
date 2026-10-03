"""Refresh a dated, public upstream research snapshot; never vendor source code."""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import urllib.request

REPOS = [
    'webcomics/dosage', 'mikf/gallery-dl', 'yt-dlp/yt-dlp',
    'manga-download/hakuneko', 'manga-download/haruneko', 'manga-py/manga-py',
    'metafates/mangal', 'fmd-project-team/FMD', 'dazedcat19/FMD2',
    'mihonapp/mihon', 'keiyoushi/extensions-source',
    'Paperback-iOS/extensions-common', 'Paperback-iOS/extensions',
    'potatoeggy/mandown', 'Girbons/comics-downloader',
    'Zehina/Webtoon-Downloader', 'TilCreator/Tapas-Comic-Downloader',
    'Webtoon-Studio/webtoon', 'Haaglar/webcomic-scrape',
    'heyeinin/comicfury-documentation', 'mgsisk/webcomic',
    'DIYgod/RSSHub', 'kurtmckee/feedparser', 'scrapy/scrapy',
    'microsoft/playwright-python', 'Snd-R/komf', 'comictagger/comictagger',
    'anansi-project/comicinfo', 'gotson/komga', 'Kareadita/Kavita',
    'internetarchive/wayback', 'readium/opds',
    'Frumph/comicpress', 'Frumph/comic-easel', 'kmhcreative/comicpost',
]


def get(url):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'MediaIntake-Webcomics-Research/1.0',
        'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read(2_000_000)


def inspect(repo):
    row = {'repository': repo, 'url': f'https://github.com/{repo}'}
    try:
        data = json.loads(get(f'https://api.github.com/repos/{repo}'))
        for key in ('description', 'language', 'archived', 'pushed_at', 'updated_at',
                    'default_branch', 'license', 'html_url'):
            row[key] = data.get(key)
        branch = data['default_branch']
        row['references'] = [f'https://github.com/{repo}/tree/{branch}',
                             f'https://github.com/{repo}/commits/{branch}']
    except Exception as exc:
        row['error'] = str(exc)
    return row


if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(inspect, REPOS))
    folder = Path(__file__).resolve().parents[1] / 'docs' / 'webcomics' / 'research'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'upstream-snapshot.json').write_text(json.dumps({
        'retrieved_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'purpose': 'Dated upstream facts and references only; no source or README text vendored.',
        'repositories': rows}, indent=2, ensure_ascii=False), encoding='utf-8')
    for row in rows:
        print(row['repository'], row.get('language'),
              (row.get('license') or {}).get('spdx_id'),
              row.get('pushed_at'), 'ARCHIVED' if row.get('archived') else '', row.get('error', ''))
