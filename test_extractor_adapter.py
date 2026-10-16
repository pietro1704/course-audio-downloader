"""Exercise external media at the public adapter boundary, using synthetic sites."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from extractor_adapter import ExtractorAdapter
from course_cli import build_adapter
from site_profile import load_profile

PROFILE={'schema':'course-site-profile/v1','mode':'extractor','origin':'https://portal.example.test',
 'title_selector':'h1','active_selector':'a.current',
 'extractor':{'url_pattern':r'https://video\.example\.test/(?:embed|watch)/(?P<id>[A-Za-z0-9_-]+)'}}
LESSON={'url':'https://portal.example.test/content/one','title':'Aula exata'}
PAGE={'url':LESSON['url'],'title':LESSON['title'],'auth':True,'ready':'complete',
 'titles':[LESSON['title']],'frames':['https://video.example.test/embed/clip'], 'links':[]}
INFO={'id':'clip','duration':12.,'url':'https://cdn.example.test/audio?token=PRIVATE',
 'protocol':'https','vcodec':'none','acodec':'aac','thumbnail':'https://cdn.example.test/art.jpg',
 'filesize':1024}

class Session:
    info=INFO
    def __init__(self, options):self.options=options
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def extract_info(self,url,download):return copy.deepcopy(self.info)
    def process_info(self,info):
        Path(self.options['outtmpl']).write_bytes(b'audio bytes')
        for hook in self.options['progress_hooks']:hook({'downloaded_bytes':11})

class ExtractorTests(unittest.TestCase):
    def adapter(self,**kwargs):return ExtractorAdapter(profile=copy.deepcopy(PROFILE),timeout=.1,**kwargs)
    def resolve(self,adapter=None,page=None,info=None):
        adapter=adapter or self.adapter()
        with patch.object(adapter,'_eval',return_value=page or PAGE),patch('browser_adapter.time.sleep'),patch.object(adapter,'_factory',return_value=Session),patch.object(Session,'info',INFO if info is None else info):
            return adapter,adapter.resolve(LESSON)[0]

    def test_profile_and_factory(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'site.json';p.write_text(json.dumps(PROFILE))
            self.assertIsInstance(build_adapter(load_profile(p)),ExtractorAdapter)

    def test_resolution_binds_metadata_and_artwork(self):
        adapter,c=self.resolve()
        self.assertEqual(c.url,PAGE['frames'][0]);self.assertEqual(c.kind,'extractor-audio')
        self.assertEqual(c.thumbnail_url,INFO['thumbnail']);self.assertEqual(c.referer,LESSON['url'])
        self.assertNotIn('PRIVATE',repr(c))

    def test_wrong_browser_lesson_never_extracts(self):
        adapter=self.adapter();page={**PAGE,'url':'https://portal.example.test/content/other'}
        with patch.object(adapter,'_eval',return_value=page),patch('browser_adapter.time.sleep'),patch('browser_adapter.time.monotonic',side_effect=[0,1,2,3]),patch.object(adapter,'_factory') as factory:
            with self.assertRaisesRegex(RuntimeError,'identity-timeout'):adapter.resolve(LESSON)
        factory.assert_not_called()

    def test_catalogued_media_identity_must_match_current_embed(self):
        a=self.adapter()
        with patch.object(a,'_eval',return_value=PAGE),patch('browser_adapter.time.sleep'),patch.object(a,'_factory') as factory:
            with self.assertRaisesRegex(RuntimeError,'catalogue-media-identity'):
                a.resolve({**LESSON,'external_media_id':'different'})
        factory.assert_not_called()

    def test_wrong_media_identity_and_ineligible_metadata(self):
        for fields in [{'id':'other'},{'_type':'playlist'},{'is_live':True},{'has_drm':True},{'duration':None},{'duration':float('nan')},{'protocol':'http'},{'url':'file:///private'},{'vcodec':'h264'},{'filesize':999999999}]:
            with self.subTest(fields=fields),self.assertRaises(RuntimeError) as error:self.resolve(info={**INFO,**fields})
            self.assertNotIn('PRIVATE',str(error.exception))

    def test_download_uses_validated_metadata_and_checks_duration(self):
        adapter,c=self.resolve()
        with tempfile.TemporaryDirectory() as t,patch.object(adapter,'_factory',return_value=Session),patch('extractor_adapter.audio._probe_duration',return_value=12):
            p=Path(t)/'source.media';self.assertEqual(adapter.download(c,p),p);self.assertEqual(p.read_bytes(),b'audio bytes')

    def test_actual_byte_cap_and_truncation(self):
        for limit,duration,pattern in [(5,12,'budget'),(1024,3,'incomplete'),(1024,float('nan'),'incomplete')]:
            adapter,c=self.resolve()
            adapter.max_source_bytes=limit
            with tempfile.TemporaryDirectory() as t,patch.object(adapter,'_factory',return_value=Session),patch('extractor_adapter.audio._probe_duration',return_value=duration):
                with self.assertRaisesRegex(RuntimeError,pattern):adapter.download(c,Path(t)/'source.media')

    def test_unknown_declared_size_is_bounded_by_progress_hook(self):
        adapter,c=self.resolve(info={**INFO,'filesize':None})
        adapter.max_source_bytes=5
        with tempfile.TemporaryDirectory() as t,patch.object(adapter,'_factory',return_value=Session),patch('extractor_adapter.audio._probe_duration',return_value=12):
            with self.assertRaisesRegex(RuntimeError,'budget'):adapter.download(c,Path(t)/'source.media')

    def test_ambiguous_media_and_untrusted_hosts_are_rejected(self):
        self.assertEqual(ExtractorAdapter.candidates({**PAGE,'frames':['https://video.example.test.evil/embed/clip']},PROFILE),[])
        with self.assertRaisesRegex(RuntimeError,'ambiguous'):
            ExtractorAdapter.candidates({**PAGE,'frames':PAGE['frames']+['https://video.example.test/embed/other']},PROFILE)
        same=ExtractorAdapter.candidates({**PAGE,'links':[{'url':'https://video.example.test/watch/clip'}]},PROFILE)
        self.assertEqual(len(same),1)

    def test_destination_collision_does_not_overwrite(self):
        adapter,c=self.resolve()
        with tempfile.TemporaryDirectory() as t,patch.object(adapter,'_factory') as factory:
            p=Path(t)/'source.media';p.write_bytes(b'keep')
            with self.assertRaisesRegex(RuntimeError,'already-exists'):adapter.download(c,p)
            self.assertEqual(p.read_bytes(),b'keep');factory.assert_not_called()

    def test_unknown_candidate_never_downloads(self):
        from pipeline import Candidate
        a=self.adapter()
        with patch.object(a,'_factory') as factory,self.assertRaisesRegex(RuntimeError,'unresolved'):a.download(Candidate('https://video.example.test/embed/other','extractor-audio'),Path('/unused'))
        factory.assert_not_called()

    def test_extraction_failure_sanitized_and_cancel_propagates(self):
        for error in [RuntimeError('https://private/?token=PRIVATE'),KeyboardInterrupt()]:
            a=self.adapter()
            with patch.object(a,'_eval',return_value=PAGE),patch('browser_adapter.time.sleep'),patch.object(a,'_factory',return_value=Session),patch.object(Session,'extract_info',side_effect=error):
                with self.assertRaises(type(error)) as raised:a.resolve(LESSON)
                self.assertNotIn('PRIVATE',str(raised.exception))

    def test_download_failure_sanitized_and_cancel_propagates(self):
        for error in [RuntimeError('token=PRIVATE'),KeyboardInterrupt()]:
            a,c=self.resolve()
            with tempfile.TemporaryDirectory() as t,patch.object(a,'_factory',return_value=Session),patch.object(Session,'process_info',side_effect=error):
                with self.assertRaises(type(error)) as raised:a.download(c,Path(t)/'source.media')
                self.assertNotIn('PRIVATE',str(raised.exception))

    def test_missing_artwork_still_resolves(self):
        a,c=self.resolve(info={**INFO,'thumbnail':None});self.assertIsNone(c.thumbnail_url)

    def test_configured_browser_target_never_falls_back_to_another_tab(self):
        a=self.adapter();a.profile['browser_target']={'window_id':42,'tab_index':3}
        with patch.object(a,'_osa',return_value=json.dumps(PAGE)) as osa:
            self.assertEqual(a._eval()['title'],LESSON['title'])
            script=osa.call_args.args[0]
            self.assertIn('tab 3 of window id 42',script)
            self.assertNotIn('repeat with',script)
            self.assertIn('starts with',script)
        with patch.object(a,'_osa',side_effect=RuntimeError('target-changed')) as osa:
            with self.assertRaises(RuntimeError):a._eval()
            self.assertEqual(osa.call_count,1)

    def test_invalid_browser_target_is_rejected(self):
        for target in [{'window_id':True,'tab_index':1},{'window_id':1,'tab_index':0},{'window_id':'1','tab_index':1}]:
            a=self.adapter();a.profile['browser_target']=target
            with patch.object(a,'_osa') as osa,self.assertRaises(ValueError):a._eval()
            osa.assert_not_called()

    def test_dependency_failure_is_actionable(self):
        with patch.dict('sys.modules',{'yt_dlp':None}),self.assertRaisesRegex(RuntimeError,'extractor-dependency-missing'):self.adapter()._factory()

if __name__=='__main__':unittest.main()
