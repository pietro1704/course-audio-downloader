import copy
import unittest
from unittest.mock import patch
from course_discovery import discover,parse_course,collect_attachments
from course_discovery import automatic_profile

PROFILE={'origin':'https://school.example.test','lesson_path_pattern':r'/lesson/\d+',
 'course':{'entry_path_pattern':r'/course/\d+','title_selector':'h1','links_selector':'a.lesson',
           'lesson_title_selector':'.title','duration_selector':'.duration'}}
PAGE={'url':'https://school.example.test/course/1','title':'Example course','ready':'complete',
 'lessons':[{'url':'https://school.example.test/lesson/1','title':'01. First','module':'Module A','duration':'02:00'},
            {'url':'https://school.example.test/lesson/2','title':'01. Second','module':'Module B','duration':'03:00'}]}

class DiscoveryTests(unittest.TestCase):
 def test_modules_titles_identities_and_no_manual_catalogue(self):
  course=parse_course(PAGE,PROFILE)
  self.assertEqual([m['title'] for m in course['modules_info']],['Module A','Module B'])
  self.assertEqual(course['lessons'][0]['site_path'],['Example course','Module A','01. First'])
  self.assertEqual(course['expected_count'],2)
  self.assertNotEqual(course['lessons'][0]['id'],course['lessons'][1]['id'])
  self.assertEqual(course['id'],parse_course(PAGE,PROFILE)['id'])
 def test_duplicate_ui_links_are_deduplicated_but_foreign_lessons_are_rejected(self):
  page=copy.deepcopy(PAGE);page['lessons'].append(page['lessons'][0])
  self.assertEqual(len(parse_course(page,PROFILE)['lessons']),2)
  page['lessons'][0]['url']='https://elsewhere.example.test/lesson/1'
  with self.assertRaisesRegex(ValueError,'foreign-course-lesson'):parse_course(page,PROFILE)
 def test_empty_and_unsafe_catalogues_fail_closed(self):
  for edit in [{'title':'../other'},{'lessons':[]},{'title':''}]:
   with self.subTest(edit=edit),self.assertRaises(ValueError):parse_course({**PAGE,**edit},PROFILE)
 def test_previous_page_is_ignored_while_navigation_commits(self):
  old=copy.deepcopy(PAGE);old['url']='https://school.example.test/course/2'
  class Browser:
   def __init__(self):self.pages=iter([old,PAGE,PAGE])
   def inventory(self,lesson):return []
   def _eval(self,url):pass
   def _script(self,script):return next(self.pages)
  with patch('course_discovery.time.sleep'):
   course,_=discover(PAGE['url'],profile=PROFILE,browser=Browser())
  self.assertEqual(course['url'],PAGE['url'])
 def test_attachment_metadata_is_linked_to_its_lesson_without_signed_urls(self):
  class Browser:
   def inventory(self,lesson):
    return [{'id':'notes-'+lesson['id'],'name':'notes.pdf','url':'https://files.example.test/signed?secret=hidden','mime':'application/pdf'}]
  course=collect_attachments(parse_course(PAGE,PROFILE),Browser())
  for lesson in course['lessons']:
   self.assertEqual(lesson['attachments'][0]['name'],'notes.pdf')
   self.assertNotIn('secret',str(lesson['attachments']));self.assertNotIn('url',lesson['attachments'][0])
 def test_automatically_tries_next_browser_after_an_unusable_session(self):
  class Browser:
   def __init__(self,valid):self.valid=valid
   def _eval(self,url):pass
   def _script(self,script):return PAGE if self.valid else {'login':True}
   def inventory(self,lesson):return []
  from unittest.mock import Mock
  first,second=Mock(),Mock()
  with patch('authenticated_browser.cookie_sessions',return_value=iter([('chrome',[]),('firefox',[])])),patch('authenticated_browser.CookieBrowser',side_effect=[first,second]),patch('authenticated_browser.browser_documents',side_effect=[Browser(False),Browser(True)]),patch('course_discovery.time.sleep'):
   course,profile=discover(PAGE['url'],profile=PROFILE)
  first.close.assert_called_once();second.close.assert_not_called()
  self.assertEqual(profile['_browser_source'],'firefox');self.assertEqual(len(course['lessons']),2)
 def test_attachment_discovery_failure_does_not_claim_completeness(self):
  class Browser:
   def inventory(self,lesson):raise RuntimeError('login-required')
  with self.assertRaisesRegex(RuntimeError,'login-required'):collect_attachments(parse_course(PAGE,PROFILE),Browser())
 def test_automatic_profile_uses_the_input_origin_without_public_site_contracts(self):
  self.assertEqual(automatic_profile('https://school.example.test/course/intro')['origin'],'https://school.example.test')
  self.assertEqual(automatic_profile('https://another.example/course')['mode'],'automatic')
  for url in ['http://school.example.test/course','https://user:secret@school.example.test/course']:
   with self.subTest(url=url),self.assertRaises(ValueError):automatic_profile(url)
 def test_discovery_uses_exact_page_and_stable_metadata(self):
  class Browser:
   def inventory(self,lesson):return []
   def _eval(self,url):self.url=url
   def _script(self,script):return PAGE
  with patch('course_discovery.time.sleep'):
   course,_=discover(PAGE['url'],profile=PROFILE,browser=Browser())
  self.assertEqual(len(course['lessons']),2)
  changed=copy.deepcopy(PAGE);changed['url']='https://school.example.test/course/2'
  class Wrong(Browser):
   def _script(self,script):return changed
  with self.assertRaisesRegex(RuntimeError,'course-page-identity-mismatch'):
   discover(PAGE['url'],profile=PROFILE,browser=Wrong(),timeout=.05)

if __name__=='__main__':unittest.main()
