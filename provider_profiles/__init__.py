"""Bundled, declarative provider contracts; no private course data or sessions."""
import json
from pathlib import Path
from urllib.parse import urlsplit


def profile_for(url):
    value=urlsplit(url)
    if value.scheme!='https' or value.username or value.password or not value.hostname:
        raise ValueError('invalid-course-url')
    origin=value.scheme+'://'+value.netloc
    for file in Path(__file__).parent.glob('*.json'):
        profile=json.loads(file.read_text(encoding='utf-8'))
        if profile['origin'].rstrip('/')==origin:
            return profile
    return dict(schema='course-site-profile/v1',origin=origin,mode='direct',generic=True,
        title_selector='h1, .lesson-title, .article-title',links_selector='a[href],audio[src],audio source[src]',
        lesson_path_pattern=r'.*(?:lesson|lecture|aula)[^/]*(?:/.*)?',
        auth_expression="![...document.querySelectorAll('input[type=password]')].some(x=>x.getClientRects().length>0)",
        course=dict(entry_path_pattern=r'/.*',title_selector='[data-course-title], .course-title, h1',
            links_selector='a[href]',lesson_title_selector='',duration_selector='.duration',
            module_selector='[data-module], .module, .section-group',module_title_selector='h2,h3,.module-title,.section-group-titulo'))
