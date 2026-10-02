"""Small IRC XDCC receiver: one requested pack, one DCC SEND, one queue item."""
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import tempfile
import time
import urllib.parse
import zipfile
from contextlib import contextmanager
from pathlib import Path
from .core import EXTENSIONS, MAX_FILE_BYTES, check_download, file_name, validate_item

_SEARCHABLE_EXTENSIONS = '|'.join(
    re.escape(extension[1:]) for extension in sorted(EXTENSIONS, key=len, reverse=True))

# Built-in ebook networks. IRCHighway serves TLS on 6697 with a certificate that
# does not verify; Undernet offers no TLS at all, so #bookz uses plain 6667.
EBOOK_NETWORK_PRESETS = (
    {'preset': 'irchighway-ebooks', 'name': 'IRCHighway #ebooks', 'server': 'irc.irchighway.net',
     'port': 6697, 'tls': True, 'verify_tls': False, 'channel': '#ebooks'},
    {'preset': 'undernet-bookz', 'name': 'Undernet #bookz', 'server': 'irc.undernet.org',
     'port': 6667, 'tls': False, 'verify_tls': True, 'channel': '#bookz'},
)
EBOOK_PRESET_CATEGORIES = ('Books', 'Light novels')
_NO_RESULTS = re.compile(r'no (?:matches|results)|returned (?:0|no)\b|nothing (?:was )?found|not found', re.I)
_NICK_IN_USE = re.compile(r'\s433\s')


class IrcNoResults(LookupError):
    """The channel's search bot reported that nothing matched."""


def preset_nick(seed=None):
    """A per-install nickname, so the app does not collide with other readers."""
    digest = hashlib.sha256(str(seed or os.environ.get('COMPUTERNAME') or os.getpid()).encode()).hexdigest()
    return 'MIReader' + str(int(digest[:6], 16) % 10000).zfill(4)


def ensure_ebook_presets(sources, applied, nick=None):
    """Add missing IRCHighway/Undernet sources once; returns (sources, applied_presets, added).

    A preset the user removed stays removed: `applied` remembers what was offered.
    """
    import uuid
    sources = [dict(source) for source in sources or [] if isinstance(source, dict)]
    applied = list(applied or [])
    added = []
    for preset in EBOOK_NETWORK_PRESETS:
        if preset['preset'] in applied:
            continue
        applied.append(preset['preset'])
        endpoint = (preset['server'].casefold(), preset['channel'].casefold())
        existing = next((source for source in sources
                         if (str(source.get('server', '')).casefold(), str(source.get('channel', '')).casefold()) == endpoint), None)
        if existing:
            # Correct settings an older manual entry may have missed (for example TLS verification).
            for key in ('port', 'tls', 'verify_tls'):
                existing.setdefault(key, preset[key])
            if existing['server'].casefold() == 'irc.irchighway.net' and existing.get('tls', True):
                existing['verify_tls'] = False
            continue
        source = {key: value for key, value in preset.items() if key != 'preset'}
        source.update(id=uuid.uuid4().hex, nick=nick or preset_nick(), timeout=90,
                      categories=list(EBOOK_PRESET_CATEGORIES), enabled=True, preset=preset['preset'])
        sources.append(source)
        added.append(source['name'])
    return sources, applied, added


