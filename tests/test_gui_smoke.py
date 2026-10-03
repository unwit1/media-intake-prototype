"""Every app action runs without raising: once with nothing selected and once with a row selected in each list.

Dialogs answer "cancel", background threads are not started and nothing is opened
externally, so this checks the wiring of each command (attribute names, argument
lists, empty-selection handling), not the network work behind it.
"""
import contextlib
import inspect
from pathlib import Path
import tempfile
import tkinter as tk
import traceback
import unittest
from unittest.mock import patch

from media_intake import gui
from media_intake.core import enqueue
from media_intake.watched import add_watch

VIEWS = ('queue_view', 'library_view', 'watched_view', 'results_view')
CANCEL = [
    ('tkinter.messagebox.showinfo', None), ('tkinter.messagebox.showerror', None),
    ('tkinter.messagebox.showwarning', None), ('tkinter.messagebox.askyesno', False),
    ('tkinter.messagebox.askokcancel', False), ('tkinter.messagebox.askyesnocancel', None),
    ('tkinter.filedialog.askopenfilename', ''), ('tkinter.filedialog.askopenfilenames', ()),
    ('tkinter.filedialog.askdirectory', ''), ('tkinter.filedialog.asksaveasfilename', ''),
    ('tkinter.simpledialog.askstring', None), ('tkinter.simpledialog.askinteger', None),
    ('webbrowser.open', None), ('subprocess.Popen', None),
]


class NoThread:
    daemon = True

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def join(self, *args):
        pass

    def is_alive(self):
        return False


def actions():
    for name, function in inspect.getmembers(gui.MediaIntakeApp, inspect.isfunction):
        params = list(inspect.signature(function).parameters.values())[1:]
        if not name.startswith('_') and not any(
                p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                for p in params):
            yield name


class GuiSmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.stack = contextlib.ExitStack()
        for target, value in CANCEL:
            self.stack.enter_context(patch(target, return_value=value))
        self.stack.enter_context(patch('threading.Thread', NoThread))
        self.stack.enter_context(patch('os.startfile', create=True))
        self.stack.enter_context(patch('tkinter.Misc.wait_window', lambda self, w=None: (w or self).destroy()))
        self.stack.enter_context(patch('tkinter.Misc.wait_variable', lambda self, v=None: None))
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.stack.close()
            self.skipTest(f'No display: {exc}')
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *exc: self.errors.append(
            ('callback', ''.join(traceback.format_exception(*exc))))
        self.app = gui.MediaIntakeApp(self.root, data_root=self.temp.name)
        self.root.update()

    def tearDown(self):
        for job in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(job)  # the app's polling timers
        self.root.destroy()
        self.app.db.close()
        self.stack.close()
        self.temp.cleanup()

    def run_all(self, select=False):
        names = list(actions())
        self.assertGreater(len(names), 50)
        for name in names:
            if select:
                for view in VIEWS:
                    tree = getattr(self.app, view)
                    if tree.get_children() and not tree.selection():
                        tree.selection_set(tree.get_children()[0])
            try:
                getattr(self.app, name)()
                self.root.update()
            except Exception:
                self.errors.append((name, traceback.format_exc(limit=5)))
            for window in self.root.winfo_children():
                if isinstance(window, tk.Toplevel):
                    window.destroy()
        self.assertEqual(self.errors, [], '\n'.join(f'{n}:\n{t}' for n, t in self.errors))

    def test_every_action_with_nothing_selected(self):
        self.run_all()

    def test_every_action_with_a_row_selected_in_each_list(self):
        db = self.app.db
        book = Path(self.temp.name) / 'Library' / 'Example.epub'
        book.parent.mkdir(parents=True, exist_ok=True)
        book.write_bytes(b'PK')
        enqueue(db, {'title': 'Queued Book', 'author': 'Ann Author', 'format': '.epub', 'source': 'direct',
                     'url': 'https://example.org/queued.epub'})
        done = enqueue(db, {'title': 'Example', 'author': 'Ann Author', 'format': '.epub', 'source': 'direct',
                            'url': 'https://example.org/done.epub'})
        db.execute("UPDATE items SET state='downloaded', file_path=? WHERE id=?", (str(book), done['id']))
        db.commit()
        add_watch(db, 'book', 'Watched Book', 'Ann Author', 'https://openlibrary.org/works/OL1W')
        self.app.refresh_queue(); self.app.refresh_library(); self.app.refresh_watches()
        self.app.show_results([{'title': 'Result Book', 'author': 'Ann Author', 'kind': 'book', 'category': 'Books',
                                'format': 'catalog', 'source_role': 'metadata', 'discovery_only': True,
                                'url': 'https://openlibrary.org/works/OL2W',
                                'source_url': 'https://openlibrary.org/works/OL2W'}])
        self.root.update()
        for view in VIEWS:
            self.assertTrue(getattr(self.app, view).get_children(), view)
        self.run_all(select=True)


if __name__ == '__main__':
    unittest.main()
