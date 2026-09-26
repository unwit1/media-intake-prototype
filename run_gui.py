import sys

from media_intake.gui import main


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--embedded-browser':
        from media_intake.embedded_browser import main as browser_main
        browser_main(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        main()

