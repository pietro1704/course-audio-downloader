"""Original documents exercise the real streaming, receipt and publication stages."""
import io
import json
import hashlib
from email.message import Message
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from documents import DocumentDelivery,document_resource,document_preflight

PDF=b'%PDF-1.7\noriginal document bytes\n%%EOF\n'
class Response(io.BytesIO):
    def __init__(self,data=PDF,mime='application/pdf',length=None):
        super().__init__(data);self.headers=Message();self.headers['Content-Type']=mime
        self.headers['Content-Length']=str(len(data) if length is None else length)
    def geturl(self):return 'https://assets.example.test/file'
class Cloud:
    def __init__(self):self.ready=True;self.evicted=[]
    def status(self,path):
        return dict(ubiquitous=True,uploaded=self.ready,uploading=not self.ready,upload_error=False,
            conflicts=False,materialized=True,size=path.stat().st_size,mtime_ns=path.stat().st_mtime_ns)
    def evict(self,path):self.evicted.append(path)
class DocumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.lesson=dict(title='Lesson: One',url='https://alpha.example.test/lesson/1',site_path=['Course','Module','Lesson: One'])
        self.resource=dict(id='stable-file',name='Anexo – original.pdf',url='https://assets.example.test/file?token=private',size=len(PDF))
        self.adapter=Mock();self.adapter.inventory.return_value=[self.resource];self.adapter.url_for.side_effect=lambda l,r:r['url']
        self.cloud=Cloud();self.d=DocumentDelivery(self.base/'work',self.base/'cloud',self.cloud,self.adapter,
            reserve_bytes=0,upload_timeout=0,poll_seconds=0)
    def transfer(self):
        with patch('documents.urllib.request.urlopen',side_effect=lambda *a,**k:Response()):return self.d.run([self.lesson])

    def shared_resources(self):
        first={**self.resource,'shared_path_with':['other-file'],'shared_file_sha256':hashlib.sha256(PDF).hexdigest()}
        second={**first,'id':'other-file','url':'https://assets.example.test/other?token=private2',
                'shared_path_with':[first['id']]}
        return [first,second]

    def test_declared_identical_documents_preserve_both_identities_and_original_filename(self):
        resources=self.shared_resources();self.adapter.inventory.return_value=resources
        report=self.transfer()
        self.assertEqual((report['documents'],report['complete']),(2,2))
        receipts=[self.d.journal[self.d.identity(self.lesson,r)] for r in resources]
        self.assertEqual(receipts[0]['cloud'],receipts[1]['cloud'])
        self.assertEqual(Path(receipts[0]['cloud']).read_bytes(),PDF)
        self.assertFalse(Path(receipts[0]['final']).exists())
        self.assertEqual(len(list(self.d.destination.rglob('*.pdf'))),1)
        self.assertEqual(receipts[0]['shared_resource_ids'],[receipts[1]['resource_id']])
        with patch('documents.urllib.request.urlopen',side_effect=AssertionError('repeat download')):
            self.assertTrue(self.d.run([self.lesson])['ok'])
        self.assertNotIn('token=private',json.dumps(self.d.journal))
        changed=dict(resources[0]);changed.pop('shared_path_with');changed.pop('shared_file_sha256')
        with self.assertRaisesRegex(ValueError,'identity-mismatch'):self.d.one(self.lesson,changed)

    def test_shared_document_permission_does_not_allow_changed_bytes_or_replace_valid_cloud_file(self):
        resources=self.shared_resources();self.adapter.inventory.return_value=resources
        changed=PDF.replace(b'original',b'changed!')
        def response(request,**kwargs):return Response(PDF if request.full_url==resources[0]['url'] else changed)
        with patch('documents.urllib.request.urlopen',side_effect=response),self.assertRaisesRegex(RuntimeError,'incomplete'):
            self.d.run([self.lesson])
        receipt=self.d.journal[self.d.identity(self.lesson,resources[0])]
        self.assertEqual(Path(receipt['cloud']).read_bytes(),PDF)
        self.assertNotIn(self.d.identity(self.lesson,resources[1]),self.d.journal)
        self.assertTrue(any(p.read_bytes()==changed for p in (self.d.root/'.document-staging').glob('*.partial')))

    def test_shared_document_permissions_are_reciprocal_and_checksum_bound_before_download(self):
        for kind in ['unknown-peer','different-hash','different-name','no-checksum']:
            resources=self.shared_resources()
            if kind=='unknown-peer':resources[1]['shared_path_with']=['foreign-id']
            if kind=='different-hash':resources[1]['shared_file_sha256']='0'*64
            if kind=='different-name':resources[1]['name']='other.pdf'
            if kind=='no-checksum':resources[1].pop('shared_file_sha256')
            self.adapter.inventory.return_value=resources
            with self.subTest(kind=kind),patch('documents.urllib.request.urlopen') as http,\
                 self.assertRaisesRegex(RuntimeError,'incomplete'):
                self.d.run([self.lesson])
            http.assert_not_called();self.assertEqual(self.d.journal,{})

    def test_cancelled_shared_document_never_downloads_or_removes_a_source(self):
        self.d.cancel()
        with patch('documents.urllib.request.urlopen') as http, self.assertRaisesRegex(RuntimeError,'cancelled'):
            self.d.one(self.lesson,self.shared_resources()[0])
        http.assert_not_called();self.assertFalse(self.d.journal)
    def test_two_unrelated_courses_exact_bytes_names_and_restart(self):
        for name in ('alpha','omega'):
            self.lesson['url']='https://'+name+'.example.test/lesson/1';self.lesson['site_path'][0]=name
            self.assertTrue(self.transfer()['ok'])
            receipt=self.d.journal[self.d.identity(self.lesson,self.resource)]
            self.assertEqual(Path(receipt['cloud']).read_bytes(),PDF)
            self.assertEqual(Path(receipt['cloud']).name,self.resource['name'])
            self.assertFalse(Path(receipt['final']).exists());self.assertEqual(receipt['state'],'complete')
            self.assertNotIn('token=private',json.dumps(self.d.journal))
            with patch('documents.urllib.request.urlopen',side_effect=AssertionError('repeat download')):
                self.d.run([self.lesson])
    def test_unconfirmed_upload_retains_original_then_resumes(self):
        self.cloud.ready=False
        with self.assertRaisesRegex(RuntimeError,'incomplete'):self.transfer()
        receipt=next(iter(self.d.journal.values()));self.assertEqual(receipt['state'],'prepared');self.assertTrue(Path(receipt['final']).exists())
        self.cloud.ready=True
        with patch('documents.urllib.request.urlopen',side_effect=AssertionError('repeat download')):
            self.d.run([self.lesson])
        self.assertFalse(Path(receipt['final']).exists())
    def test_html_truncated_pdf_and_media_fail_without_receipt(self):
        for response in [Response(b'<html>login</html>','text/html'),Response(b'bad',length=50),Response(b'bad'),Response(PDF,'audio/mpeg')]:
            with patch('documents.urllib.request.urlopen',return_value=response),self.assertRaisesRegex(RuntimeError,'incomplete'):
                self.d.run([self.lesson])
            self.assertEqual(self.d.journal,{})
    def test_cancel_before_download_and_changed_identity_preserve_data(self):
        self.d.cancel()
        with self.assertRaisesRegex(RuntimeError,'cancelled'):self.d.one(self.lesson,self.resource)
        self.adapter.url_for.assert_not_called();self.d.stop.clear();self.transfer()
        changed=dict(self.resource,name='Other.pdf')
        with self.assertRaisesRegex(ValueError,'identity-mismatch'):self.d.one(self.lesson,changed)
    def test_duplicate_names_and_symlink_do_not_download(self):
        self.adapter.inventory.return_value=[self.resource,dict(self.resource,id='other')]
        with patch('documents.urllib.request.urlopen') as http,self.assertRaisesRegex(RuntimeError,'incomplete'):self.d.run([self.lesson])
        http.assert_not_called()
        outside=self.base/'outside';outside.mkdir();(self.d.root/'Course').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'unsafe-document-path'):self.d.paths(self.lesson,'file.pdf')
    def test_no_documents_and_audio_filter(self):
        self.adapter.inventory.return_value=[dict(self.resource,mime='audio/mpeg')]
        with patch('documents.urllib.request.urlopen') as http:self.assertEqual(self.d.run([self.lesson])['documents'],0)
        http.assert_not_called();self.assertTrue(next(iter(self.d.pages.values()))['inventory_complete'])
    def test_readonly_preflight_requires_native_confirmation(self):
        self.transfer();profile=dict(origin='https://alpha.example.test')
        before={p:p.read_bytes() for p in self.d.root.rglob('*') if p.is_file()}
        self.assertEqual(document_preflight([self.lesson],profile,self.d.root,self.d.destination,self.cloud)[0]['receipts'],1)
        self.assertEqual(before,{p:p.read_bytes() for p in self.d.root.rglob('*') if p.is_file()})
        self.cloud.ready=False
        with self.assertRaisesRegex(ValueError,'unconfirmed'):document_preflight([self.lesson],profile,self.d.root,self.d.destination,self.cloud)
    def test_changed_declared_size_rejected(self):
        self.transfer()
        with self.assertRaisesRegex(ValueError,'identity-mismatch'):self.d.one(self.lesson,dict(self.resource,size=99))
    def test_invalid_names_and_types(self):
        for raw in [dict(self.resource,name='../x.pdf'),dict(self.resource,mime=1),dict(self.resource,size=True),dict(self.resource,url='http://insecure/file')]:
            with self.assertRaises(ValueError):document_resource(raw)

    def test_cancel_during_stream_preserves_partial_without_publication(self):
        delivery=self.d
        class CancelResponse(Response):
            def read(self,n):
                chunk=super().read(n);delivery.cancel();return chunk
        with patch('documents.urllib.request.urlopen',return_value=CancelResponse()),self.assertRaisesRegex(RuntimeError,'cancelled'):
            self.d.one(self.lesson,self.resource)
        self.assertFalse(self.d.journal)
        self.assertFalse(self.d.destination.exists())
        self.assertTrue(list((self.d.root/'.document-staging').glob('*.partial')))

    def test_expired_session_stops_before_next_lesson(self):
        self.adapter.inventory.side_effect=RuntimeError('login-required')
        with self.assertRaisesRegex(RuntimeError,'login-required'):self.d.run([self.lesson,self.lesson])
        self.adapter.inventory.assert_called_once()
        self.assertEqual(json.loads((self.d.docs/'documents-status.json').read_text())['stage'],'failed')
