"""Small WebView2 sign-in window used for user-operated forum searches."""
import os
from pathlib import Path
import urllib.parse
from datetime import datetime, timezone
import secrets

FORUM_HOST = 'fora.snahp.eu'
MAX_CAPTURE_BYTES = 5_000_000


class BrowserActions:
    def __init__(self, capture_folder):
        self.capture_folder = Path(capture_folder)
        self.window = None

    def save_current_page(self, page_html):
        """Save a user-selected forum page for the existing thread-only parser."""
        if self.window is None:
            raise RuntimeError('The browser is not ready')
        parts = urllib.parse.urlsplit(self.window.get_current_url() or '')
        if parts.scheme != 'https' or parts.hostname != FORUM_HOST or parts.username or parts.password:
            raise ValueError('Only pages on the signed-in forum can be imported')
        if not isinstance(page_html, str):
            raise ValueError('The current page did not return HTML')
        content = page_html.encode('utf-8')
        if not content or len(content) > MAX_CAPTURE_BYTES:
            raise ValueError('Forum page must be smaller than 5 MB')
        self.capture_folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        target = self.capture_folder / f'forum-results-{stamp}-{secrets.token_hex(3)}.html'
        temporary = target.with_suffix('.html.tmp')
        temporary.write_bytes(content)
        temporary.replace(target)
        return 'Saved this forum page for thread-title import.'


def _forum_import_button_script():
    return r'''(() => {
      if (location.hostname !== "fora.snahp.eu" || document.getElementById("media-intake-import")) return;
      const button = document.createElement("button");
      button.id = "media-intake-import";
      button.textContent = "Import thread titles";
      button.setAttribute("style", "position:fixed;z-index:2147483647;right:18px;bottom:18px;padding:11px 15px;border:0;border-radius:8px;background:#2563eb;color:white;font:600 14px sans-serif;box-shadow:0 2px 10px #0008;cursor:pointer");
      button.addEventListener("click", async () => {
        button.disabled = true;
        button.textContent = "Importing…";
        try {
          button.textContent = await window.pywebview.api.save_current_page(document.documentElement.outerHTML);
        } catch (error) {
          button.textContent = "Import failed";
          button.title = String(error);
        }
        setTimeout(() => { button.disabled = false; button.textContent = "Import thread titles"; }, 2200);
      });
      document.body.appendChild(button);
    })();'''


def main(url, profile_folder, capture_folder):
    """Launch pywebview in its own process so Tkinter keeps its own UI loop."""
    parsed = urllib.parse.urlsplit(str(url or ''))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Embedded browser requires a public HTTPS URL')
    profile = Path(profile_folder).resolve()
    capture = Path(capture_folder).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    capture.mkdir(parents=True, exist_ok=True)
    # The profile stays alongside the app's SQLite data and is independent of
    # Edge/Chrome profiles. It contains the signed-in webview session cookies.
    os.environ['WEBVIEW2_USER_DATA_FOLDER'] = str(profile)
    try:
        import webview
        actions = BrowserActions(capture)
        window = webview.create_window(
            'Media Intake browser', url, width=980, height=720,
            min_size=(560, 420), js_api=actions)
        actions.window = window

        def on_loaded():
            current = urllib.parse.urlsplit(window.get_current_url() or '')
            if current.scheme == 'https' and current.hostname == FORUM_HOST:
                window.evaluate_js(_forum_import_button_script())

        window.events.loaded += on_loaded
        webview.start(gui='edgechromium')
    except Exception as exc:
        # No attempt is made to automate sign-in or work around a site challenge.
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                'Embedded browser unavailable',
                f'WebView2 could not start ({exc}). Install the Microsoft WebView2 Runtime, '
                'or open the site in your normal browser and use Load saved HTML.')
            root.destroy()
        except Exception:
            raise
