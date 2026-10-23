"""Small native iCloud boundary; run with a Python containing PyObjC Foundation."""
import argparse
import json
from pathlib import Path


def resource_value(url, key):
    success, value, error = url.getResourceValue_forKey_error_(None, key, None)
    if not success or error is not None:
        raise RuntimeError('icloud-resource-query-failed')
    return value


def status(path, foundation=None):
    if foundation is None:
        import Foundation as foundation
    path = Path(path)
    url = foundation.NSURL.fileURLWithPath_(str(path))
    fields = {
        'ubiquitous': 'NSURLIsUbiquitousItemKey',
        'uploaded': 'NSURLUbiquitousItemIsUploadedKey',
        'uploading': 'NSURLUbiquitousItemIsUploadingKey',
        'conflicts': 'NSURLUbiquitousItemHasUnresolvedConflictsKey',
        'upload_error': 'NSURLUbiquitousItemUploadingErrorKey',
        'download_status': 'NSURLUbiquitousItemDownloadingStatusKey',
    }
    result = {}
    for name, key in fields.items():
        value = resource_value(url, getattr(foundation, key))
        if name == 'download_status':
            result[name] = str(value) if value is not None else None
        elif name == 'upload_error':
            result[name] = value is not None
        else:
            result[name] = bool(value) if value is not None else None
    stat = path.stat()
    result.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                  materialized=not bool(getattr(stat, 'st_flags', 0) & 0x40000000)
                  and result['download_status'] != 'NSURLUbiquitousItemDownloadingStatusNotDownloaded')
    return result


def uploaded(value):
    return (value.get('ubiquitous') is True and value.get('uploaded') is True
            and value.get('uploading') is False and value.get('conflicts') is False
            and value.get('upload_error') is False)


def evict(path):
    import Foundation
    if not uploaded(status(path, Foundation)):
        raise RuntimeError('icloud-upload-not-confirmed')
    success, error = Foundation.NSFileManager.defaultManager().evictUbiquitousItemAtURL_error_(
        Foundation.NSURL.fileURLWithPath_(str(path)), None)
    if not success or error is not None:
        raise RuntimeError('icloud-eviction-failed')
    return {'eviction_requested': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['status', 'evict'])
    parser.add_argument('path', type=Path)
    args = parser.parse_args(argv)
    try:
        result = status(args.path) if args.operation == 'status' else evict(args.path)
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'error': str(error) if isinstance(error, RuntimeError) else type(error).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
