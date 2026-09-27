"""FanFicFare-backed downloads for watched stories."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .core import MAX_FILE_BYTES, check_download, file_name


def story_url_key(url):
    parsed = urllib.parse.urlsplit(str(url or '').strip())
    if (parsed.scheme not in ('http', 'https') or not parsed.netloc or
            parsed.username or parsed.password):
        raise ValueError('Fanfiction story URL must be public HTTP(S) without embedded credentials')
    return 'fanfic+https://story/' + hashlib.sha256(url.strip().encode('utf-8')).hexdigest()


def _fanficfare_command():
    configured = os.environ.get('FANFICFARE_EXECUTABLE', '').strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return [str(path)]
        raise ValueError('FANFICFARE_EXECUTABLE does not point to a file')

    if getattr(sys, 'frozen', False):
        bundle_root = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
        bundled = bundle_root / ('FanFicFare.exe' if os.name == 'nt' else 'FanFicFare')
        if bundled.is_file():
            return [str(bundled)]

    executable = shutil.which('fanficfare') or shutil.which('fanficfare.exe')
    if executable:
        return [executable]

    if importlib.util.find_spec('fanficfare') is not None:
        # FanFicFare exposes a console entry point rather than a __main__ module.
        return [sys.executable, '-c', 'from fanficfare.cli import main; main()']

    raise RuntimeError('Fanfiction downloads need FanFicFare. Install it with: python -m pip install FanFicFare')


def _isolated_environment(profile):
    """Keep FanFicFare from implicitly reading browser cookies or user credentials."""
    profile = str(profile.resolve())
    env = os.environ.copy()
    env['HOME'] = profile
    env['USERPROFILE'] = profile
    env['XDG_CONFIG_HOME'] = str(Path(profile) / 'xdg')
    env['APPDATA'] = str(Path(profile) / 'AppData' / 'Roaming')
    env['LOCALAPPDATA'] = str(Path(profile) / 'AppData' / 'Local')
    return env


def _run_fanficfare(args, working_folder, profile):
    command = _fanficfare_command() + [
        '--non-interactive', '--format', 'epub',
        '--option', 'use_browser_cache=false',
        '--option', 'use_cloudscraper=false',
    ] + list(args)
    try:
        result = subprocess.run(command, cwd=working_folder,
                                env=_isolated_environment(profile), capture_output=True,
                                text=True, timeout=1800, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('FanFicFare did not finish within 30 minutes') from exc
    output = '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    if result.returncode:
        detail = output[-1400:] if output else f'exit code {result.returncode}'
        raise RuntimeError(f'FanFicFare failed: {detail}')
    return output


def _epub_metadata(path):
    try:
        with zipfile.ZipFile(path) as archive:
            opf_name = next((name for name in archive.namelist()
                             if name.casefold().endswith('.opf')), None)
            if not opf_name:
                return {}
            root = ET.fromstring(archive.read(opf_name))
        dc = '{http://purl.org/dc/elements/1.1/}'
        metadata = root.find('{http://www.idpf.org/2007/opf}metadata')
        if metadata is None:
            metadata = root.find('.//{http://www.idpf.org/2007/opf}metadata')
        if metadata is None:
            return {}
        title = metadata.findtext(dc + 'title')
        author = metadata.findtext(dc + 'creator')
        return {'title': title.strip() if title and title.strip() else None,
                'author': author.strip() if author and author.strip() else None}
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return {}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _story_url(item):
    url = str(item.get('story_url') or item.get('source') or '').strip()
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.netloc or
            parsed.username or parsed.password):
        raise ValueError('Fanfiction story URL must be public HTTP(S) without embedded credentials')
    return url


def download_fanfiction(db, row, root, max_bytes=MAX_FILE_BYTES, destination=None):
    """Create or update one EPUB per story, replacing it only after a valid update."""
    row = dict(row)
    item = json.loads(row['metadata'])
    story_url = _story_url(item)
    folder = Path(destination) if destination else Path(root) / 'downloads'
    folder.mkdir(parents=True, exist_ok=True)

    prior_path = Path(row['file_path']) if row.get('file_path') else None
    if prior_path is not None and prior_path.is_file():
        target = prior_path
    else:
        target = folder / file_name(item)
        if target.exists():
            key = hashlib.sha256(story_url.encode('utf-8')).hexdigest()[:10]
            target = target.with_name(f'{target.stem} [{key}]{target.suffix}')

    work_folder = target.parent
    work_folder.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix='.fanficfare-', dir=work_folder))
    profile = stage_dir / 'profile'
    profile.mkdir()
    staged = stage_dir / 'story.epub'
    existing = target.is_file()
    try:
        from .core import update_download_progress
        update_download_progress(db, row['id'], 0, None,
                                 'FanFicFare is fetching story chapters')
        if existing:
            shutil.copy2(target, staged)
            output = _run_fanficfare(['--update-epub', str(staged)], stage_dir, profile)
        else:
            args = ['--option', f'output_filename={staged}', story_url]
            output = _run_fanficfare(args, stage_dir, profile)

        if not staged.is_file():
            detail = output[-1000:] if output else 'No output was returned.'
            raise RuntimeError('FanFicFare did not create an EPUB. The site may require an account, '
                               'be unsupported, or be blocking automated access. ' + detail)
        size = staged.stat().st_size
        update_download_progress(db, row['id'], size, None,
                                 'Validating updated fanfiction EPUB')
        if size <= 0 or size > max_bytes:
            raise ValueError('Fanfiction EPUB is empty or exceeds the size limit')
        check_download(staged, '.epub')
        digest = _sha256(staged)
        archive_metadata = _epub_metadata(staged)
        for field in ('title', 'author'):
            if archive_metadata.get(field):
                item[field] = archive_metadata[field]
        if existing:
            os.replace(staged, target)
        else:
            target = folder / file_name(item)
            if target.exists():
                key = hashlib.sha256(story_url.encode('utf-8')).hexdigest()[:10]
                target = target.with_name(f'{target.stem} [{key}]{target.suffix}')
            os.replace(staged, target)
        db.execute("""UPDATE items SET metadata=?,state='downloaded',file_path=?,sha256=?,
            error=NULL,progress_bytes=NULL,progress_total=NULL,progress_message=NULL,
            updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                   (json.dumps(item, ensure_ascii=False), str(target.resolve()), digest, row['id']))
        db.commit()
        return dict(db.execute('SELECT * FROM items WHERE id=?', (row['id'],)).fetchone())
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
