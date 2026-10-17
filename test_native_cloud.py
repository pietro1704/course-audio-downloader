import contextlib,importlib.util,subprocess,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
from cloud_delivery import NativeCloud


class NativeCloudTests(unittest.TestCase):
    def modules(self):
        return {'Foundation':types.ModuleType('Foundation'),'objc':types.SimpleNamespace(autorelease_pool=contextlib.nullcontext)}

    def test_matching_runtime_reuses_native_queries_and_eviction_without_processes(self):
        foundation=self.modules()
        with patch.dict(sys.modules,foundation),patch('cloud_delivery.subprocess.run',side_effect=AssertionError('unexpected process')):
            cloud=NativeCloud(sys.executable)
            with patch('icloud_bridge.status',return_value={'uploaded':False}) as status,patch('icloud_bridge.evict',return_value={'eviction_requested':True}) as evict:
                self.assertEqual(cloud.status(Path('pending')),{'uploaded':False})
                self.assertEqual(cloud.evict(Path('complete')),{'eviction_requested':True})
                status.assert_called_once_with(Path('pending'),foundation['Foundation']);evict.assert_called_once_with(Path('complete'))

    def test_native_failure_is_sanitized_and_cancellation_propagates(self):
        with patch.dict(sys.modules,self.modules()):cloud=NativeCloud(sys.executable)
        with patch('icloud_bridge.status',side_effect=OSError('PRIVATE path')):
            with self.assertRaisesRegex(RuntimeError,'^icloud-native-query-failed$'):cloud.status('pending')
        with patch('icloud_bridge.status',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):cloud.status('pending')

    def test_other_configured_python_keeps_its_process_boundary(self):
        with patch.dict(sys.modules,self.modules()):cloud=NativeCloud('/different/runtime/python')
        with patch('cloud_delivery.subprocess.run',return_value=subprocess.CompletedProcess([],0,'{"uploaded":false}','')) as run:
            self.assertEqual(cloud.status(Path('pending')),{'uploaded':False})
        self.assertEqual(run.call_args.args[0][0],'/different/runtime/python')

    def test_missing_foundation_retains_configured_bridge(self):
        with patch.dict(sys.modules,{'Foundation':None}):cloud=NativeCloud(sys.executable)
        with patch('cloud_delivery.subprocess.run',return_value=subprocess.CompletedProcess([],0,'{"uploaded":false}','')) as run:
            self.assertEqual(cloud.status(Path('pending')),{'uploaded':False})
        run.assert_called_once()

    @unittest.skipUnless(sys.platform=='darwin' and importlib.util.find_spec('Foundation') is not None,'requires native Foundation runtime')
    def test_real_foundation_resource_query_uses_the_same_native_runtime(self):
        import Foundation
        from icloud_bridge import resource_value,status
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'local-file';path.write_bytes(b'local')
            self.assertEqual(resource_value(Foundation.NSURL.fileURLWithPath_(str(path)),Foundation.NSURLFileSizeKey),5)
            cloud=NativeCloud(sys.executable);self.assertIsNotNone(cloud._native)
            try:expected=status(path)
            except RuntimeError:
                # Ubiquitous-item keys may be unavailable for an ordinary local file.
                with patch('cloud_delivery.subprocess.run',side_effect=AssertionError('unexpected process')):
                    with self.assertRaisesRegex(RuntimeError,'^icloud-native-query-failed$'):cloud.status(path)
            else:
                with patch('cloud_delivery.subprocess.run',side_effect=AssertionError('unexpected process')):self.assertEqual(cloud.status(path),expected)
