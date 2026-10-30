"""
Keep metrics.json fresh without anyone having to install a cron job.

compute_metrics.py writes the folder statistics the listing reads, and nothing
in the container image runs it, so a deployment serves whatever statistics were
computed elsewhere, or none at all. The web server starts one background
refresher that recomputes them once they are older than the refresh interval.

Gunicorn runs several workers and every worker imports the app, so the
refresher locks a file and only the worker holding the lock recomputes. The
workers that do not hold it pick up the new file the next time they serve a
listing, because the server reloads metrics.json whenever its mtime changes.
That also covers a metrics.json written by cron or by hand.

Set METRICS_REFRESH_SECONDS to 0 to turn the refresher off, for example on a
host where cron already refreshes the file.
"""

from __future__ import annotations

import fcntl
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from compute_metrics import compute_all_metrics

DEFAULT_REFRESH_SECONDS = 6 * 60 * 60
ENV_REFRESH_SECONDS = "METRICS_REFRESH_SECONDS"

LOCK_SUFFIX = ".refresh.lock"

# How often the refresher wakes up to compare the file's age against the interval.
POLL_SECONDS = 60


def refresh_seconds(environ: Mapping[str, str]) -> int:
    """
    Return the refresh interval in seconds, or 0 when refreshing is turned off.

    A value that is not a whole number of seconds falls back to the default
    rather than stopping the server, because this knob is optional and a typo
    in it should not take the site down.

    >>> refresh_seconds({})
    21600
    >>> refresh_seconds({"METRICS_REFRESH_SECONDS": "900"})
    900
    >>> refresh_seconds({"METRICS_REFRESH_SECONDS": "0"})
    0
    >>> refresh_seconds({"METRICS_REFRESH_SECONDS": "-5"})
    0
    >>> refresh_seconds({"METRICS_REFRESH_SECONDS": "hourly"})
    Ignoring METRICS_REFRESH_SECONDS='hourly', expected a whole number of seconds
    21600
    """
    raw = environ.get(ENV_REFRESH_SECONDS)
    if raw is None:
        return DEFAULT_REFRESH_SECONDS
    try:
        seconds = int(raw)
    except ValueError:
        print(f"Ignoring {ENV_REFRESH_SECONDS}={raw!r}, expected a whole number of seconds")
        return DEFAULT_REFRESH_SECONDS
    return max(seconds, 0)


def needs_refresh(age_seconds: float | None, interval_seconds: int) -> bool:
    """
    Return True when the statistics are missing or older than the interval.

    >>> needs_refresh(None, 3600)
    True
    >>> needs_refresh(7200.0, 3600)
    True
    >>> needs_refresh(60.0, 3600)
    False
    >>> needs_refresh(None, 0)
    False
    """
    if interval_seconds <= 0:
        return False
    if age_seconds is None:
        return True
    return age_seconds >= interval_seconds


def file_age_seconds(path: Path, now: float) -> float | None:
    """Return how long ago path was last written, or None if it is not there."""
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


@contextmanager
def refresh_lock(lock_path: Path) -> Iterator[bool]:
    """
    Hold the refresh lock for the length of the block, or yield False if a worker already holds it.

    The lock is an advisory lock on the file rather than the existence of the
    file, so the kernel drops it when the process holding it exits, however it
    exits. A lock file left behind by a killed worker therefore blocks nothing,
    and a refresh that takes longer than expected keeps its lock for as long as
    it runs instead of having it taken away.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as folder:
    ...     lock = Path(folder) / ("metrics.json" + LOCK_SUFFIX)
    ...     with refresh_lock(lock) as mine, refresh_lock(lock) as theirs:
    ...         (mine, theirs)
    (True, False)

    Leaving the block releases the lock, so the next refresh can take it.

    >>> with tempfile.TemporaryDirectory() as folder:
    ...     lock = Path(folder) / ("metrics.json" + LOCK_SUFFIX)
    ...     with refresh_lock(lock) as first:
    ...         pass
    ...     with refresh_lock(lock) as second:
    ...         (first, second)
    (True, True)
    """
    try:
        lock_file = lock_path.open("w")
    except OSError as error:
        print(f"Cannot write the metrics refresh lock at {lock_path}: {error}")
        yield False
        return
    with lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _refresh_when_stale(metrics_file: Path, lock_path: Path, interval_seconds: int) -> None:
    """Recompute the statistics once, if they are stale and no other worker is on it."""
    if not needs_refresh(file_age_seconds(metrics_file, time.time()), interval_seconds):
        return
    with refresh_lock(lock_path) as locked:
        if not locked:
            return
        print(f"Refreshing {metrics_file} because it is older than {interval_seconds}s")
        compute_all_metrics(verbose=False)


def _refresh_loop(metrics_file: Path, lock_path: Path, interval_seconds: int) -> None:
    """Check the age of the statistics forever, recomputing them when they age out."""
    while True:
        try:
            _refresh_when_stale(metrics_file, lock_path, interval_seconds)
        # Broad on purpose: a refresher that dies never refreshes again, so one unreadable
        # metadata file or one S3 error must not stop every refresh after it.
        except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            print(f"Metrics refresh failed, trying again in {POLL_SECONDS}s: {error!r}")
        time.sleep(POLL_SECONDS)


def start_refresher(metrics_file: Path, interval_seconds: int) -> threading.Thread | None:
    """
    Start the background refresher, or return None when refreshing is turned off.
    """
    if interval_seconds <= 0:
        print("Metrics refresh is off, metrics.json will only change if something else writes it")
        return None
    lock_path = metrics_file.with_name(metrics_file.name + LOCK_SUFFIX)
    thread = threading.Thread(
        target=_refresh_loop,
        args=(metrics_file, lock_path, interval_seconds),
        name="metrics-refresher",
        daemon=True,
    )
    thread.start()
    print(f"Metrics refresh is on, recomputing when metrics.json is older than {interval_seconds}s")
    return thread
