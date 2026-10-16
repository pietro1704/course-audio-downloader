import tempfile
import types
import unittest
from pathlib import Path

from icloud_bridge import resource_value, status, uploaded


class BridgeTests(unittest.TestCase):
    def test_query_success_is_not_the_resource_value(self):
        class URL:
            def getResourceValue_forKey_error_(self, output, key, error):
                return True, False, None
        self.assertIs(resource_value(URL(), 'uploaded'), False)

    def test_failed_query_is_not_treated_as_pending_or_uploaded(self):
        class URL:
            def getResourceValue_forKey_error_(self, output, key, error):
                return False, True, 'native error'
        with self.assertRaisesRegex(RuntimeError, 'icloud-resource-query-failed'):
            resource_value(URL(), 'uploaded')

    def test_missing_or_uploading_or_error_or_conflict_is_not_confirmed(self):
        good = dict(ubiquitous=True, uploaded=True, uploading=False, conflicts=False, upload_error=False)
        self.assertTrue(uploaded(good))
        for key in good:
            changed = dict(good)
            changed[key] = not changed[key]
            self.assertFalse(uploaded(changed), key)
        self.assertFalse(uploaded({}))


class NativeStatusTests(unittest.TestCase):
    def test_pending_upload_is_not_reported_as_uploaded(self):
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / 'sample.m4a'
            media.write_bytes(b'sample')
            keys = ['NSURLIsUbiquitousItemKey', 'NSURLUbiquitousItemIsUploadedKey',
                    'NSURLUbiquitousItemIsUploadingKey', 'NSURLUbiquitousItemHasUnresolvedConflictsKey',
                    'NSURLUbiquitousItemUploadingErrorKey', 'NSURLUbiquitousItemDownloadingStatusKey']
            module = types.ModuleType('Foundation')
            for key in keys:
                setattr(module, key, key)
            class URL:
                @staticmethod
                def fileURLWithPath_(path):
                    return URL()
                def getResourceValue_forKey_error_(self, output, key, error):
                    return True, (None if key.endswith('ErrorKey') else False), None
            module.NSURL = URL
            result = status(media, module)
            self.assertIs(result['uploaded'], False)
            self.assertIs(result['ubiquitous'], False)
            self.assertFalse(uploaded(result))


if __name__ == '__main__':
    unittest.main()