def _irc_ssl_context(config):
    if config.get('verify_tls', True):
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def enqueue_irc(db, config, watch_id=None):
    config = dict(config)
    for key in ('server', 'channel', 'bot', 'title', 'format'):
        if not config.get(key): raise ValueError(f'Missing IRC {key}')
    if bool(config.get('pack')) == bool(config.get('request')):
        raise ValueError('Set exactly one of IRC pack or request')
    for key in ('server', 'channel', 'bot', 'nick', 'pack', 'request'):
        if '\n' in str(config.get(key, '')) or '\r' in str(config.get(key, '')):
            raise ValueError('Newline in IRC field')
    if not re.fullmatch(r'#[\w\-\[\]{}^|`]+', config['channel']):
        raise ValueError('Invalid IRC channel')
    if not re.fullmatch(r'[\w\-\[\]{}^|`]+', config['bot']):
        raise ValueError('Invalid IRC bot')
    if config.get('pack') and not re.fullmatch(r'\d{1,8}', str(config['pack'])):
        raise ValueError('IRC pack must be numeric')
    if config.get('request') and (not re.fullmatch(r'![\w\-\[\]{}^|`]+ [^\x00-\x1f]{1,350}', config['request'])
                                  or config['request'].split(' ', 1)[0][1:].casefold() != config['bot'].casefold()):
        raise ValueError('IRC request must start with !bot and contain one filename')
    from .core import enqueue, enqueue_for_watch
    token = str(config.get('pack') or config['request'])
    url = 'irc+xdcc://' + config['server'].lower() + '/' + urllib.parse.quote(config['channel'], safe='') + '/' + config['bot'].lower() + '/' + urllib.parse.quote(token, safe='')
    item = {'title': config['title'], 'author': config.get('author', 'Unknown'),
            'format': config['format'], 'url': url,
            'source': 'irc://' + config['server'] + '/' + config['channel'],
            'series': config.get('series'), 'series_index': config.get('series_index'),
            'source_key': config.get('source_key') or 'irc', 'irc': config}
    for key in ('kindle_email_auto_send', 'kindle_email_targets_override',
                'library_profile_ids', 'book_lists', 'goodreads_lists'):
        if config.get(key):
            item[key] = config[key]
    for key in ('rights_evidence_path', 'rights_work_key'):
        if config.get(key):
            item[key] = config[key]
    return enqueue_for_watch(db, item, watch_id) if watch_id is not None else enqueue(db, item)


def parse_offer(line, bot, max_bytes=MAX_FILE_BYTES):
    # Only take the offer from the requested bot. Never use the advertised filename as a path.
    if not line.startswith(':') or ' PRIVMSG ' not in line: return None
    sender = line[1:].split('!', 1)[0]
    if bot and sender.casefold() != bot.casefold(): return None
    match = re.search(r'\x01DCC SEND (.+?)\x01', line)
    if not match: return None
    parts = match.group(1).rsplit(' ', 3)
    if len(parts) != 4: raise ValueError('Malformed DCC SEND')
    filename, host, port, size = parts
    filename = filename.strip('"')
    port, size = int(port), int(size)
    if not (0 < port < 65536 and 0 < size <= max_bytes):
        raise ValueError('Invalid DCC port or size')
    if host.isdecimal():
        number = int(host)
        if not 0 <= number <= 0xffffffff: raise ValueError('Invalid DCC IPv4 address')
        host = str(ipaddress.IPv4Address(number))
    else:
        host = str(ipaddress.ip_address(host))
    return {'filename': filename, 'host': host, 'port': port, 'size': size, 'sender': sender}


def send_line(sock, line):
    if '\r' in line or '\n' in line: raise ValueError('Invalid IRC command')
    sock.sendall((line + '\r\n').encode('utf-8'))


