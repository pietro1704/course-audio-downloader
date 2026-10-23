"""Execute generated browser navigation code in a real JavaScript runtime."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from browser_adapter import SafariAdapter
from embedded_adapter import EmbeddedAdapter
from site_profile import navigation_script,load_profile

TARGET='https://portal.example.test/lesson/two'
PROFILE=dict(schema='course-site-profile/v1',mode='embedded-json',origin='https://portal.example.test',
             navigation_mode='matching-link',navigation_ignored_query_keys=['source'])
class NavigationTests(unittest.TestCase):
 def evaluate(self,nodes,current='https://portal.example.test/lesson/one',target=TARGET,frame='https://player.example.test/embed/one'):
  code='const clicks=[];global.location=new URL('+json.dumps(current)+');const nodes='+json.dumps(nodes)+';global.document={querySelector:()=>({src:'+json.dumps(frame)+'}),querySelectorAll:()=>nodes.map(x=>({...x,hasAttribute:k=>Object.hasOwn(x,k),getAttribute:k=>x[k]??null,click:()=>clicks.push(x.href)}))};'
  code+='const result='+navigation_script(target,PROFILE,dict(page_path='/lesson/one',player='https://player.example.test/embed/one'))+';process.stdout.write(JSON.stringify({result,clicks}));'
  r=subprocess.run(['node'],input=code,capture_output=True,text=True,timeout=15);self.assertEqual(r.returncode,0,r.stderr);return json.loads(r.stdout)
 def test_matching_link_ignores_only_declared_tracking_keys(self):
  v=self.evaluate([dict(href=TARGET+'?source=menu')]);self.assertEqual(v,dict(result='link-clicked',clicks=[TARGET+'?source=menu']))
 def test_wrong_origin_query_and_unsafe_controls_fall_back(self):
  cases=[dict(href='https://other.example.test/lesson/two'),dict(href=TARGET+'?action=remove'),dict(href=TARGET,target='_blank'),dict(href=TARGET,download='audio'),dict(href=TARGET,**{'aria-disabled':'true'}),dict(href='http://['),dict(href='javascript:alert(1)'),dict(href='https://name:secret@portal.example.test/lesson/two')]
  for node in cases:
   with self.subTest(node=node):self.assertEqual(self.evaluate([node]),dict(result='reload-required',clicks=[]))
 def test_wrong_candidates_do_not_hide_valid_link(self):
  v=self.evaluate([dict(href=TARGET,target='_blank'),dict(href='http://['),dict(href=TARGET,target='_SeLf')]);self.assertEqual(v['result'],'link-clicked');self.assertEqual(v['clicks'],[TARGET])
 def test_same_document_always_reloads_even_when_fragment_differs(self):
  for current in [TARGET,TARGET+'?source=sidebar',TARGET+'#section']:
   with self.subTest(current=current):self.assertEqual(self.evaluate([dict(href=TARGET)],current=current),dict(result='reload-required',clicks=[]))
 def test_existing_reload_is_used_when_no_link_matches(self):
  a=EmbeddedAdapter(profile={**PROFILE,'browser_target':dict(window_id=42,tab_index=2)})
  a._link_context=dict(page_path='/lesson/one',player='https://player.example.test/embed/one')
  with patch.object(a,'_osa',side_effect=['reload-required','navigation-requested']) as osa:
   self.assertEqual(a._eval(TARGET),{})
  self.assertEqual(osa.call_count,2);self.assertIn('do JavaScript',osa.call_args_list[0].args[0]);self.assertIn('set URL of t to',osa.call_args_list[1].args[0]);self.assertIn('tab 2 of window id 42',osa.call_args_list[1].args[0])
 def test_opt_out_and_cancellation_preserve_existing_contract(self):
  a=SafariAdapter(profile={'origin':PROFILE['origin']})
  with patch.object(a,'_osa',return_value='navigation-requested') as osa:a._eval(TARGET)
  self.assertIn('set URL of t to',osa.call_args.args[0]);self.assertNotIn('do JavaScript',osa.call_args.args[0])
  a=EmbeddedAdapter(profile=PROFILE);a._link_context=dict(page_path='/lesson/one',player='https://player.example.test/embed/one')
  with patch.object(a,'_osa',side_effect=KeyboardInterrupt()):
   with self.assertRaises(KeyboardInterrupt):a._eval(TARGET)
 def test_invalid_navigation_settings_fail_profile_validation(self):
  for override in [dict(navigation_mode='unknown'),dict(mode='direct'),dict(navigation_ignored_query_keys='source'),dict(navigation_ignored_query_keys=[1]),dict(navigation_ignored_query_keys=[''])]:
   with self.subTest(override=override),tempfile.TemporaryDirectory() as t:
    p=Path(t)/'site.json';p.write_text(json.dumps({**PROFILE,**override}))
    with self.assertRaises(ValueError):load_profile(p)

 def test_changed_predecessor_or_player_never_clicks(self):
  for kwargs in [dict(current='https://portal.example.test/lesson/other'),dict(frame='https://player.example.test/embed/other')]:
   with self.subTest(kwargs=kwargs):self.assertEqual(self.evaluate([dict(href=TARGET)],**kwargs),dict(result='reload-required',clicks=[]))
 def test_first_embedded_navigation_requires_full_reload(self):
  a=EmbeddedAdapter(profile=PROFILE)
  with patch.object(a,'_osa',return_value='navigation-requested') as osa:a._eval(TARGET)
  self.assertIn('set URL of t to',osa.call_args.args[0]);self.assertNotIn('do JavaScript',osa.call_args.args[0])
 def test_failure_consumes_permission_and_next_navigation_reloads(self):
  a=EmbeddedAdapter(profile=PROFILE);a._link_context=dict(page_path='/lesson/one',player='https://player.example.test/embed/one')
  with patch.object(a,'_osa',side_effect=RuntimeError('Safari-unavailable')):
   with self.assertRaises(RuntimeError):a._eval(TARGET)
  self.assertIsNone(a._link_context)
  with patch.object(a,'_osa',return_value='navigation-requested') as osa:a._eval(TARGET)
  self.assertIn('set URL of t to',osa.call_args.args[0])
