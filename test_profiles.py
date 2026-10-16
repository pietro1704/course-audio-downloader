"""Synthetic website contracts shared by regression tests."""
from copy import deepcopy
from template_adapter import TemplateAdapter as BaseTemplateAdapter,player_identity as identify
from embedded_adapter import EmbeddedAdapter as BaseEmbeddedAdapter
from media_transport import resolve_player as resolve,thumbnail_player as thumbnail

HTML_PLAYER={'host_pattern':r'player\.example\.test','path_pattern':r'/embed/(?P<library>[^/]+)/(?P<video>[^/]+)',
             'element':'media-player','identity_attributes':{'video':'video-id','library':'library-id'},
             'unprotected_attributes':{'drm-version':['0']},'media_attribute':'content-src','poster_attribute':'cast-poster'}
TEMPLATE_PROFILE={'schema':'course-site-profile/v1','mode':'template-hls','origin':'https://lessons.example.test',
 'lesson_path_pattern':r'/area/conteudo/aula/\d+','login_paths':['/auth/login'],
 'title_suffix_pattern':' - Example Academy$','active_selector':'.sections a[href]',
 'identity_expressions':{'collection':'window.exampleCollection','lesson':'window.exampleLesson'},
 'player':{'host_pattern':r'player-(?P<library>vz-[a-z0-9]{8}-[a-z0-9]{3})\.tv\.media\.example\.test',
 'path_pattern':'/embed/','query_fields':{'video':'v'},'allowed_query_keys':['v','color'],
 'field_patterns':{'video':'[a-f0-9-]{36}'},'config_url':'https://config.media.example.test/{library}/{video}.json',
 'manifest_url':'https://b-{library}.tv.media.example.test/{video}/playlist.m3u8',
 'thumbnail_url':'https://cdn.media.example.test/{library}/{video}/thumbnail.jpg'}}
EMBEDDED_PROFILE={'schema':'course-site-profile/v1','mode':'embedded-json','origin':'https://portal.example.test',
                  'player':{'host_pattern':r'embed\.example\.test'}}

class ProfiledTemplateAdapter(BaseTemplateAdapter):
    def __init__(self,*args,**kwargs):
        kwargs.setdefault('profile',deepcopy(TEMPLATE_PROFILE));super().__init__(*args,**kwargs)

class ProfiledEmbeddedAdapter(BaseEmbeddedAdapter):
    def __init__(self,*args,**kwargs):
        kwargs.setdefault('profile',deepcopy(EMBEDDED_PROFILE));super().__init__(*args,**kwargs)

def player_identity(src):return identify(src,TEMPLATE_PROFILE)
def resolve_player(frame,lesson):return resolve(frame,lesson,HTML_PLAYER)
def thumbnail_player(frame,lesson):return thumbnail(frame,lesson,HTML_PLAYER)