def discover_irc_channels(config, limit=500, timeout=25):
    """Connect to an IRC server and return channels from its standard LIST reply."""
    config = dict(config or {})
    server = str(config.get('server') or '').strip()
    nick = str(config.get('nick') or 'MediaIntake').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*[A-Za-z0-9]', server) or '..' in server:
        raise ValueError('Enter a valid IRC server hostname.')
    if not re.fullmatch(r'[\w\-\[\]{}^|`]{1,30}', nick):
        raise ValueError('Enter a valid IRC nickname.')
    try:
        port = int(config.get('port', 6697 if config.get('tls', True) else 6667))
    except (TypeError, ValueError) as exc:
        raise ValueError('IRC port must be a number from 1 to 65535.') from exc
    if not 1 <= port <= 65535:
        raise ValueError('IRC port must be a number from 1 to 65535.')
    if not 1 <= int(limit) <= 2000:
        raise ValueError('IRC channel list limit must be between 1 and 2000.')
    channel_mask = str(config.get('channel_mask') or '').strip()
    if channel_mask and (len(channel_mask) > 300 or not re.fullmatch(
            r'#[\w\-\[\]{}^|`*?]+(?:,#[\w\-\[\]{}^|`*?]+)*', channel_mask)):
        raise ValueError('IRC channel listing filter must contain comma-separated channel masks.')
    timeout = max(3, min(int(timeout), 90))
    channels = {}
    password_env = str(config.get('password_env') or '').strip()
    password = os.environ.get(password_env) if password_env else None
    if password_env and not password:
        raise ValueError(f'IRC password variable {password_env} is unset.')

    plain = socket.create_connection((server, port), timeout=min(timeout, 20))
    sock = None
    try:
        sock = (_irc_ssl_context(config).wrap_socket(plain, server_hostname=server)
                if config.get('tls', True) else plain)
        sock.settimeout(timeout)
        if password:
            send_line(sock, 'PASS ' + password)
        send_line(sock, 'NICK ' + nick)
        send_line(sock, 'USER ' + nick + ' 0 * :Media Intake')
        deadline = time.monotonic() + timeout
        requested = False
        with sock.makefile('rb') as reader:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('IRC server did not finish listing channels before timeout.')
                sock.settimeout(remaining)
                raw = reader.readline(4096)
                if not raw:
                    raise ConnectionError('IRC server closed the connection before channel listing finished.')
                line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                if line.startswith('PING '):
                    send_line(sock, 'PONG ' + line[5:])
                    continue
                if re.search(r'\s(?:432|433|464)\s', line):
                    raise ConnectionError('IRC registration was rejected: ' + line[:250])
                if re.search(r'\s001\s', line) and not requested:
                    send_line(sock, 'LIST' + (' ' + channel_mask if channel_mask else ''))
                    requested = True
                    continue
                if re.search(r'\s421\s[^ ]+\sLIST(?:\s|$)', line, re.I):
                    raise ConnectionError('This IRC server does not allow the standard LIST command.')
                if re.search(r'\s323\s', line) and requested:
                    break
                if not requested or not re.search(r'\s322\s', line):
                    continue
                parts = line.split(' ', 4)
                if len(parts) < 4:
                    continue
                channel = parts[3]
                if not re.fullmatch(r'#[\w\-\[\]{}^|`]+', channel):
                    continue
                user_count, topic = 0, ''
                if len(parts) == 5:
                    details = parts[4]
                    user_text, separator, topic_text = details.partition(' :')
                    try:
                        user_count = int(user_text.strip())
                    except ValueError:
                        pass
                    topic = topic_text.strip() if separator else ''
                channels[channel.casefold()] = {
                    'channel': channel, 'users': user_count, 'topic': topic[:240],
                }
                if len(channels) >= limit:
                    raise ValueError(f'The server returned more than {limit} channels; narrow its LIST first.')
    except (socket.timeout, TimeoutError) as exc:
        raise TimeoutError('Timed out connecting to the IRC server or listing its channels.') from exc
    finally:
        (sock or plain).close()
    return sorted(channels.values(), key=lambda item: item['channel'].casefold())


def send_user_command(sock, command, config):
    command = command.replace('{bot}', config.get('bot') or '').replace('{pack}', str(config.get('pack', ''))).replace('{channel}', config['channel']).replace('{request}', config.get('request', ''))
    if '\r' in command or '\n' in command: raise ValueError('Invalid IRC command')
    pieces = command.split(' ', 2)
    verb = pieces[0].casefold()
    if verb == '/join' and len(pieces) >= 2:
        send_line(sock, 'JOIN ' + pieces[1])
    elif verb in ('/msg', '/notice') and len(pieces) == 3:
        send_line(sock, ('PRIVMSG' if verb == '/msg' else 'NOTICE') + ' ' + pieces[1] + ' :' + pieces[2])
    else:
        raise ValueError('Supported IRC commands: /join, /msg, /notice')


