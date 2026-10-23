"""File-descriptor locks shared by the local CLI on Windows, Linux and macOS."""
import portalocker

LOCK_EX=portalocker.LOCK_EX
LOCK_NB=portalocker.LOCK_NB
LOCK_UN=portalocker.LOCK_UN


def flock(stream,flags):
    try:portalocker.lock(stream,flags)
    except portalocker.exceptions.LockException as error:
        raise BlockingIOError('another-run-is-active') from error
