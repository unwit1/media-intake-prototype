"""Kindle conversion and USB/email delivery for downloaded books."""
import datetime as dt
import base64
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import tempfile
import threading
import uuid


logger = logging.getLogger(__name__)
_DEVICE_WRITE_LOCK = threading.Lock()
_TARGET_EXTENSIONS = {'AZW3': '.azw3', 'MOBI': '.mobi'}
_EMAIL_SUPPORTED_EXTENSIONS = {'.epub', '.pdf', '.txt'}
_MAX_KINDLE_EMAIL_BYTES = 50 * 1024 * 1024
_SMTP_PASSWORD_KEY = 'kindle_smtp_password_dpapi'


def get_setting(db, key, default=''):
    row = db.execute('SELECT value FROM gui_settings WHERE key=?', (key,)).fetchone()
    return str(row['value']) if row else str(default)


def set_setting(db, key, value):
    db.execute('INSERT INTO gui_settings(key,value) VALUES(?,?) '
               'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, str(value)))
    db.commit()


def get_kindle_email_targets(db):
    """Return named Kindle email destinations, migrating the legacy single field."""
    raw = get_setting(db, 'kindle_email_targets', '')
    if raw:
        try:
            configured = json.loads(raw)
        except (TypeError, ValueError):
            configured = []
        if isinstance(configured, list):
            targets = []
            seen = set()
            for value in configured:
                if isinstance(value, str):
                    target = {'name': '', 'email': value.strip()}
                elif isinstance(value, dict):
                    target = {
                        'name': str(value.get('name') or '').strip(),
                        'email': str(value.get('email') or '').strip(),
                    }
                else:
                    continue
                if target['email'] and target['email'].casefold() not in seen:
                    targets.append(target)
                    seen.add(target['email'].casefold())
            if targets:
                return targets
    legacy = get_setting(db, 'kindle_email_to', '').strip()
    return [{'name': 'Kindle 1', 'email': legacy}] if legacy else []


def _dpapi_crypt(payload, protect, entropy=None, description='Media Intake SMTP credential'):
    """Protect a credential for the current Windows user with DPAPI."""
    if os.name != 'nt':
        raise RuntimeError('Secure SMTP password storage requires Windows DPAPI.')
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob), wintypes.LPVOID, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(DataBlob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_buffer = ctypes.create_string_buffer(payload)
    input_blob = DataBlob(len(payload), ctypes.cast(
        input_buffer, ctypes.POINTER(ctypes.c_byte)))
    entropy_bytes = entropy or b'MediaIntake/KindleSMTP/v1'
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
    entropy_blob = DataBlob(len(entropy_bytes), ctypes.cast(
        entropy_buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DataBlob()
    description = wintypes.LPWSTR()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob), description,
            ctypes.byref(entropy_blob), None, None, flags, ctypes.byref(output_blob))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), ctypes.byref(description),
            ctypes.byref(entropy_blob), None, None, flags, ctypes.byref(output_blob))
    if not ok:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))


def get_kindle_smtp_password(db):
    encrypted = get_setting(db, _SMTP_PASSWORD_KEY, '')
    if not encrypted:
        return ''
    try:
        blob = base64.urlsafe_b64decode(encrypted.encode('ascii'))
        return _dpapi_crypt(blob, protect=False).decode('utf-8')
    except Exception as exc:
        raise RuntimeError(
            'Could not unlock the saved SMTP password for this Windows user. '
            'Enter and save it again.') from exc


def set_kindle_smtp_password(db, password):
    password = str(password or '')
    if not password:
        db.execute('DELETE FROM gui_settings WHERE key=?', (_SMTP_PASSWORD_KEY,))
        db.commit()
        return
    encrypted = base64.urlsafe_b64encode(
        _dpapi_crypt(password.encode('utf-8'), protect=True)).decode('ascii')
    set_setting(db, _SMTP_PASSWORD_KEY, encrypted)


def get_local_secret(db, name):
    """Unlock a small app credential protected for this Windows user."""
    key = 'local_secret_' + str(name or '').strip()
    encrypted = get_setting(db, key, '')
    if not encrypted:
        return ''
    try:
        blob = base64.urlsafe_b64decode(encrypted.encode('ascii'))
        entropy = f'MediaIntake/LocalSecret/{name}/v1'.encode('utf-8')
        return _dpapi_crypt(blob, protect=False, entropy=entropy,
                            description='Media Intake local credential').decode('utf-8')
    except Exception as exc:
        raise RuntimeError(
            f'Could not unlock the saved {name} credential for this Windows user. '
            'Enter and save it again.') from exc


