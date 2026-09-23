"""Crash-safe file writes for pipeline-produced artifacts.

Every run writes JSON/Markdown that the next job parses (articles.json,
milestones.json, feeds_health.json, router state ...). Writing in place can
leave a truncated or half-written file if the process dies mid-write, so the
payload is written to a sibling temp file, flushed + fsynced, then moved into
place with :func:`os.replace` — an atomic rename on the same filesystem (POSIX
and Windows). This mirrors the pattern already used by ``rate_limit.py`` for the
request budget, and centralizes it so every writer shares one implementation.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path


def atomic_write(path: Path | str, data: str | bytes, *, encoding: str = "utf-8") -> None:
    """Write ``data`` to ``path`` atomically (temp file + rename).

    The target's parent directory is created if missing. On any failure the
    temp file (and fd) are cleaned up and the original target is left intact.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode(encoding) if isinstance(data, str) else data

    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"short write to {tmp!s}")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        # mkstemp creates 0600; match what a plain write produces under umask.
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        with suppress(OSError):
            os.unlink(tmp)
        raise
