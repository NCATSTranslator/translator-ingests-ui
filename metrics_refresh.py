"""
Keep metrics.json fresh without anyone having to install a cron job.

compute_metrics.py writes the folder statistics the listing reads, and nothing
in the container image runs it, so a deployment serves whatever statistics were
computed elsewhere, or none at all. The web server starts one background
refresher that recomputes them once they are older than the refresh interval.

Gunicorn runs several workers and every worker imports the app, so the
refresher takes a lock file and only the worker holding it recomputes. The
workers that do not hold it pick up the new file the next time they serve a
listing, because the server reloads metrics.json whenever its mtime changes.
That also covers a metrics.json written by cron or by hand.

Set METRICS_REFRESH_SECONDS to 0 to turn the refresher off, for example on a
host where cron already refreshes the file.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from compute_metrics import compute_all_metrics

DEFAULT_REFRESH_SECONDS = 6 * 60 * 60
ENV_REFRESH_SECONDS = "METRICS_REFRESH_SECONDS"

# A refresh killed before it released the lock must not block every later one.
LOCK_STALE_SECONDS = 2 * 60 * 60
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


def claim_refresh(lock_path: Path, now: float) -> bool:
    """
    Return True if this process may refresh, taking over a lock left behind stale.

    The lock is a file created exclusively, so exactly one process wins the race.

    >>> import tempfile, time
    >>> with tempfile.TemporaryDirectory() as folder:
    ...     lock = Path(folder) / ("metrics.json" + LOCK_SUFFIX)
    ...     mine = claim_refresh(lock, time.time())
    ...     theirs = claim_refresh(lock, time.time())
    ...     (mine, theirs)
    (True, False)

    A lock older than LOCK_STALE_SECONDS is taken over, so a refresh that was
    killed before it released the lock cannot block every refresh after it.

    >>> with tempfile.TemporaryDirectory() as folder:
    ...     lock = Path(folder) / ("metrics.json" + LOCK_SUFFIX)
    ...     held = claim_refresh(lock, time.time())
    ...     os.utime(lock, (0, 0))
    ...     claim_refresh(lock, time.time())  # doctest: +ELLIPSIS
    Taking over a metrics refresh lock left behind ...s ago
    True
    """
    try:
        os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        pass
    except OSError as error:
        print(f"Cannot write the metrics refresh lock at {lock_path}: {error}")
        return False

    age = file_age_seconds(lock_path, now)
    if age is None or age < LOCK_STALE_SECONDS:
        return False
    print(f"Taking over a metrics refresh lock left behind {age:.0f}s ago")
    lock_path.unlink(missing_ok=True)
    try:
        os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        return False
    return True


def _refresh_when_stale(metrics_file: Path, lock_path: Path, interval_seconds: int) -> None:
    """Recompute the statistics once, if they are stale and no other worker is on it."""
    if not needs_refresh(file_age_seconds(metrics_file, time.time()), interval_seconds):
        return
    if not claim_refresh(lock_path, time.time()):
        return
    try:
        print(f"Refreshing {metrics_file} because it is older than {interval_seconds}s")
        compute_all_metrics(verbose=False)
    finally:
        lock_path.unlink(missing_ok=True)


def _refresh_loop(metrics_file: Path, lock_path: Path, interval_seconds: int) -> None:
    """Check the age of the statistics forever, recomputing them when they age out."""
    while True:
        _refresh_when_stale(metrics_file, lock_path, interval_seconds)
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
