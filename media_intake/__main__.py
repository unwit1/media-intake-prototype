import argparse
import json
import sys
from .core import connect, download, enqueue, import_calibre, list_items, search_catalog, search_opds
from .sources import SITE_PROFILES, resolve_detail, search_openlibrary, search_site
from .irc import enqueue_irc, search_irc
from .fmhy import candidates, fallback_search, refresh_directory
from .serial import KINDS, discover_feed, enqueue_serial


def main():
    p = argparse.ArgumentParser(prog='python -m media_intake', description='Search, queue, download, and import media')
    p.add_argument('--root', default='./media-intake-data', help='Queue database and downloads directory')
    sub = p.add_subparsers(dest='action', required=True)
    s = sub.add_parser('search'); s.add_argument('query'); s.add_argument('--catalog'); s.add_argument('--opds'); s.add_argument('--pages', type=int, default=3)
    ss = sub.add_parser('search-site'); ss.add_argument('site', choices=SITE_PROFILES); ss.add_argument('query'); ss.add_argument('--base'); ss.add_argument('--html', help='saved browser search-page HTML')
    rs = sub.add_parser('resolve-site'); rs.add_argument('site', choices=[*SITE_PROFILES, 'generic']); rs.add_argument('detail_url'); rs.add_argument('--title', required=True); rs.add_argument('--author', default='Unknown'); rs.add_argument('--format'); rs.add_argument('--html', help='saved browser detail-page HTML')
    ol = sub.add_parser('search-openlibrary'); ol.add_argument('query'); ol.add_argument('--limit', type=int, default=10)
    f = sub.add_parser('fallback'); f.add_argument('query'); f.add_argument('--directory'); f.add_argument('--opds', action='append', default=[]); f.add_argument('--site', choices=SITE_PROFILES, action='append', default=[]); f.add_argument('--limit', type=int, default=15)
    fl = sub.add_parser('list-fmhy'); fl.add_argument('query'); fl.add_argument('--directory'); fl.add_argument('--section'); fl.add_argument('--limit', type=int, default=30)
    fr = sub.add_parser('refresh-fmhy'); fr.add_argument('--out', required=True); fr.add_argument('--html')
    a = sub.add_parser('add'); a.add_argument('--title', required=True); a.add_argument('--author', default='Unknown'); a.add_argument('--url', required=True); a.add_argument('--format', required=True); a.add_argument('--source', required=True); a.add_argument('--series'); a.add_argument('--series-index', type=float)
    ai = sub.add_parser('add-irc'); ai.add_argument('config', help='JSON file describing IRC server, bot, pack and metadata')
    sub.add_parser('gui', help='Open the desktop interface')
    si = sub.add_parser('search-irc'); si.add_argument('query'); si.add_argument('--config', required=True, help='JSON server/channel configuration'); si.add_argument('--limit', type=int, default=100)
    air = sub.add_parser('add-irc-result'); air.add_argument('file', help='JSON output from search-irc'); air.add_argument('--index', type=int, default=0)
    sf = sub.add_parser('discover-feed'); sf.add_argument('url'); sf.add_argument('--kind', required=True, choices=sorted(KINDS)); sf.add_argument('--limit', type=int, default=100)
    ase = sub.add_parser('add-serial'); ase.add_argument('manifest', help='JSON series manifest with ordered chapter URLs')
    wc = sub.add_parser('add-webcomic', help='Resolve a public comic URL and persist an incremental watch')
    wc.add_argument('url'); wc.add_argument('--title'); wc.add_argument('--recipe', help='Declarative recipe JSON file')
    wc.add_argument('--adapter'); wc.add_argument('--archive-existing', action='store_true')
    sub.add_parser('list-webcomics')
    wcp = sub.add_parser('poll-webcomics'); wcp.add_argument('--force', action='store_true'); wcp.add_argument('--library')
    b = sub.add_parser('add-json'); b.add_argument('file'); b.add_argument('--index', type=int, default=0)
    l = sub.add_parser('list'); l.add_argument('--state')
    d = sub.add_parser('download'); d.add_argument('id', type=int)
    i = sub.add_parser('import-calibre'); i.add_argument('id', type=int); i.add_argument('--library')
    args = p.parse_args()
    if args.action == 'search':
        if bool(args.catalog) == bool(args.opds): p.error('Specify exactly one of --catalog or --opds')
        result = search_catalog(args.catalog, args.query) if args.catalog else search_opds(args.opds, args.query, args.pages)
    elif args.action == 'search-site': result = search_site(args.site, args.query, args.base, args.html)
    elif args.action == 'resolve-site': result = resolve_detail(args.site, args.detail_url, args.title, args.author, args.format, args.html)
    elif args.action == 'search-openlibrary': result = search_openlibrary(args.query, args.limit)
    elif args.action == 'fallback': result = fallback_search(args.query, args.directory, args.opds, args.site, args.limit)
    elif args.action == 'list-fmhy': result = candidates(args.query, args.directory, args.limit, args.section)
    elif args.action == 'refresh-fmhy': result = refresh_directory(args.out, args.html)
    elif args.action == 'search-irc': result = search_irc(args.query, json.loads(open(args.config, encoding='utf-8').read()), args.limit)
    elif args.action == 'discover-feed': result = discover_feed(args.url, args.kind, args.limit)
    elif args.action == 'gui':
        from .gui import main as gui_main
        gui_main(args.root)
        return
    else:
        db = connect(args.root)
        if args.action == 'add': result = enqueue(db, {k: v for k, v in vars(args).items() if k in ('title','author','url','format','source','series','series_index') and v is not None})
        elif args.action == 'add-irc': result = enqueue_irc(db, json.loads(open(args.config, encoding='utf-8').read()))
        elif args.action == 'add-irc-result': result = enqueue_irc(db, json.loads(open(args.file, encoding='utf-8').read())[args.index]['irc'])
        elif args.action == 'add-serial': result = enqueue_serial(db, json.loads(open(args.manifest, encoding='utf-8').read()))
        elif args.action == 'add-webcomic':
            from .webcomics.watcher import add_series
            config = {'adapter': args.adapter} if args.adapter else {}
            if args.recipe:
                with open(args.recipe, encoding='utf-8') as stream:
                    config['recipe'] = json.load(stream)
            result = {'watch_id': add_series(db, args.url, title=args.title, config=config, archive_existing=args.archive_existing)}
        elif args.action == 'list-webcomics':
            from .webcomics.storage import library
            result = library(db)
        elif args.action == 'poll-webcomics':
            from .webcomics.watcher import process_watch
            from .watched import _now
            rows = db.execute("SELECT * FROM watched_items WHERE kind='webcomic' AND json_extract(metadata,'$.webcomics') IS NOT NULL AND (? OR next_check_at<=?)",
                              (args.force, _now().isoformat())).fetchall()
            for row in rows:
                process_watch(db, dict(row), args.library or str(__import__('pathlib').Path(args.root) / 'downloads'))
            result = {'checked': len(rows)}
        elif args.action == 'add-json': result = enqueue(db, json.loads(open(args.file, encoding='utf-8').read())[args.index])
        elif args.action == 'list': result = list_items(db, args.state)
        elif args.action == 'download': result = download(db, args.id, args.root)
        elif args.action == 'import-calibre': result = import_calibre(db, args.id, args.library)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)

