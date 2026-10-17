"""Exercise pinned-tab activation through real audio and document script dispatch."""
import itertools
import json
import unittest
from unittest.mock import Mock, patch
from browser_adapter import SafariAdapter
from document_browser import DocumentBrowser


class SafariSession:
    """A background tab exposes SSR identity but defers its interactive panel."""
    def __init__(self, origin):
        self.origin = origin
        self.native_url = origin + '/lesson/1'
        self.selected = False
        self.panel_ready = False
        self.scripts = []
        self.resource = dict(id='original', name='Original.pdf', url=origin+'/file.pdf')

    def __call__(self, script):
        self.scripts.append(script)
        guard = 'if (URL of t) starts with ' + json.dumps(self.origin+'/')
        if guard not in script or not self.native_url.startswith(self.origin+'/'):
            raise RuntimeError('Browser target changed')
        activation = 'set current tab of window id 42 to t'
        if activation in script:
            if script.index(activation) < script.index(guard):
                raise AssertionError('activation preceded origin validation')
            self.selected = True
        if 'set URL of t to ' in script:
            return 'navigation-requested'
        if 'window.mountPanel()' in script:
            self.panel_ready = self.selected
            return 'true'
        if 'items:' in script:
            return json.dumps(dict(ready=self.panel_ready, items=[self.resource] if self.panel_ready else []))
        return json.dumps(dict(url=self.native_url,canonical='',title='Lesson',ready='complete',auth=True,identities={}))


class BrowserActivationTests(unittest.TestCase):
    def browser(self, cls=DocumentBrowser, origin='https://alpha.example.test', activate=True):
        return cls(profile=dict(origin=origin, title_selector='h1',
            browser_target=dict(window_id=42, tab_index=7), activate_browser_tab=activate,
            documents=dict(panel_script='window.mountPanel()',items_expression='window.files',ready_expression='window.ready')),timeout=1)

    def test_background_document_panel_becomes_ready_in_same_pinned_tab(self):
        for origin in ['https://alpha.example.test','https://omega.example.test']:
            browser=self.browser(origin=origin);session=SafariSession(origin)
            lesson=dict(title='Lesson',url=origin+'/lesson/1')
            clock=Mock();clock.monotonic.side_effect=itertools.count(0,.1)
            with patch.object(browser,'_osa',side_effect=session),patch('document_browser.time',clock):
                self.assertEqual(browser.inventory(lesson),[session.resource])
            self.assertTrue(session.selected)
            self.assertTrue(all('set t to tab 7 of window id 42' in s for s in session.scripts))
            self.assertFalse(any('make new' in s for s in session.scripts))

    def test_audio_inspection_and_navigation_activate_existing_target(self):
        browser=self.browser(SafariAdapter);session=SafariSession(browser.origin)
        with patch.object(browser,'_osa',side_effect=session):
            browser._eval(browser.origin+'/lesson/1')
            self.assertTrue(session.selected)
            session.selected=False
            self.assertEqual(browser._eval()['title'],'Lesson')
            self.assertTrue(session.selected)

    def test_document_control_read_reactivates_target_after_selection_changes(self):
        browser=self.browser();session=SafariSession(browser.origin)
        with patch.object(browser,'_osa',side_effect=session):
            browser._script('window.mountPanel()')
            self.assertTrue(session.selected)

    def test_opt_out_preserves_selection(self):
        browser=self.browser(SafariAdapter,activate=False);session=SafariSession(browser.origin)
        with patch.object(browser,'_osa',side_effect=session):browser._eval()
        self.assertFalse(session.selected)

    def test_foreign_origin_never_activates_or_opens_controls(self):
        browser=self.browser();session=SafariSession(browser.origin);session.native_url='https://foreign.example.test/'
        with patch.object(browser,'_osa',side_effect=session),self.assertRaisesRegex(RuntimeError,'target changed'):
            browser._script('window.mountPanel()')
        self.assertFalse(session.selected);self.assertFalse(session.panel_ready)

    def test_cancelled_native_dispatch_does_not_issue_another_request(self):
        browser=self.browser()
        with patch.object(browser,'_osa',side_effect=KeyboardInterrupt) as native,self.assertRaises(KeyboardInterrupt):
            browser.inventory(dict(title='Lesson',url=browser.origin+'/lesson/1'))
        self.assertEqual(native.call_count,1)

    def test_activation_requires_boolean_and_explicit_target(self):
        for value in ['true',1,None]:
            browser=self.browser(SafariAdapter,activate=value)
            with patch.object(browser,'_osa') as native,self.assertRaisesRegex(ValueError,'invalid-browser-activation'):
                browser._eval()
            native.assert_not_called()
        browser=self.browser(SafariAdapter);browser.profile.pop('browser_target')
        with patch.object(browser,'_osa') as native,self.assertRaisesRegex(ValueError,'activation-requires-explicit-browser-target'):
            browser._eval()
        native.assert_not_called()