def set_local_secret(db, name, value):
    """Save a small app credential using Windows DPAPI instead of plaintext."""
    name = str(name or '').strip()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', name):
        raise ValueError('Credential name is invalid.')
    key = 'local_secret_' + name
    value = str(value or '')
    if not value:
        db.execute('DELETE FROM gui_settings WHERE key=?', (key,))
        db.commit()
        return
    entropy = f'MediaIntake/LocalSecret/{name}/v1'.encode('utf-8')
    encrypted = base64.urlsafe_b64encode(
        _dpapi_crypt(value.encode('utf-8'), protect=True, entropy=entropy,
                     description='Media Intake local credential')).decode('ascii')
    set_setting(db, key, encrypted)


def _windows_volume_labels():
    if os.name != 'nt':
        return []
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetLogicalDrives.restype = wintypes.DWORD
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    drives = kernel32.GetLogicalDrives()
    found = []
    for index in range(26):
        if not drives & (1 << index):
            continue
        root = f'{chr(ord("A") + index)}:\\'
        label = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        maximum = wintypes.DWORD()
        flags = wintypes.DWORD()
        filesystem = ctypes.create_unicode_buffer(261)
        ok = kernel32.GetVolumeInformationW(
            root, label, len(label), ctypes.byref(serial), ctypes.byref(maximum),
            ctypes.byref(flags), filesystem, len(filesystem))
        if ok:
            found.append((Path(root), label.value))
    return found


def find_kindle_documents_folder(configured_path=''):
    """Find the Kindle Documents folder, preferring an explicitly selected path."""
    configured_path = str(configured_path or '').strip()
    if configured_path:
        path = Path(configured_path).expanduser()
        if path.is_dir() and path.name.casefold() == 'documents':
            return path
        documents = path / 'documents'
        return documents if documents.is_dir() else None

    for root, label in _windows_volume_labels():
        if 'kindle' not in str(label or '').casefold():
            continue
        documents = Path(root) / 'documents'
        if documents.is_dir():
            return documents
    return None


