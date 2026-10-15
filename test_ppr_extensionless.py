import tempfile
import unittest
from pathlib import Path
from pipeline import Candidate, Pipeline
from ppr_cli import SafariAdapter

class ExtensionlessTests(unittest.TestCase):
    def test_named_mp3_download_control_accepts_extensionless_cdn(self):
        candidates=SafariAdapter.candidates({'links':[{'url':'https://cdn.example/opaque-id','label':'Áudio da aula (mp3)'},{'url':'https://cdn.example/movie','label':'Vídeo da aula'},{'url':'https://example/cursos','label':'Áudio'}]})
        self.assertEqual(candidates,[Candidate('https://cdn.example/opaque-id','mp3')])

    def test_extensionless_original_keeps_mp3_suffix_and_failure_preserves_bytes(self):
        class Adapter:
            def resolve(self,lesson):return [Candidate('https://cdn.example/opaque-id','mp3')]
            def download(self,candidate,destination):
                self.destination=destination
                destination.write_bytes(b'original invalid fixture')
                return destination
        with tempfile.TemporaryDirectory() as tmp:
            adapter=Adapter()
            result=Pipeline(tmp,adapter,reserve_bytes=0).one({'url':'https://padrepauloricardo.org/aulas/example','title':'Example'})
            self.assertEqual(result['status'],'failed')
            self.assertEqual(adapter.destination.suffix,'.mp3')
            sources=list(Path(tmp).rglob('*.mp3'))
            self.assertEqual(len(sources),1)
            self.assertEqual(sources[0].read_bytes(),b'original invalid fixture')

if __name__=='__main__':unittest.main()