@contextmanager
def receive_offer(config, max_bytes=MAX_FILE_BYTES):
    nick = config.get('nick', 'MediaIntake')
    server = config['server']
    port = int(config.get('port', 6697 if config.get('tls', True) else 6667))
    with socket.create_connection((server, port), timeout=25) as plain:
        plain.settimeout(int(config.get('timeout', 180)))
        sock = _irc_ssl_context(config).wrap_socket(plain, server_hostname=server) if config.get('tls', True) else plain
        with sock:
            password_env = config.get('password_env')
            if password_env:
                password = os.environ.get(password_env)
                if not password: raise ValueError(f'IRC password variable {password_env} is unset')
                send_line(sock, 'PASS ' + password)
            send_line(sock, 'NICK ' + nick)
            send_line(sock, 'USER ' + nick + ' 0 * :Media Intake')
            sent_join, sent_request = False, False
            nick_attempts = 0
            with sock.makefile('rb') as reader:
                while True:
                    raw = reader.readline(4096)
                    if not raw: raise ConnectionError('IRC connection closed before DCC SEND')
                    line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                    if line.startswith('PING '): send_line(sock, 'PONG ' + line[5:]); continue
                    if _NICK_IN_USE.search(line) and not sent_join and nick_attempts < 3:
                        nick_attempts += 1
                        nick = f"{config.get('nick', 'MediaIntake')[:12]}{int(time.time() * 1000) % 1000:03d}"
                        send_line(sock, 'NICK ' + nick)
                        continue
                    if re.search(r'\s(?:403|432|433|464|471|473|474|475|477)\s', line):
                        raise ConnectionError('IRC registration or channel rejected: ' + line[:250])
                    if (sent_request and config.get('_search') and re.search(r'\s(?:NOTICE|PRIVMSG)\s' + re.escape(nick) + r'\s', line, re.I)
                            and _NO_RESULTS.search(line.split(' :', 1)[-1])):
                        raise IrcNoResults(line.split(' :', 1)[-1][:250])
                    if re.search(r'\s001\s', line) and not sent_join:
                        send_line(sock, 'JOIN ' + config['channel']); sent_join = True
                    if (re.search(r'\s366\s', line) or re.search(r'^:' + re.escape(nick) + r'!', line, re.I) and ' JOIN ' in line) and sent_join and not sent_request:
                        commands = config.get('commands') or (['/msg {channel} {request}'] if config.get('request') else ['/msg {bot} xdcc send #{pack}'])
                        if not isinstance(commands, list) or not commands or len(commands) > 10:
                            raise ValueError('IRC commands must be a list of 1 to 10 strings')
                        for command in commands:
                            send_user_command(sock, command, config)
                        sent_request = True
                    if sent_request:
                        offer = parse_offer(line, config.get('bot'), max_bytes)
                        if offer:
                            yield offer
                            return