def _find_kindling_cli(configured_path=''):
    configured_path = str(configured_path or '').strip()
    if configured_path:
        path = Path(configured_path).expanduser()
        if path.is_dir():
            path = path / ('kindling-cli.exe' if os.name == 'nt' else 'kindling-cli')
        if path.is_file() and path.name.casefold().startswith('kindling-cli'):
            return str(path)
    executable_name = 'kindling-cli.exe' if os.name == 'nt' else 'kindling-cli'
    located = shutil.which(executable_name)
    if located:
        return located
    candidates = []
    bundled_root = getattr(sys, '_MEIPASS', '')
    if bundled_root:
        candidates.append(Path(bundled_root) / 'tools' / executable_name)
    candidates.append(Path(__file__).resolve().parent.parent / 'tools' / executable_name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def _book_for_delivery(db, row, item):
    kind = str(item.get('kind') or item.get('watch_kind') or '').casefold()
    category = str(item.get('category') or '').casefold()
    watch_id = item.get('watch_request_id') or item.get('watch_id')
    if watch_id:
        watch = db.execute('SELECT kind FROM watched_items WHERE id=?',
                           (int(watch_id),)).fetchone()
        if watch:
            kind = str(watch['kind'] or '').casefold()
    kind = kind.replace(' ', '').replace('-', '')
    if kind and kind not in {'book', 'lightnovel'}:
        return False
    if category and category not in {'book', 'books', 'light novel', 'light novels'}:
        return False
    extension = Path(str(row.get('file_path') or '')).suffix.casefold()
    if extension in {'.epub', '.azw3', '.mobi', '.txt'}:
        return True
    return extension == '.pdf' and (
        kind in {'book', 'lightnovel'} or category in {'book', 'books', 'light novel', 'light novels'} or
        (not kind and not category))


def _convert_book(source, target_extension, converter_path, output):
    source = Path(source)
    if source.suffix.casefold() == target_extension:
        return source
    if source.suffix.casefold() not in {'.epub', '.opf'}:
        raise ValueError(
            'The bundled AZW3 converter requires EPUB or OPF input. '
            'This source format cannot be converted by the Kindle converter.')
    if target_extension not in {'.azw3', '.mobi'}:
        raise ValueError('Kindle output format must be AZW3 or MOBI.')
    executable = _find_kindling_cli(converter_path)
    if not executable:
        raise FileNotFoundError(
            'The bundled Kindling AZW3 converter was not found. Rebuild the portable app '
            'or choose kindling-cli.exe in Settings > Kindle.')
    output = Path(output)
    command = [executable, 'build', str(source), '--output', str(output)]
    if target_extension == '.mobi':
        command.append('--legacy-mobi')
    completed = subprocess.run(
        command, check=False, capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=900)
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        details = (completed.stderr or completed.stdout or '').strip()[-1200:]
        raise RuntimeError('Kindling could not convert this book.' +
                           (f' {details}' if details else ''))
    return output


def _delivery_filename(item, source, extension, documents):
    from .core import clean, file_name

    metadata = dict(item)
    metadata.setdefault('title', Path(source).stem)
    metadata.setdefault('author', 'Unknown')
    metadata['format'] = extension
    stem = Path(file_name(metadata)).stem
    stem = clean(stem)[:90].rstrip(' .') or 'Book'
    filename = stem + extension
    if len(str(documents / filename)) > 240:
        filename = clean(str(metadata.get('title') or 'Book'))[:65].rstrip(' .') + extension
    return filename


def _copy_verified(source, destination):
    source = Path(source)
    destination = Path(destination)
    source_hash = _digest(source)
    if destination.exists() and destination.stat().st_size == source.stat().st_size:
        if _digest(destination) == source_hash:
            return destination
    if destination.exists():
        base = destination
        destination = base.with_name(
            f'{base.stem} [{source_hash[:12]}]{base.suffix}')
        if destination.exists():
            if destination.stat().st_size == source.stat().st_size and _digest(destination) == source_hash:
                return destination
            destination = base.with_name(
                f'{base.stem} [{source_hash} {uuid.uuid4().hex[:8]}]{base.suffix}')

    partial = destination.with_name(f'.{destination.stem}.{uuid.uuid4().hex}.partial')
    try:
        shutil.copyfile(source, partial)
        if partial.stat().st_size != source.stat().st_size or _digest(partial) != source_hash:
            raise IOError('The Kindle copy did not match the converted book.')
        os.replace(partial, destination)
    finally:
        if partial.exists():
            partial.unlink()
    return destination


def _save_delivery(db, row, details):
    try:
        item = json.loads(row.get('metadata') or '{}')
    except (TypeError, ValueError):
        item = {}
    item['kindle_delivery'] = details
    db.execute('UPDATE items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (json.dumps(item, ensure_ascii=False), row['id']))
    db.commit()
    refreshed = db.execute('SELECT * FROM items WHERE id=?', (row['id'],)).fetchone()
    return dict(refreshed)


def _deliver_to_kindle_usb(db, item_id, data_root, force=False):
    """Convert and copy a downloaded book to a mounted Kindle, verifying the copy."""
    row = db.execute('SELECT * FROM items WHERE id=?', (int(item_id),)).fetchone()
    if not row or row['state'] not in {'downloaded', 'imported'} or not row['file_path']:
        raise ValueError('Download the book before sending it to a Kindle.')
    row = dict(row)
    item = json.loads(row.get('metadata') or '{}')
    if not _book_for_delivery(db, row, item):
        raise ValueError('This queue item is not a supported book file.')
    if not force and get_setting(db, 'kindle_auto_send', '0') != '1':
        return row

    target_name = get_setting(db, 'kindle_target_format', 'AZW3').upper()
    target_extension = _TARGET_EXTENSIONS.get(target_name)
    if not target_extension:
        target_name, target_extension = 'AZW3', '.azw3'
    configured_device = get_setting(db, 'kindle_device_path', '')
    documents = find_kindle_documents_folder(configured_device)
    if not documents:
        return _save_delivery(db, row, {
            'status': 'waiting_for_device', 'format': target_name,
            'updated_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        })

    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    converter_path = get_setting(db, 'kindle_converter_path', '')
    row = _save_delivery(db, row, {
        'status': 'sending', 'format': target_name,
        'device': documents.parent.name or str(documents.parent),
        'updated_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
    })
    try:
        # A Kindle exposes a removable filesystem, so serialize device writes
        # while allowing book searches and source downloads to run concurrently.
        with _DEVICE_WRITE_LOCK:
            with tempfile.TemporaryDirectory(prefix='kindle-convert-', dir=root) as temp:
                converted = Path(row['file_path'])
                if converted.suffix.casefold() != target_extension:
                    converted = _convert_book(
                        converted, target_extension, converter_path,
                        Path(temp) / ('converted' + target_extension))
                filename = _delivery_filename(item, row['file_path'], target_extension,
                                               documents)
                destination = _copy_verified(converted, documents / filename)
            details = {
                'status': 'arrived', 'format': target_name,
                'device': documents.parent.name or str(documents.parent),
                'path': str(destination.resolve()),
                'arrived_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
            }
            return _save_delivery(db, row, details)
    except Exception as exc:
        logger.exception('Kindle delivery failed for queue item %s', item_id)
        return _save_delivery(db, row, {
            'status': 'failed', 'format': target_name, 'error': str(exc)[:1200],
            'updated_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        })


def _validated_email_address(value, label):
    value = str(value or '').strip()
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError(f'Enter a valid {label} email address.')
    display, address = parseaddr(value)
    if (display or address != value or address.count('@') != 1 or
            any(char.isspace() for char in address) or address.startswith('@') or
            address.endswith('@')):
        raise ValueError(f'Enter a valid {label} email address.')
    return address


def _deliver_to_kindle_email(db, item_id, data_root, force=False,
                             targets_override=None, email_format_override=None):
    """Submit an individual copy to each configured Kindle email destination."""
    row = db.execute('SELECT * FROM items WHERE id=?', (int(item_id),)).fetchone()
    if not row or row['state'] not in {'downloaded', 'imported'} or not row['file_path']:
        raise ValueError('Download the book before sending it to a Kindle.')
    row = dict(row)
    item = json.loads(row.get('metadata') or '{}')
    if not _book_for_delivery(db, row, item):
        raise ValueError('This queue item is not a supported book file.')
    if not force and get_setting(db, 'kindle_auto_send', '0') != '1':
        return row

    targets = (list(targets_override) if targets_override is not None
               else get_kindle_email_targets(db))
    host = get_setting(db, 'kindle_smtp_host', '').strip()
    sender = get_setting(db, 'kindle_smtp_from', '').strip()
    username = get_setting(db, 'kindle_smtp_username', '').strip()
    try:
        if not targets:
            raise ValueError('Add at least one Kindle email address in Settings > Kindle.')
        targets = [
            {'name': target.get('name') or f'Kindle {index}',
             'email': _validated_email_address(target.get('email'), 'Kindle')}
            for index, target in enumerate(targets, start=1)
        ]
        sender = _validated_email_address(sender or username, 'sender')
        if not host or any(char.isspace() or ord(char) < 33 for char in host):
            raise ValueError('Enter your outgoing mail server (SMTP host).')
        port = int(get_setting(db, 'kindle_smtp_port', '587'))
        if port < 1 or port > 65535:
            raise ValueError('SMTP port must be between 1 and 65535.')
        security = get_setting(db, 'kindle_smtp_security', 'STARTTLS').upper()
        if security not in {'STARTTLS', 'SSL'}:
            raise ValueError('Choose STARTTLS or SSL for SMTP security.')
        password = get_kindle_smtp_password(db)
        if bool(username) != bool(password):
            raise ValueError('Enter both the SMTP username and password, or leave both blank.')

        source = Path(row['file_path']).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f'The downloaded library file is missing: {source}')
        email_format = str(email_format_override or
                           get_setting(db, 'kindle_email_format', 'EPUB')).upper()
        if email_format not in {'EPUB', 'AZW3'}:
            email_format = 'EPUB'
        extension = source.suffix.casefold()
        attachment_source = source
        if email_format == 'AZW3':
            if extension in {'.epub', '.opf'}:
                root = Path(data_root)
                root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='kindle-email-', dir=str(root)) as temp:
                    attachment_source = _convert_book(
                        source, '.azw3', get_setting(db, 'kindle_converter_path', ''),
                        Path(temp) / 'email-copy.azw3')
                    attachment_bytes = attachment_source.read_bytes()
            elif extension == '.azw3':
                attachment_bytes = source.read_bytes()
            else:
                raise ValueError(
                    'AZW3 email delivery needs an AZW3 file or an EPUB/OPF that Kindling can convert.')
            extension = '.azw3'
        else:
            if extension not in _EMAIL_SUPPORTED_EXTENSIONS:
                raise ValueError(
                    'EPUB email delivery accepts EPUB, PDF, or TXT files. '
                    'Choose AZW3 experimental for an AZW3 file or an EPUB/OPF conversion.')
            attachment_bytes = source.read_bytes()
        filename = _delivery_filename(item, source, extension, Path(data_root))
        mime_type, _encoding = mimetypes.guess_type(filename)
        maintype, _, subtype = (mime_type or 'application/octet-stream').partition('/')
        if len(attachment_bytes) > _MAX_KINDLE_EMAIL_BYTES:
            raise ValueError('The book exceeds Kindle email\'s 50 MB attachment limit.')

        sending = _save_delivery(db, row, {
            'status': 'sending_email', 'method': 'email',
            'recipient': targets[0]['email'],
            'format': extension.lstrip('.').upper(),
            'targets': [{'name': target['name'], 'recipient': target['email'],
                         'status': 'sending'} for target in targets],
            'updated_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        })

        def new_message(target):
            from .core import clean
            message = EmailMessage()
            message['Subject'] = 'Send to Kindle: ' + clean(item.get('title') or source.stem)
            message['From'] = sender
            message['To'] = target['email']
            message['Message-ID'] = make_msgid()
            message.set_content('Sent to your Kindle library by Media Intake.')
            message.add_attachment(attachment_bytes, maintype=maintype,
                                   subtype=subtype or 'octet-stream', filename=filename)
            if len(message.as_bytes()) > _MAX_KINDLE_EMAIL_BYTES:
                raise ValueError('The message exceeds Kindle email\'s 50 MB limit.')
            return message

        results = []
        try:
            connection = (smtplib.SMTP_SSL(host, port, timeout=40,
                                           context=ssl.create_default_context())
                          if security == 'SSL' else
                          smtplib.SMTP(host, port, timeout=40))
            with connection as server:
                server.ehlo()
                if security == 'STARTTLS':
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if username:
                    server.login(username, password)
                for target in targets:
                    message = new_message(target)
                    try:
                        refused = server.send_message(
                            message, from_addr=sender, to_addrs=[target['email']])
                        if refused:
                            raise RuntimeError('SMTP server refused this Kindle address.')
                        status = 'sent_unconfirmed'
                        error = ''
                    except Exception as exc:
                        status = 'failed'
                        error = str(exc)
                        for private_value in (target['email'], sender, username, password):
                            if private_value:
                                error = error.replace(private_value, '[redacted]')
                        error = error[:500] or type(exc).__name__
                    results.append({'name': target['name'], 'recipient': target['email'],
                                    'status': status, 'error': error,
                                    'message_id': message['Message-ID']})
        except Exception as exc:
            safe_error = str(exc)
            for private_value in [target['email'] for target in targets] + [sender, username, password]:
                if private_value:
                    safe_error = safe_error.replace(private_value, '[redacted]')
            logger.error('Kindle email delivery failed for queue item %s (%s)',
                         item_id, type(exc).__name__)
            results = [{'name': target['name'], 'recipient': target['email'],
                        'status': 'failed', 'error': safe_error[:500] or type(exc).__name__}
                       for target in targets]

        sent = sum(target['status'] == 'sent_unconfirmed' for target in results)
        failed = len(results) - sent
        status = 'sent_unconfirmed' if sent else 'failed'
        sent_at = dt.datetime.now().astimezone().isoformat(timespec='seconds')
        failed_targets = [target for target in results if target['status'] == 'failed']
        failed_names = ', '.join(target['name'] for target in failed_targets)
        error_summary = '; '.join(
            f"{target['name']}: {target['error']}" for target in failed_targets
            if target.get('error'))
        note = ('SMTP accepted the message; Kindle library delivery is not confirmed.'
                if not failed else
                f'SMTP accepted {sent} of {len(results)} Kindle email(s); failed: {failed_names}.')
        return _save_delivery(db, sending, {
            'status': status, 'method': 'email',
            'recipient': targets[0]['email'], 'format': extension.lstrip('.').upper(),
            'targets': results,
            'message_id': results[0].get('message_id') if len(results) == 1 else None,
            'sent_at': sent_at, 'note': note,
            'error': error_summary if not sent else '',
        })
    except Exception as exc:
        logger.error('Kindle email delivery failed for queue item %s (%s)',
                     item_id, type(exc).__name__)
        safe_error = str(exc)
        for private_value in [target.get('email', '') for target in targets] + [sender, username]:
            if private_value:
                safe_error = safe_error.replace(private_value, '[redacted]')
        return _save_delivery(db, row, {
            'status': 'failed', 'method': 'email', 'error': safe_error[:1200],
            'updated_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        })


def deliver_to_kindle(db, item_id, data_root, force=False):
    """Deliver through the configured USB or Send to Kindle email route."""
    if get_setting(db, 'kindle_delivery_method', 'USB').upper() == 'EMAIL':
        return _deliver_to_kindle_email(db, item_id, data_root, force=force)
    return _deliver_to_kindle_usb(db, item_id, data_root, force=force)


def maybe_auto_send_to_kindle(db, item_id, data_root):
    """Send completed downloads for a Goodreads list or global auto-delivery."""
    row = db.execute('SELECT * FROM items WHERE id=?', (int(item_id),)).fetchone()
    if not row or not row['file_path']:
        return dict(row) if row else None
    row = dict(row)
    try:
        item = json.loads(row.get('metadata') or '{}')
    except (TypeError, ValueError):
        item = {}
    if not _book_for_delivery(db, row, item):
        return row
    list_targets = item.get('kindle_email_targets_override')
    list_auto_send = bool(item.get('kindle_email_auto_send') and
                          isinstance(list_targets, list) and list_targets)
    global_auto_send = get_setting(db, 'kindle_auto_send', '0') == '1'
    if not list_auto_send and not global_auto_send:
        return row
    try:
        if list_auto_send:
            configured = get_kindle_email_targets(db)
            by_email = {str(target.get('email') or '').strip().casefold(): target
                        for target in configured if isinstance(target, dict) and
                        str(target.get('email') or '').strip()}
            selected = []
            missing = []
            for saved_target in list_targets:
                if not isinstance(saved_target, dict):
                    continue
                email = str(saved_target.get('email') or '').strip()
                current_target = by_email.get(email.casefold())
                if current_target is None:
                    missing.append(str(saved_target.get('name') or 'Selected Kindle'))
                elif not any(str(target.get('email') or '').casefold() == email.casefold()
                             for target in selected):
                    selected.append(current_target)
            if missing:
                return _save_delivery(db, row, {
                    'status': 'failed', 'method': 'email', 'format': 'EPUB',
                    'error': 'Selected Kindle destination is no longer configured: ' +
                             ', '.join(missing[:5]),
                    'updated_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                })
            if not selected:
                return _save_delivery(db, row, {
                    'status': 'failed', 'method': 'email', 'format': 'EPUB',
                    'error': 'No selected Goodreads-list Kindle destination is configured.',
                    'updated_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                })
            return _deliver_to_kindle_email(
                db, item_id, data_root, force=True, targets_override=selected,
                email_format_override='EPUB')
        return deliver_to_kindle(db, item_id, data_root, force=True)
    except Exception:
        logger.exception('Automatic Kindle delivery setup failed for queue item %s', item_id)
        return row


def pending_kindle_delivery_ids(db):
    if get_setting(db, 'kindle_auto_send', '0') != '1':
        return []
    pending = []
    for row in db.execute("SELECT id,metadata FROM items WHERE state IN ('downloaded','imported')"):
        try:
            item = json.loads(row['metadata'] or '{}')
        except (TypeError, ValueError):
            continue
        if (item.get('kindle_delivery') or {}).get('status') == 'waiting_for_device':
            pending.append(int(row['id']))
    return pending


def retry_pending_kindle_deliveries(db, data_root):
    """Retry pending auto-sends after a Kindle is connected."""
    configured_device = get_setting(db, 'kindle_device_path', '')
    if not find_kindle_documents_folder(configured_device):
        return []
    completed = []
    for item_id in pending_kindle_delivery_ids(db):
        result = deliver_to_kindle(db, item_id, data_root, force=True)
        completed.append(result)
    return completed


def email_to_kindle(db, item_id, data_root, targets):
    """Email one downloaded book to the chosen Kindle destinations, regardless of auto-send."""
    targets = [target for target in targets or [] if isinstance(target, dict) and target.get('email')]
    if not targets:
        raise ValueError('Choose a Kindle email address (Settings > Kindle).')
    return _deliver_to_kindle_email(db, item_id, data_root, force=True, targets_override=targets)
