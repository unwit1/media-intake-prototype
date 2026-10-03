"""User-operated WebView2 shortlink helper (browser fallback for shortlinks.py).

Runs the vendored "Bypass All Shortlinks Debloated" userscript (data/shortlinks)
on shortener pages and hands the final file-host or file URL back to the app as
a small JSON capture. The user solves any CAPTCHA; the script's CAPTCHA
automation settings (Audio, hCaptcha) are locked off.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import urllib.error
import urllib.parse
import urllib.request

CAPTURE_PREFIX = 'shortlink-'
GM_XHR_MAX_BYTES = 2_000_000

_GM_SHIM = r'''(() => {
  if (window.__mediaIntakeShortlinkShim) return;
  window.__mediaIntakeShortlinkShim = true;
  const prefix = "__media_intake_gm_";
  const locked = {Audio: false, hCaptcha: false};
  const read = (key, fallback) => {
    try {
      const raw = localStorage.getItem(prefix + key);
      let value = raw === null ? fallback : JSON.parse(raw);
      if (String(key).startsWith("_MonkeyConfig_") && typeof value === "string") {
        value = JSON.stringify(Object.assign(JSON.parse(value), locked));
      }
      return value;
    } catch (error) { return fallback; }
  };
  window.unsafeWindow = window;
  window.GM_info = {script: {name: "Bypass All Shortlinks Debloated", version: __VERSION__}, scriptHandler: "Media Intake"};
  window.GM_getMetadata = key => key === "name" ? GM_info.script.name : (key === "version" ? GM_info.script.version : "");
  window.GM_getValue = read;
  window.GM_setValue = (key, value) => { try { localStorage.setItem(prefix + key, JSON.stringify(value)); } catch (error) {} };
  window.GM_deleteValue = key => { try { localStorage.removeItem(prefix + key); } catch (error) {} };
  window.GM_addStyle = css => { const style = document.createElement("style"); style.textContent = css; (document.head || document.documentElement).appendChild(style); return style; };
  window.GM_openInTab = url => { location.href = String(url); return {close() {}}; };
  window.GM_setClipboard = text => { try { navigator.clipboard.writeText(String(text)); } catch (error) {} };
  window.GM_registerMenuCommand = (name, callback) => {
    if (!/settings/i.test(String(name)) || document.getElementById("media-intake-shortlink-settings")) return;
    const button = document.createElement("button");
    button.id = "media-intake-shortlink-settings";
    button.textContent = "Bypass settings";
    button.setAttribute("style", "position:fixed;z-index:2147483647;left:18px;bottom:18px;padding:8px 11px;border:0;border-radius:8px;background:#334155;color:white;font:600 12px sans-serif;cursor:pointer");
    button.addEventListener("click", () => callback());
    (document.body || document.documentElement).appendChild(button);
  };
  window.GM_xmlhttpRequest = details => {
    const request = {method: details.method || "GET", url: String(details.url), headers: details.headers || {},
                     data: details.data == null ? null : String(details.data)};
    window.pywebview.api.gm_xhr(request).then(reply => {
      let response = reply.responseText;
      if (details.responseType === "json") { try { response = JSON.parse(reply.responseText); } catch (error) { response = null; } }
      const result = Object.assign({}, reply, {response, readyState: 4});
      if (details.onload) details.onload(result);
      if (details.onloadend) details.onloadend(result);
    }).catch(error => { if (details.onerror) details.onerror({error: String(error)}); });
    return {abort() {}};
  };
})();
'''

_SEND_BUTTON = r'''(() => {
  if (document.getElementById("media-intake-shortlink-send")) return;
  const button = document.createElement("button");
  button.id = "media-intake-shortlink-send";
  button.textContent = __AUTOMATIC__ ? "Sent to Media Intake" : "Use this page as the download";
  button.setAttribute("style", "position:fixed;z-index:2147483647;right:18px;bottom:18px;padding:11px 15px;border:0;border-radius:8px;background:#2563eb;color:white;font:600 14px sans-serif;box-shadow:0 2px 10px #0008;cursor:pointer");
  button.addEventListener("click", async () => {
    try { button.textContent = await window.pywebview.api.capture(location.href); }
    catch (error) { button.textContent = "Could not send"; button.title = String(error); }
  });
  (document.body || document.documentElement).appendChild(button);
})();'''


def gm_shim(version):
    return _GM_SHIM.replace('__VERSION__', json.dumps(str(version)))


def send_button_script(automatic):
    return _SEND_BUTTON.replace('__AUTOMATIC__', 'true' if automatic else 'false')


def userscript_payload():
    """One payload so MonkeyConfig and the GM functions share the script's scope,
    however the host wraps evaluate_js."""
    from .shortlinks import DATA, USERSCRIPT, source_info
    config = (DATA / 'MonkeyConfig-Mod.js').read_text(encoding='utf-8')
    script = (DATA / USERSCRIPT).read_text(encoding='utf-8')
    return ('try {\n' + gm_shim(source_info().get('userscript_version', '')) + '\n' + config
            + '\nwindow.MonkeyConfig = MonkeyConfig;\n' + script
            + '\n} catch (error) { console.error("Shortlink userscript failed", error); }\nnull;')


def _public_web_url(url):
    parts = urllib.parse.urlsplit(str(url or ''))
    return parts.scheme in ('http', 'https') and bool(parts.hostname) and not parts.username and not parts.password


class ShortlinkActions:
    """Python side of the window's js_api."""

    def __init__(self, source_url, capture_folder, opener=None):
        self.source_url = source_url
        self.capture_folder = Path(capture_folder)
        self.captured = set()
        self.opener = opener or urllib.request.urlopen

    def capture(self, url):
        if not _public_web_url(url):
            raise ValueError('Only a public web address can be sent')
        if url in self.captured:
            return 'Already sent to Media Intake'
        self.captured.add(url)
        self.capture_folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        target = self.capture_folder / f'{CAPTURE_PREFIX}{stamp}-{secrets.token_hex(3)}.json'
        temporary = target.with_suffix('.json.tmp')
        temporary.write_text(json.dumps({'source_url': self.source_url, 'final_url': url,
                                         'captured_at': datetime.now(timezone.utc).isoformat()}),
                             encoding='utf-8')
        temporary.replace(target)
        return 'Sent to Media Intake'

    def gm_xhr(self, request):
        """GM_xmlhttpRequest: public HTTP(S) only, no cookies or credentials, bounded size."""
        request = dict(request or {})
        url = str(request.get('url') or '')
        method = str(request.get('method') or 'GET').upper()
        if not _public_web_url(url):
            raise ValueError('Userscript requests must use a public HTTP(S) address')
        if method not in ('GET', 'POST', 'HEAD'):
            raise ValueError('Unsupported userscript request method')
        headers = {str(key): str(value) for key, value in (request.get('headers') or {}).items()
                   if str(key).casefold() not in ('cookie', 'authorization', 'host', 'proxy-authorization')}
        headers.setdefault('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) MediaIntake')
        data = request.get('data')
        outgoing = urllib.request.Request(url, data=str(data).encode('utf-8') if data is not None else None,
                                          headers=headers, method=method)
        try:
            reply = self.opener(outgoing, timeout=20)
        except urllib.error.HTTPError as error:
            reply = error
        with reply:
            body = reply.read(GM_XHR_MAX_BYTES + 1)[:GM_XHR_MAX_BYTES]
            return {'status': reply.status, 'statusText': str(reply.reason or ''), 'finalUrl': reply.geturl(),
                    'responseHeaders': ''.join(f'{key}: {value}\r\n' for key, value in reply.headers.items()),
                    'responseText': body.decode('utf-8', 'replace')}


