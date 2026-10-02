import sys

from media_intake.gui import main


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--embedded-browser':
        from media_intake.embedded_browser import main as browser_main
        browser_main(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) == 5 and sys.argv[1] == '--shortlink-browser':
        from media_intake.shortlink_browser import main as shortlink_main
        shortlink_main(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        if sys.platform == 'win32' and not getattr(sys, 'frozen', False):
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('MediaArchiveImporter.Latest')
        main(window_title='Media Archive Importer (Latest)' if not getattr(sys, 'frozen', False) else None)

