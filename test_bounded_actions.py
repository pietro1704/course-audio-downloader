import json
from pathlib import Path
import tempfile
import threading
import unittest

from bounded_actions import run_bounded


class BoundedActionsTests(unittest.TestCase):
    def test_real_independent_renames_overlap_and_keep_input_result_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = ['first', 'second', 'third', 'fourth']
            for key in items:
                (root / key).write_text(key)
            barrier = threading.Barrier(2)
            mutex = threading.Lock()
            active = maximum = 0
            receipts = root / 'receipts.json'
            receipts.write_text('{}')

            def operation(key):
                nonlocal active, maximum
                with mutex:
                    active += 1
                    maximum = max(maximum, active)
                barrier.wait(timeout=5)
                target = root / (key + '.moved')
                (root / key).rename(target)
                with mutex:
                    rows = json.loads(receipts.read_text())
                    rows[key] = str(target)
                    receipts.write_text(json.dumps(rows))
                    active -= 1
                return target.read_text()

            self.assertEqual(run_bounded(items, operation, identity=str, concurrency=2), items)
            self.assertEqual(maximum, 2)
            self.assertEqual(set(json.loads(receipts.read_text())), set(items))
            self.assertTrue(all(not (root / key).exists() for key in items))

    def test_failure_stops_admission_and_drains_other_active_receipt(self):
        barrier = threading.Barrier(2)
        drained = threading.Event()
        stop = threading.Event()
        calls = []

        def operation(key):
            calls.append(key)
            barrier.wait(timeout=5)
            if key == 'fails':
                raise RuntimeError('native-upload-failed')
            self.assertTrue(stop.wait(timeout=5))
            drained.set()
            return key

        with self.assertRaisesRegex(RuntimeError, 'native-upload-failed'):
            run_bounded(['fails', 'survives', 'never'], operation,
                        identity=str, concurrency=2, stop=stop)
        self.assertTrue(drained.is_set())
        self.assertEqual(set(calls), {'fails', 'survives'})

    def test_cancel_stops_next_admission_after_current_operation_finishes(self):
        stop = threading.Event()
        calls = []
        def operation(key):
            calls.append(key)
            stop.set()
            return 'durably-finished'
        with self.assertRaisesRegex(InterruptedError, 'operations-cancelled'):
            run_bounded(['one', 'two'], operation, identity=str, concurrency=1, stop=stop)
        self.assertEqual(calls, ['one'])

    def test_pre_cancelled_and_duplicate_identity_do_not_launch_operations(self):
        calls = []
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            run_bounded(['one'], calls.append, identity=str, stop=stop)
        with self.assertRaisesRegex(ValueError, 'duplicate-operation-identity'):
            run_bounded(['one', 'one'], calls.append, identity=str, concurrency=2)
        self.assertEqual(calls, [])

    def test_changed_identity_is_checked_before_mutating_later_item(self):
        items = [{'id': 'first'}, {'id': 'second'}]
        calls = []
        def operation(item):
            calls.append(item['id'])
            items[1]['id'] = 'replacement'
        with self.assertRaisesRegex(ValueError, 'operation-identity-changed'):
            run_bounded(items, operation, identity=lambda row: row['id'])
        self.assertEqual(calls, ['first'])

    def test_base_exception_drains_active_work_and_is_not_retried(self):
        calls = []
        def operation(key):
            calls.append(key)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run_bounded(['one', 'two'], operation, identity=str)
        self.assertEqual(calls, ['one'])

    def test_empty_work_and_invalid_boundaries(self):
        self.assertEqual(run_bounded([], lambda _: None, identity=str), [])
        for width in [0, 9, True, 1.5]:
            with self.subTest(width=width), self.assertRaises(ValueError):
                run_bounded([], lambda _: None, identity=str, concurrency=width)
        for key in ['', None, 1]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                run_bounded([key], lambda _: None, identity=lambda value: value)
