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
import urllib.parse
import zipfile
from contextlib import contextmanager
from pathlib import Path
from .core import MAX_FILE_BYTES, check_download, file_name, validate_item


def enqueue_irc(db, config):
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
    from .core import enqueue
    token = str(config.get('pack') or config['request'])
    url = 'irc+xdcc://' + config['server'].lower() + '/' + urllib.parse.quote(config['channel'], safe='') + '/' + config['bot'].lower() + '/' + urllib.parse.quote(token, safe='')
    return enqueue(db, {'title': config['title'], 'author': config.get('author','Unknown'),
                        'format': config['format'], 'url': url, 'source': 'irc://' + config['server'] + '/' + config['channel'],
                        'series': config.get('series'), 'series_index': config.get('series_index'),
                        'irc': config})


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
        sock = ssl.create_default_context().wrap_socket(plain, server_hostname=server) if config.get('tls', True) else plain
        with sock:
            password_env = config.get('password_env')
            if password_env:
                password = os.environ.get(password_env)
                if not password: raise ValueError(f'IRC password variable {password_env} is unset')
                send_line(sock, 'PASS ' + password)
            send_line(sock, 'NICK ' + nick)
            send_line(sock, 'USER ' + nick + ' 0 * :Media Intake')
            sent_join, sent_request = False, False
            with sock.makefile('rb') as reader:
                while True:
                    raw = reader.readline(4096)
                    if not raw: raise ConnectionError('IRC connection closed before DCC SEND')
                    line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                    if line.startswith('PING '): send_line(sock, 'PONG ' + line[5:]); continue
                    if re.search(r'\s(?:433|432|464|475|474)\s', line): raise ConnectionError('IRC registration or channel rejected: ' + line[:250])
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
    try:
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
        db.execute("UPDATE items SET state='downloaded', file_path=?, sha256=?, error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(destination.resolve()), digest.hexdigest(), row['id']))
        db.commit()
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return dict(db.execute('SELECT * FROM items WHERE id=?', (row['id'],)).fetchone())


def parse_search_index(data, config, limit=100):
    """Read a search bot's ZIP listing without extracting untrusted archive paths."""
    if len(data) > 5_000_000: raise ValueError('IRC search index exceeds 5 MB')
    entries = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 20: raise ValueError('Too many files in IRC index')
        for member in archive.infolist():
            if not member.filename.lower().endswith(('.txt', '.nfo')) or member.file_size > 5_000_000: continue
            with archive.open(member) as stream:
                content = stream.read(5_000_001)
            if len(content) > 5_000_000: raise ValueError('IRC index expanded beyond 5 MB')
            for line in content.decode('utf-8-sig', 'replace').splitlines():
                match = re.search(r'(?<!\S)!([\w\-\[\]{}^|`]+)\s+(.+?\.(?:epub|pdf|mobi|azw3|cbz))(?=\s*::\s*INFO\s*::|\s*$)', line, re.I)
                if not match: continue
                bot, filename = match.group(1), match.group(2).strip()
                if '\x00' in filename or len(filename) > 320: continue
                ext = Path(filename).suffix.lower()
                name = Path(filename).name.removesuffix(ext).replace('_', ' ').strip()
                author, sep, title = name.partition(' - ')
                if not sep: title, author = name, 'Unknown'
                request = '!' + bot + ' ' + filename
                item_config = {k: config[k] for k in ('server', 'port', 'tls', 'nick', 'channel', 'password_env', 'timeout') if k in config}
                item_config.update(bot=bot, request=request, title=title, author=author, format=ext)
                entries.append({'title': title, 'author': author, 'format': ext,
                                'source': 'irc://' + config['server'] + '/' + config['channel'],
                                'irc': item_config, 'filename': filename})
                if len(entries) >= limit: return entries
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
    with receive_offer(config, max_bytes=5_000_000) as offer:
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
