"""Declarative website contracts; course identities belong to input data."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def load_profile(path):
    value = json.loads(Path(path).read_text())
    if value.get('schema') != 'course-site-profile/v1':
        raise ValueError('unsupported-site-profile')
    if value.get('mode') not in {'direct', 'embedded-json', 'template-hls', 'extractor'}:
        raise ValueError('unsupported-profile-mode')
    origin = urlsplit(value.get('origin', ''))
    if origin.scheme != 'https' or not origin.netloc or origin.path not in {'', '/'} or origin.username or origin.query:
        raise ValueError('invalid-profile-origin')
    for key in ('lesson_path_pattern', 'title_suffix_pattern'):
        if value.get(key): re.compile(value[key])
    if value['mode'] == 'template-hls':
        player = value.get('player', {})
        for key in ('host_pattern', 'path_pattern', 'manifest_url'):
            if not player.get(key): raise ValueError('incomplete-player-profile')
    if value['mode'] == 'extractor':
        pattern = value.get('extractor', {}).get('url_pattern', '')
        if 'id' not in re.compile(pattern).groupindex:
            raise ValueError('extractor-pattern-missing-identity')
    validate_url_path_aliases(value)
    validate_navigation(value)
    return value


def validate_url_path_aliases(profile):
    aliases = profile.get('url_path_prefix_aliases', {})
    if not isinstance(aliases, dict):
        raise ValueError('invalid-url-path-prefix-aliases')
    for source, target in aliases.items():
        for prefix in (source, target):
            if (not isinstance(prefix, str) or len(prefix) < 3 or
                    not prefix.startswith('/') or not prefix.endswith('/') or
                    any(c in prefix for c in ('?', '#', '\\', '//')) or
                    any(ord(c) <= 32 for c in prefix) or
                    any(part in {'.', '..'} for part in prefix.split('/'))):
                raise ValueError('invalid-url-path-prefix-aliases')
        if target in aliases and aliases[target] != target:
            raise ValueError('chained-url-path-prefix-aliases')


def normalized_url_path(path, profile):
    aliases = profile.get('url_path_prefix_aliases', {})
    for prefix in sorted(aliases, key=len, reverse=True):
        if path.startswith(prefix):
            return aliases[prefix] + path[len(prefix):]
    return path


def inspection_script(profile):
    """Generate read-only page inspection from a trusted local site profile."""
    expressions = profile.get('identity_expressions', {})
    identities = '{' + ','.join(json.dumps(k) + ':(' + v + ')' for k, v in expressions.items()) + '}'
    settings = json.dumps({k: profile.get(k) for k in
                          ('active_selector', 'active_text_pattern', 'active_title_selector',
                           'active_title_attribute', 'title_selector', 'links_selector')}, ensure_ascii=False)
    return """(()=>{const p=SETTINGS;
const titleOf=a=>{const x=p.active_title_selector?a.querySelector(p.active_title_selector):a;
 return x?(p.active_title_attribute?x.getAttribute(p.active_title_attribute):x.textContent).trim():''};
const active=p.active_selector?[...document.querySelectorAll(p.active_selector)].filter(a=>
 !p.active_text_pattern||new RegExp(p.active_text_pattern).test(a.textContent)):[];
return JSON.stringify({url:location.href,canonical:document.querySelector('link[rel=canonical]')?.href||'',
 title:p.title_selector?document.querySelector(p.title_selector)?.textContent||'':document.title,
 ready:document.readyState,auth:AUTH,identities:IDENTITIES,
 titles:active.filter(a=>a.pathname===location.pathname).map(titleOf),
 active:active.map(a=>({path:a.pathname,title:titleOf(a)})),
 frames:[...document.querySelectorAll('iframe[src]')].map(x=>x.src),
 iframes:[...document.querySelectorAll('iframe[src]')].map(x=>x.src),
 posters:[...document.querySelectorAll('video[poster]')].map(x=>x.poster),
 links:[...document.querySelectorAll(p.links_selector||'a[href],audio,audio source')].map(x=>
 ({url:x.currentSrc||x.src||x.href||'',label:x.innerText||x.getAttribute('aria-label')||''}))});})()""".replace('SETTINGS', settings).replace('AUTH', '(' + profile.get('auth_expression', 'true') + ')').replace('IDENTITIES', identities)


def identity_matches(page, lesson, profile):
    keys = set(profile.get('identity_expressions', {}))
    expected = lesson.get('page_identity', {})
    return keys <= expected.keys() and all(page.get('identities', {}).get(k) == expected[k] for k in keys)


def player_fields(url, profile):
    player = profile.get('player', profile)
    p = urlsplit(url)
    host = re.fullmatch(player.get('host_pattern', r'(?!)'), p.hostname or '')
    path = re.fullmatch(player.get('path_pattern', r'(?!)'), p.path)
    if p.scheme != 'https' or not host or not path:
        raise RuntimeError('unsupported-player-frame')
    from urllib.parse import parse_qs
    query = parse_qs(p.query)
    allowed = player.get('allowed_query_keys')
    if allowed is not None and set(query) - set(allowed):
        raise RuntimeError('unsupported-player-parameters')
    fields = {**host.groupdict(), **path.groupdict()}
    for name, key in player.get('query_fields', {}).items():
        if len(query.get(key, [])) != 1: raise RuntimeError('missing-player-field')
        fields[name] = query[key][0]
    for key, pattern in player.get('field_patterns', {}).items():
        if not re.fullmatch(pattern, fields.get(key, '')): raise RuntimeError('invalid-player-field')
    return fields


def validate_navigation(profile):
    if profile.get('navigation_mode','reload') not in {'reload','matching-link'}:
        raise ValueError('unsupported-navigation-mode')
    if profile.get('navigation_mode')=='matching-link' and profile.get('mode')!='embedded-json':
        raise ValueError('navigation-requires-guarded-embedded-adapter')
    keys=profile.get('navigation_ignored_query_keys',[])
    if not isinstance(keys,list) or any(not isinstance(k,str) or not k for k in keys):
        raise ValueError('invalid-navigation-query-keys')


def navigation_script(url,profile,context=None):
    """Click only the exact declared lesson URL, otherwise request normal reload."""
    validate_navigation(profile)
    settings=json.dumps(dict(url=url,ignored=profile.get('navigation_ignored_query_keys',[]),previous=context),ensure_ascii=False)
    return r"""(()=>{const p=SETTINGS;const target=new URL(p.url);
const key=(value,withoutHash=false)=>{try{const u=new URL(value,location.href);
 for(const k of p.ignored)u.searchParams.delete(k);
 if(withoutHash)u.hash='';return u.href;}catch{return null}};
// Revisiting a lesson must refresh expiring player/download credentials.
if(key(location.href,true)===key(target.href,true))return 'reload-required';
if(!p.previous||location.pathname.replace(/\/+$/,'')!==p.previous.page_path)return 'reload-required';
const frame=document.querySelector('iframe[src]');
try{const u=new URL(frame?.src||'',location.href);
 if(!frame||u.origin+u.pathname!==p.previous.player)return 'reload-required';
}catch{return 'reload-required'}
const a=[...document.querySelectorAll('a[href]')].find(a=>
 key(a.href)===key(target.href)&&!a.hasAttribute('download')&&!a.hasAttribute('disabled')&&
 (a.getAttribute('aria-disabled')||'').toLowerCase()!=='true'&&
 (!a.target||a.target.toLowerCase()==='_self'));
if(!a)return 'reload-required';a.click();return 'link-clicked';})()""".replace('SETTINGS',settings)
