"""A lock file so two ingest runs never overlap (EC-ING-21).

Created with O_EXCL, so only one process can hold it. A lock older than `stale_after_s`
is assumed to be left over from a crashed run and is taken over (an ingest takes seconds).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class IngestLockedError(RuntimeError):
    pass


@contextmanager
def ingest_lock(path: Path, stale_after_s: float = 30 * 60) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue  # released in the meantime; try again
            if attempt == 0 and age > stale_after_s:
                path.unlink(missing_ok=True)
                continue
            raise IngestLockedError(
                f"another ingest is running (lock file {path}). If none is, delete that file."
            ) from None
    else:
        raise IngestLockedError(f"could not acquire {path}")
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
