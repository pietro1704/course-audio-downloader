"""Course-neutral catalogue, literal output paths and identity validation."""
import json
import unicodedata
from pathlib import Path
from pipeline import Pipeline


def path_parts(parts):
    if (not isinstance(parts, list) or len(parts) < 2 or
            any(not isinstance(p, str) or not p or p in {'.', '..'} or
                any(c in p for c in '/\\\x00') for p in parts)):
        raise ValueError('invalid-site-path')
    return parts


def relative_audio_path(lesson):
    parts = path_parts(lesson['site_path'])
    return Path(*parts[:-1], parts[-1] + '.m4a')


def load_lessons(path):
    data = json.loads(Path(path).read_text())
    if data.get('schema') not in {None, 'course-catalog/v1'}:
        raise ValueError('unsupported-catalogue')
    strict = data.get('schema') == 'course-catalog/v1'
    if strict and (data.get('complete') is not True or data.get('expected_count') != data.get('collected_count')):
        raise ValueError('incomplete-catalogue')
    lessons = []
    groups = data.get('courses', data.get('modules', []))
    for index, group in enumerate(groups, 1):
        items = group.get('lessons', [])
        if group.get('expected_count', len(items)) != len(items): raise ValueError('module-count-mismatch')
        for number, item in enumerate(items, 1):
            if not item.get('url') or not item.get('title'): raise ValueError('unresolved-catalogue-lesson')
            parts = item.get('site_path') or ([data['title']] if data.get('title') else []) + [group['title'], item['title']]
            import course_audio_bot as audio
            lesson = dict(item, course_title=group['title'], site_path=path_parts(parts),
                          stem=f"{index:02d}-{item.get('number', number):03d}-{audio.slugify(item['title'])}")
            lessons.append(lesson)
    if not groups and data.get('lessons'):
        lessons = [dict(x, site_path=path_parts(x['site_path'])) for x in data['lessons']]
    identities = [Pipeline.identity(x['url']) for x in lessons]
    paths = [unicodedata.normalize('NFD', str(relative_audio_path(x))).casefold() for x in lessons]
    if len(set(identities)) != len(lessons) or len(set(paths)) != len(lessons):
        raise ValueError('duplicate-catalogue-identity-or-path')
    if strict and len(lessons) != data['expected_count']: raise ValueError('catalogue-count-mismatch')
    if not lessons: raise ValueError('empty-catalogue')
    return lessons
