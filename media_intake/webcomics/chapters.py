"""Evidence-preserving organization; an episode number is not a chapter."""
import re
from .models import WebcomicChapter, identity

BOUNDARY = re.compile(r'\b(chapter|ch\.|book|volume|vol\.|arc|part)\s*([\d]+(?:\.\d+)?|[IVXLCDM]+|one|two|three|four|five)\b', re.I)
SPECIAL = re.compile(r'^\s*(prologue|epilogue|interlude)\b', re.I)
KINDS = {'cover': 'cover', 'bonus': 'bonus', 'guest': 'guest_strip',
         'hiatus': 'hiatus_notice', 'intermission': 'intermission',
         'author note': 'author_notes', 'side story': 'side_story',
         'title page': 'title_page', 'chapter separator': 'chapter_separator'}


def content_type(title):
    text = title.casefold()
    return next((kind for word, kind in KINDS.items() if re.search(r'\b' + re.escape(word) + r'\b', text)), 'story')


def resolve_chapter(title, source_url, declared=None, override=None):
    if override is not None:
        if not override:
            return None
        return WebcomicChapter(identity('manual:' + override), override,
                              organization='MANUAL', evidence=[{'kind': 'persisted_override'}])
    if declared:
        return WebcomicChapter(identity('declared:' + declared), declared,
                              evidence=[{'kind': 'archive_heading', 'text': declared,
                                         'source_url': source_url}])
    match = BOUNDARY.search(title) or SPECIAL.search(title)
    if not match:
        return None
    label = match.group(0).strip()
    number = match.group(2) if match.lastindex == 2 else None
    return WebcomicChapter(identity('inferred:' + label.casefold()), label, number=number,
                          organization='INFERRED', confidence=0.7,
                          evidence=[{'kind': 'entry_title', 'text': title, 'source_url': source_url}])


def group_entries(entries, declared=None, overrides=None):
    chapters = {}
    for entry in entries:
        chapter = resolve_chapter(entry.title, entry.source_url,
                                  (declared or {}).get(entry.source_id),
                                  (overrides or {}).get(entry.source_id))
        entry.content_type = content_type(entry.title)
        if chapter:
            entry.chapter_id = chapter.source_id
            if chapter.source_id not in chapters:
                chapter.ordinal = entry.ordinal
                chapters[chapter.source_id] = chapter
            chapters[chapter.source_id].entry_ids.append(entry.source_id)
    return list(chapters.values())
