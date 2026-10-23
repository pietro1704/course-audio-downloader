"""Bound independent, durable operations without retrying or cancelling active work.

The caller owns persistence and operation identity. Validate all identities before
starting, admit at most the configured width, and drain in-flight operations on
failure/cancellation so their caller-owned receipts can be committed safely.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import threading


def run_bounded(items, operation, *, identity, concurrency=1, stop=None):
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError('invalid-operation-concurrency')
    items = list(items)
    keys = [identity(item) for item in items]
    if any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError('invalid-or-duplicate-operation-identity')
    stop = stop if stop is not None else threading.Event()
    if stop.is_set():
        raise InterruptedError('operations-cancelled')
    results = [None] * len(items)
    iterator = iter(enumerate(items))
    failure = None
    pending = {}
    pool = ThreadPoolExecutor(max_workers=concurrency)

    def invoke(index, item):
        if identity(item) != keys[index]:
            raise ValueError('operation-identity-changed')
        return operation(item)

    def admit():
        while failure is None and not stop.is_set() and len(pending) < concurrency:
            try:
                index, item = next(iterator)
            except StopIteration:
                return
            pending[pool.submit(invoke, index, item)] = index

    try:
        admit()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                try:
                    results[index] = future.result()
                except BaseException as error:
                    if failure is None:
                        failure = error
                        stop.set()
            admit()
    except BaseException:
        stop.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if failure is not None:
        raise failure
    if stop.is_set():
        raise InterruptedError('operations-cancelled')
    return results
