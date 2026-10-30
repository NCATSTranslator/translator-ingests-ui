#!/usr/bin/env python3
"""
Precompute folder statistics for the KGX Storage web server.

Run this script periodically (e.g., via cron every hour) to update folder metrics.
The web server reads metrics.json (always the latest successful run). Each run also
writes a timestamped copy metrics-YYYYMMDD-HHMMSS.json and keeps the three newest
snapshots; older snapshots are removed.

Usage:
    python compute_metrics.py
"""

import boto3
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from metrics_path_rules import exclude_key_for_folder_modified_date
from release_dates import DISPLAY_FORMAT, release_date_for_prefix

BUCKET_NAME = os.environ.get("BUCKET_NAME", "kgx-translator-ingests")
METRICS_FILE = Path(os.environ.get("METRICS_FILE", Path(__file__).parent / "metrics.json"))
S3_CLIENT = boto3.client("s3")

# Timestamped copies of metrics.json (metrics-YYYYMMDD-HHMMSS.json); keep newest only.
MAX_METRICS_ARCHIVES = 3
_METRICS_ARCHIVE_NAME = re.compile(r"^metrics-\d{8}-\d{6}\.json$")


def format_size(size_bytes):
    """Format bytes to human readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def get_folder_stats(prefix, verbose: bool = True):
    """Calculate total size and file count for a folder."""
    paginator = S3_CLIENT.get_paginator("list_objects_v2")
    total_size = 0
    file_count = 0
    latest_modified_all = None
    latest_modified_display = None

    try:
        for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                total_size += obj.get("Size", 0)
                file_count += 1
                lm = obj["LastModified"]
                if latest_modified_all is None or lm > latest_modified_all:
                    latest_modified_all = lm
                if not exclude_key_for_folder_modified_date(prefix, key):
                    if latest_modified_display is None or lm > latest_modified_display:
                        latest_modified_display = lm
    except Exception as e:
        # Only when asked: a background refresh of a bucket S3 is refusing would otherwise
        # put one line per folder into the log. compute_all_metrics reports the count.
        if verbose:
            print(f"Error processing {prefix}: {e}")
        return None

    chosen_modified = latest_modified_display or latest_modified_all
    last_modified = chosen_modified.strftime(DISPLAY_FORMAT) if chosen_modified else "-"

    return {
        "size": total_size,
        "size_display": format_size(total_size),
        "file_count": file_count,
        # A release folder reports the date its own metadata records, because
        # re-uploading an unchanged release moves LastModified to the re-upload.
        "modified": release_date_for_prefix(S3_CLIENT, BUCKET_NAME, prefix) or last_modified
    }


def discover_all_folders(max_depth=4):
    """Discover all folders in the bucket up to max_depth levels."""
    folders_to_process = [""]  # Start with root
    all_folders = set()

    while folders_to_process:
        prefix = folders_to_process.pop(0)
        depth = prefix.count("/")

        if depth >= max_depth:
            continue

        try:
            paginator = S3_CLIENT.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix, Delimiter="/"):
                # Get subfolders
                for prefix_obj in page.get("CommonPrefixes", []):
                    folder_path = prefix_obj["Prefix"]
                    all_folders.add(folder_path)
                    folders_to_process.append(folder_path)
        except Exception as e:
            print(f"Error discovering folders in {prefix}: {e}")
            continue

    return sorted(all_folders)


def _list_metrics_archives(parent: Path) -> list[Path]:
    """Return timestamped metrics snapshot paths, newest first."""
    found: list[Path] = []
    for path in parent.iterdir():
        if path.is_file() and _METRICS_ARCHIVE_NAME.match(path.name):
            found.append(path)
    return sorted(found, key=lambda p: p.name, reverse=True)


def archive_and_prune_metrics() -> Path | None:
    """
    Copy metrics.json to metrics-YYYYMMDD-HHMMSS.json and delete older archives.

    Keeps at most MAX_METRICS_ARCHIVES timestamped files (not counting metrics.json).
    Returns the new archive path, or None if metrics.json was missing.
    """
    if not METRICS_FILE.is_file():
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = METRICS_FILE.parent / f"metrics-{stamp}.json"
    # Staged under this process's own name, because the stamp is only accurate to the second:
    # two writers in the same second would otherwise copy into one file at once.
    staged = archive_path.with_name(f"{archive_path.name}.{os.getpid()}.staging")
    try:
        shutil.copy2(METRICS_FILE, staged)
        staged.replace(archive_path)
    finally:
        staged.unlink(missing_ok=True)

    parent = METRICS_FILE.parent
    archives = _list_metrics_archives(parent)
    for old in archives[MAX_METRICS_ARCHIVES:]:
        old.unlink(missing_ok=True)

    return archive_path


def compute_all_metrics(verbose: bool = True) -> dict[str, dict[str, object]]:
    """
    Compute metrics for all folders and save to JSON file.

    Pass verbose=False to leave out the per folder progress, which is worth
    reading when the script is run by hand and is only noise in the log of a
    server refreshing in the background.
    """
    print(f"Starting metric computation at {datetime.now()}")
    print(f"Bucket: {BUCKET_NAME}")

    # Discover all folders
    print("Discovering folders...")
    folders = discover_all_folders()
    print(f"Found {len(folders)} folders to process")

    # Compute metrics for each folder
    metrics = {}
    total = len(folders)

    for idx, folder in enumerate(folders, 1):
        if verbose:
            print(f"[{idx}/{total}] Processing: {folder}")
        stats = get_folder_stats(folder, verbose=verbose)
        if stats:
            metrics[folder] = stats

        # Progress update every 10 folders
        if verbose and idx % 10 == 0:
            print(f"Progress: {idx}/{total} ({idx/total*100:.1f}%)")

    failed = total - len(metrics)
    if failed:
        print(f"{failed} of {total} folders could not be scanned, so they are not in the cache. "
              f"Run compute_metrics.py by hand to see why.")

    # Add metadata
    output = {
        "computed_at": datetime.now().isoformat(),
        "bucket": BUCKET_NAME,
        "folder_count": len(metrics),
        "metrics": metrics
    }

    # Save to JSON file (atomic write). The temporary file is named after this process, because
    # the server refreshes in the background now: a run started by hand or by cron at the same
    # time would otherwise write into the same temporary file and publish a half written one.
    temp_file = METRICS_FILE.with_name(f"{METRICS_FILE.name}.{os.getpid()}.tmp")
    try:
        with open(temp_file, "w") as f:
            json.dump(output, f, indent=2)

        # Atomic rename
        temp_file.replace(METRICS_FILE)
    finally:
        temp_file.unlink(missing_ok=True)

    archived = archive_and_prune_metrics()
    if archived:
        print(f"Archived snapshot: {archived.name} (keeping up to {MAX_METRICS_ARCHIVES})")

    print(f"\nCompleted at {datetime.now()}")
    print(f"Metrics saved to: {METRICS_FILE}")
    print(f"Total folders processed: {len(metrics)}")

    return metrics


if __name__ == "__main__":
    start_time = time.time()
    compute_all_metrics()
    elapsed = time.time() - start_time
    print(f"Total time: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)")
