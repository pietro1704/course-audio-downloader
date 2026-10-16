"""Explicit route aliases preserve lesson identity during live browser discovery."""
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from browser_adapter import SafariAdapter, canonical
from document_browser import DocumentBrowser
from site_profile import load_profile


class UrlPathAliasTests(unittest.TestCase):
    def inputs(self):
        profile = dict(schema='course-site-profile/v1', mode='direct',
                       origin='https://portal.example.test',
                       url_path_prefix_aliases={'/en-GB/': '/en-gb/'},
                       documents=dict(panel_script='window.openPanel()',
                                      items_expression='window.files'))
        lesson = dict(title='Lesson', url=profile['origin']+'/en-GB/lesson/CaseID')
        page = dict(url=lesson['url'], canonical=profile['origin']+'/en-gb/lesson/CaseID',
                    title='Lesson', auth=True, ready='complete',
                    links=[dict(url='https://assets.example.test/audio.mp3', label='Audio')])
        return profile, lesson, page

    def test_live_audio_resolver_accepts_only_configured_prefix_alias(self):
        profile, lesson, page = self.inputs()
        adapter = SafariAdapter(profile=profile, timeout=1)
        clock = Mock(); clock.monotonic.side_effect = itertools.count(0, .01)
        with patch('browser_adapter.time', clock), patch.object(
                adapter, '_eval', side_effect=lambda navigate=None: {} if navigate else page):
            self.assertEqual(adapter.resolve(lesson)[0].url, page['links'][0]['url'])

    def test_document_inventory_keeps_raw_page_guards_after_alias_match(self):
        profile, lesson, page = self.inputs()
        adapter = DocumentBrowser(profile=profile, timeout=1)
        clock = Mock(); clock.monotonic.side_effect = itertools.count(0, .01)
        files = [dict(id='file-1', name='Original.pdf', url='https://assets.example.test/file.pdf')]
        with patch('document_browser.time', clock), patch.object(
                adapter, '_eval', side_effect=lambda navigate=None: {} if navigate else page), patch.object(
                adapter, '_script', side_effect=[True, dict(ready=True, items=files)]) as script:
            self.assertEqual(adapter.inventory(lesson), files)
        guard = script.call_args_list[0].args[0]
        self.assertIn(json.dumps(page['url']), guard)
        self.assertIn(json.dumps(page['canonical']), guard)

    def test_other_identity_origin_title_and_auth_never_open_document_controls(self):
        profile, lesson, page = self.inputs()
        mutations = [dict(canonical=profile['origin']+'/en-gb/lesson/caseid'),
                     dict(canonical='https://other.example.test/en-gb/lesson/CaseID'),
                     dict(url=profile['origin']+'/en-GB/lesson/OtherID'),
                     dict(title='Other lesson'), dict(auth=False)]
        for changed in mutations:
            with self.subTest(changed=changed):
                adapter = DocumentBrowser(profile=profile, timeout=1)
                clock = Mock(); clock.monotonic.side_effect = itertools.count(0, .1)
                with patch('document_browser.time', clock), patch.object(
                        adapter, '_eval', side_effect=lambda navigate=None: {} if navigate else {**page, **changed}), patch.object(
                        adapter, '_script') as script:
                    with self.assertRaisesRegex(RuntimeError, 'page-identity-timeout'):
                        adapter.inventory(lesson)
                    script.assert_not_called()

    def test_aliases_are_opt_in_and_preserve_case_sensitive_suffixes(self):
        profile, lesson, page = self.inputs()
        self.assertNotEqual(canonical(lesson['url']), canonical(page['canonical']))
        self.assertEqual(canonical(lesson['url'], profile), canonical(page['canonical'], profile))
        self.assertNotEqual(canonical(lesson['url'], profile),
                            canonical(page['canonical'].replace('CaseID', 'caseid'), profile))
        self.assertNotEqual(canonical(lesson['url'], profile),
                            canonical(page['canonical'].replace('/en-gb/', '/en-gb-other/'), profile))
        adapter = DocumentBrowser(profile={k:v for k,v in profile.items() if k!='url_path_prefix_aliases'})
        self.assertFalse(adapter._matches(page, lesson))

    def test_cancellation_with_alias_profile_never_opens_controls(self):
        profile, lesson, _ = self.inputs()
        adapter = DocumentBrowser(profile=profile)
        with patch.object(adapter, '_eval', side_effect=KeyboardInterrupt), patch.object(adapter, '_script') as script:
            with self.assertRaises(KeyboardInterrupt): adapter.inventory(lesson)
            script.assert_not_called()

    def test_profile_rejects_ambiguous_or_invalid_prefix_contracts(self):
        profile, _, _ = self.inputs()
        invalid = [[], None, {'/': '/en/'}, {'/en': '/EN/'}, {'//host/': '/en/'},
                   {'/en/': 'https://other.example.test/'}, {'/en/': '/../'},
                   {'/en/': '/e?n/'}, {'/en/': '/e#n/'}, {'/en/': '/e\\n/'},
                   {'/en/': '/e n/'}, {'/A/': '/B/', '/B/': '/C/'}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'profile.json'
            path.write_text(json.dumps(profile)); self.assertEqual(load_profile(path), profile)
            for aliases in invalid:
                with self.subTest(aliases=aliases):
                    path.write_text(json.dumps({**profile, 'url_path_prefix_aliases':aliases}))
                    with self.assertRaisesRegex(ValueError, 'url-path-prefix-aliases'): load_profile(path)