def download_irc(db, row, root, max_bytes=MAX_FILE_BYTES, destination=None):
    item = validate_item(json.loads(row['metadata']))
    config = item['irc']
    folder = Path(destination) if destination else Path(root) / 'downloads'
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.partial-', dir=folder)
    digest, received = hashlib.sha256(), 0
    last_progress = 0
    try:
        from .core import update_download_progress
        update_download_progress(db, row['id'], 0, None, 'Connecting to IRC and waiting for a file offer')
        with os.fdopen(fd, 'wb') as out, receive_offer(config, max_bytes) as offer:
            advertised_ext = Path(offer['filename']).suffix.lower()
            if advertised_ext != item['format']:
                raise ValueError(f'DCC file format {advertised_ext} does not match requested {item["format"]}')
            with socket.create_connection((offer['host'], offer['port']), timeout=30) as dcc:
                dcc.settimeout(int(config.get('timeout', 180)))
                while received < offer['size']:
                    chunk = dcc.recv(min(65536, offer['size'] - received))
                    if not chunk: raise ConnectionError('DCC transfer ended early')
                    out.write(chunk); digest.update(chunk); received += len(chunk)
                    dcc.sendall(struct.pack('!I', received & 0xffffffff))
                    if received - last_progress >= 1024 * 1024 or received >= offer['size']:
                        update_download_progress(db, row['id'], received, offer['size'], 'Receiving DCC file')
                        last_progress = received
        check_download(tmp, item['format'])
        base = folder / file_name(item)
        destination = base
        if base.exists():
            if hashlib.sha256(base.read_bytes()).hexdigest() == digest.hexdigest():
                os.unlink(tmp)
            else:
                destination = base.with_name(base.stem + ' [' + digest.hexdigest()[:8] + ']' + base.suffix)
                os.replace(tmp, destination)
        else:
            os.replace(tmp, destination)
        db.execute("UPDATE items SET state='downloaded', file_path=?, sha256=?, error=NULL, progress_bytes=NULL, progress_total=NULL, progress_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(destination.resolve()), digest.hexdigest(), row['id']))
        db.commit()
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return dict(db.execute('SELECT * FROM items WHERE id=?', (row['id'],)).fetchone())


def has_configured_source(source_options, category=None):
    """Return whether IRC has a configured source applicable to a category."""
    options = source_options or {}
    for source in options.get('irc_sources') or ():
        if not isinstance(source, dict) or source.get('enabled', True) is False:
            continue
        categories = source.get('categories') or ()
        if category is None or category in categories:
            return True
    legacy_configs = options.get('irc_configs') or (
        [options['irc_config']] if options.get('irc_config') else [])
    if isinstance(legacy_configs, (str, Path, dict)):
        legacy_configs = [legacy_configs]
    for config in legacy_configs:
        if isinstance(config, dict) and config.get('server') and config.get('channel'):
            return True
        try:
            if Path(config).is_file():
                return True
        except (TypeError, OSError):
            continue
    return False


def irc_source_priority_label(name, config=None):
    """Return the label used for one configured IRC server in download priorities."""
    config = config if isinstance(config, dict) else {}
    name = str(name or config.get('name') or config.get('server') or 'IRC source').strip()
    server = str(config.get('server') or '').strip()
    channel = str(config.get('channel') or '').strip()
    endpoint = ' '.join(value for value in (server, channel) if value)
    return f'IRC: {name} ({endpoint})' if endpoint else f'IRC: {name}'


def configured_search_sources(source_options, category=None, priority=None):
    """Build the ordered IRC source list, with legacy JSON files as fallbacks."""
    options = source_options or {}
    sources = []
    for source in options.get('irc_sources') or ():
        if not isinstance(source, dict) or source.get('enabled', True) is False:
            continue
        if category is not None and category not in (source.get('categories') or ()):
            continue
        config = {key: value for key, value in source.items()
                  if key not in {'id', 'name', 'categories', 'enabled'}}
        sources.append((source.get('name') or source.get('server') or 'IRC source', config))

    legacy_configs = options.get('irc_configs') or (
        [options['irc_config']] if options.get('irc_config') else [])
    if isinstance(legacy_configs, (str, Path, dict)):
        legacy_configs = [legacy_configs]
    for config_path in legacy_configs:
        if isinstance(config_path, dict):
            sources.append((config_path.get('name') or config_path.get('server') or 'IRC config',
                            dict(config_path)))
            continue
        try:
            config = json.loads(Path(config_path).read_text(encoding='utf-8'))
            sources.append((Path(config_path).stem, config))
        except Exception as exc:
            sources.append((str(config_path), exc))
    if priority:
        ranks = {str(label).casefold(): index for index, label in enumerate(priority)}
        indexed = list(enumerate(sources))
        indexed.sort(key=lambda pair: (
            ranks.get(irc_source_priority_label(pair[1][0], pair[1][1]).casefold(),
                      len(ranks) + pair[0]),
            pair[0]))
        sources = [source for _index, source in indexed]
    return sources


def search_configured_sources(query, source_options, category, allowed_formats, limit=100,
                              priority=None, progress_callback=None):
    """Search IRC sources in priority order and stop at the first relevant result set."""
    results, errors = [], []
    for name, config_or_error in configured_search_sources(
              source_options, category, priority=priority):
        if progress_callback:
            try:
                progress_callback(irc_source_priority_label(name, config_or_error)
                                  if not isinstance(config_or_error, Exception)
                                  else f'IRC {name}')
            except Exception:
                pass
        if isinstance(config_or_error, Exception):
            errors.append(f'{name}: {config_or_error}')
            continue
        try:
            found = [item for item in search_irc(query, config_or_error, limit=limit)
                     if str(item.get('format') or '').casefold() in allowed_formats]
            if not found:
                continue
            for item in found:
                item['irc_source_name'] = name
                item['download_priority_label'] = irc_source_priority_label(
                    name, config_or_error)
                if isinstance(item.get('irc'), dict):
                    item['irc']['source_name'] = name
            return found, errors
        except Exception as exc:
            errors.append(f'{name}: {exc}')
    return results, errors


class _ExitOnly:
    """Close an already-entered context manager at the end of a with block."""

    def __init__(self, context):
        self.context = context

    def __enter__(self):
        return self.context

    def __exit__(self, *exc):
        return self.context.__exit__(*exc)


def parse_search_index(data, config, limit=100):
    """Read a search bot's ZIP (or plain-text) listing without extracting untrusted archive paths."""
    if len(data) > 5_000_000: raise ValueError('IRC search index exceeds 5 MB')
    if not data.startswith(b'PK'):
        return parse_search_text(data.decode('utf-8-sig', 'replace'), config, limit)
    entries = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 20: raise ValueError('Too many files in IRC index')
        for member in archive.infolist():
            if not member.filename.lower().endswith(('.txt', '.nfo')) or member.file_size > 5_000_000: continue
            with archive.open(member) as stream:
                content = stream.read(5_000_001)
            if len(content) > 5_000_000: raise ValueError('IRC index expanded beyond 5 MB')
            entries.extend(parse_search_text(content.decode('utf-8-sig', 'replace'), config, limit - len(entries)))
            if len(entries) >= limit: return entries
    return entries


def parse_search_text(text, config, limit=100):
    """Turn search-result lines ("!Bot Author - Title.ext ::INFO:: size") into queueable entries."""
    entries = []
    if limit <= 0:
        return entries
    for line in str(text).splitlines():
        match = re.search(
            rf'(?<!\S)!([\w\-\[\]{{}}^|`]+)\s+(.+?\.(?:{_SEARCHABLE_EXTENSIONS}))'
            r'(?=\s*::\s*INFO\s*::|\s*$)', line, re.I)
        if not match:
            continue
        bot, filename = match.group(1), match.group(2).strip()
        if '\x00' in filename or len(filename) > 320:
            continue
        ext = Path(filename).suffix.lower()
        name = Path(filename).name.removesuffix(ext).replace('_', ' ').strip()
        author, sep, title = name.partition(' - ')
        if not sep:
            title, author = name, 'Unknown'
        info = re.search(r'::\s*INFO\s*::\s*(.+)$', line, re.I)
        request = '!' + bot + ' ' + filename
        item_config = {k: config[k] for k in (
            'server', 'port', 'tls', 'verify_tls', 'nick', 'channel',
            'password_env', 'timeout') if k in config}
        item_config.update(bot=bot, request=request, title=title, author=author, format=ext)
        entries.append({'title': title, 'author': author, 'format': ext,
                        'source': 'irc://' + config['server'] + '/' + config['channel'],
                        'irc': item_config, 'filename': filename, 'bot': bot,
                        'size_text': info.group(1).strip() if info else ''})
        if len(entries) >= limit:
            return entries
    return entries

def search_irc(query, config, limit=100):
    """Search a channel and receive its DCC ZIP index, then return selectable entries."""
    if not 1 <= limit <= 500: raise ValueError('limit must be between 1 and 500')
    if not 1 <= len(query) <= 150 or any(ord(c) < 32 for c in query):
        raise ValueError('Invalid IRC search query')
    config = dict(config)
    if not config.get('server') or not re.fullmatch(r'#[\w\-\[\]{}^|`]+', config.get('channel', '')):
        raise ValueError('IRC search needs a server and channel')
    config['commands'] = ['/msg {channel} @search ' + query]
    # Search index senders vary by network. Accept only a ZIP from the joined channel session.
    config['bot'] = None
    config['_search'] = True
    try:
        offer_context = receive_offer(config, max_bytes=5_000_000)
        offer = offer_context.__enter__()
    except IrcNoResults:
        return []
    with _ExitOnly(offer_context):
        if Path(offer['filename']).suffix.lower() != '.zip':
            raise ValueError('IRC search response was not a ZIP index')
        buf = io.BytesIO()
        with socket.create_connection((offer['host'], offer['port']), timeout=30) as dcc:
            dcc.settimeout(int(config.get('timeout', 180)))
            while buf.tell() < offer['size']:
                chunk = dcc.recv(min(65536, offer['size'] - buf.tell()))
                if not chunk: raise ConnectionError('IRC search index transfer ended early')
                buf.write(chunk)
                dcc.sendall(struct.pack('!I', buf.tell() & 0xffffffff))
    return parse_search_index(buf.getvalue(), config, limit)


def search_all_sources(query, source_options, category=None, allowed_formats=None, limit=200, workers=4):
    """Search every configured IRC source at once; returns (results, errors) across all of them."""
    from concurrent.futures import ThreadPoolExecutor
    sources = [(name, config) for name, config in configured_search_sources(source_options, category)
               if not isinstance(config, Exception)]
    results, errors = [], []

    def one(source):
        name, config = source
        try:
            return name, config, search_irc(query, config, limit=limit), None
        except Exception as exc:
            return name, config, [], exc

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(sources) or 1))) as pool:
        for name, config, found, error in pool.map(one, sources):
            if error is not None:
                errors.append(f'{name}: {error}')
                continue
            for item in found:
                if allowed_formats and str(item.get('format') or '').casefold() not in allowed_formats:
                    continue
                item['irc_source_name'] = name
                item['download_priority_label'] = irc_source_priority_label(name, config)
                if isinstance(item.get('irc'), dict):
                    item['irc']['source_name'] = name
                results.append(item)
    return results, errors


def receive_dcc_file(offer, folder, max_bytes=MAX_FILE_BYTES, timeout=180, progress=None):
    """Receive an accepted DCC SEND into `folder` under a sanitized name; returns the path."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', Path(offer['filename']).name).strip(' .')[:200] or 'download'
    if offer['size'] > max_bytes:
        raise ValueError('Offered file is larger than the download limit')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.partial-irc-', dir=folder)
    received = 0
    try:
        with os.fdopen(fd, 'wb') as out, socket.create_connection((offer['host'], offer['port']), timeout=30) as dcc:
            dcc.settimeout(timeout)
            while received < offer['size']:
                chunk = dcc.recv(min(65536, offer['size'] - received))
                if not chunk:
                    raise ConnectionError('DCC transfer ended early')
                out.write(chunk)
                received += len(chunk)
                dcc.sendall(struct.pack('!I', received & 0xffffffff))
                if progress:
                    progress(received, offer['size'])
        target = folder / name
        if target.exists():
            target = target.with_name(f'{target.stem} [{int(time.time())}]{target.suffix}')
        os.replace(tmp, target)
        return target
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