def is_download_destination(url):
    from .core import EXTENSIONS
    from .file_hosts import FILE_HOSTS, MANUAL_HOSTS
    parts = urllib.parse.urlsplit(str(url or ''))
    host = (parts.hostname or '').casefold()
    return (host in FILE_HOSTS or host.removeprefix('www.') in MANUAL_HOSTS
            or Path(urllib.parse.unquote(parts.path)).suffix.casefold() in EXTENSIONS)


def page_action(current_url, start_url):
    """What to do after a page loads: 'inject', 'capture', 'offer' or None."""
    from .shortlinks import is_shortlink
    if not _public_web_url(current_url):
        return None
    if is_shortlink(current_url):
        return 'inject'
    if is_download_destination(current_url):
        return 'capture'
    return 'offer' if current_url != start_url else None


def read_captures(capture_folder, seen=None):
    """Return new capture records (oldest first); `seen` is a set of handled file names."""
    seen = seen if seen is not None else set()
    records = []
    for path in sorted(Path(capture_folder).glob(f'{CAPTURE_PREFIX}*.json')):
        if path.name in seen:
            continue
        seen.add(path.name)
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and _public_web_url(record.get('final_url')):
            records.append(record)
    return records


def main(url, profile_folder, capture_folder):
    """Open a shortlink with the userscript; send the final destination back to the app."""
    if not _public_web_url(url):
        raise ValueError('Shortlink helper requires a public web address')
    profile = Path(profile_folder).resolve()
    capture = Path(capture_folder).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    capture.mkdir(parents=True, exist_ok=True)
    os.environ['WEBVIEW2_USER_DATA_FOLDER'] = str(profile)
    payload = userscript_payload()
    try:
        import webview
        actions = ShortlinkActions(url, capture)
        window = webview.create_window('Media Intake shortlink helper', url, width=1000, height=740,
                                       min_size=(560, 420), js_api=actions)

        def on_loaded():
            current = window.get_current_url() or ''
            action = page_action(current, url)
            if action == 'inject':
                window.evaluate_js(payload)
            elif action == 'capture':
                actions.capture(current)
                window.evaluate_js(send_button_script(True))
            elif action == 'offer':
                window.evaluate_js(send_button_script(False))

        window.events.loaded += on_loaded
        webview.start(gui='edgechromium')
    except Exception as exc:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror('Shortlink helper unavailable',
                                 f'WebView2 could not start ({exc}). Install the Microsoft WebView2 Runtime '
                                 'and pywebview (requirements-browser.txt).')
            root.destroy()
        except Exception:
            raise
