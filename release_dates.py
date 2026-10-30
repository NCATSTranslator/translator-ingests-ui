"""
Real release dates for release folders, read from the release's own metadata.

S3 has no directory mtime, so the listing derives a folder's date from the
LastModified of the objects under it. S3 sets LastModified on every PUT, even
when the bytes are identical, so re-uploading a release that is already
published moves that date to the day of the re-upload. A release records when
it was really made, in release-metadata.json and graph-metadata.json, so a
release folder takes its displayed date from that metadata and falls back to
LastModified only when the release records no date at all.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

RELEASE_METADATA_FILENAME = "release-metadata.json"
GRAPH_METADATA_FILENAME = "graph-metadata.json"

# The date each file records the release under, in order of preference.
_METADATA_DATE_FIELDS: tuple[tuple[str, str], ...] = (
    (RELEASE_METADATA_FILENAME, "release_date"),
    (GRAPH_METADATA_FILENAME, "dateCreated"),
)

# releases/<graph>/<version>/, the only folders that hold a single release.
_RELEASE_VERSION_PREFIX = re.compile(r"^releases/[^/]+/([^/]+)/$")

# Releases made before semantic versioning are named after their date.
DATE_VERSION_FORMAT = "%Y_%m_%d"

DISPLAY_FORMAT = "%Y-%m-%d %H:%M"
DATE_ONLY_FORMAT = "%Y-%m-%d"


def release_version_from_prefix(prefix: str) -> str | None:
    """
    Return the version named by a release folder prefix, or None for any other prefix.

    >>> release_version_from_prefix("releases/gtopdb/1.0.0/")
    '1.0.0'
    >>> release_version_from_prefix("releases/translator_kg/2026_07_05/")
    '2026_07_05'
    >>> release_version_from_prefix("releases/gtopdb/") is None
    True
    >>> release_version_from_prefix("data/gtopdb/1.0.0/") is None
    True
    """
    match = _RELEASE_VERSION_PREFIX.match(prefix)
    return match.group(1) if match else None


def format_release_date(value: str | None) -> str | None:
    """
    Return a recorded date formatted for the listing, or None if it holds no date.

    Accepts the ISO 8601 timestamps the pipeline writes and the YYYY_MM_DD form
    that releases made before semantic versioning are named after.

    >>> format_release_date("2026-09-04T22:58:48Z")
    '2026-09-04 22:58'
    >>> format_release_date("2026_07_05")
    '2026-07-05'

    A date that no calendar has holds no date, so it falls through to the next
    place the release records one rather than being displayed as it stands.

    >>> format_release_date("1.0.0") is None
    True
    >>> format_release_date("2026_99_99") is None
    True
    >>> format_release_date("2026_02_30") is None
    True
    >>> format_release_date("") is None
    True
    >>> format_release_date(None) is None
    True
    """
    if not value:
        return None
    try:
        return datetime.strptime(value, DATE_VERSION_FORMAT).strftime(DATE_ONLY_FORMAT)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value).strftime(DISPLAY_FORMAT)
    except ValueError:
        return None


def release_date_from_metadata(metadata_json: bytes | str | None, field: str) -> str | None:
    """
    Return the date recorded in field of a metadata file, or None if there is none.

    >>> release_date_from_metadata('{"release_date": "2026-09-04T22:58:48Z"}', "release_date")
    '2026-09-04 22:58'
    >>> release_date_from_metadata('{"dateCreated": ""}', "dateCreated") is None
    True
    >>> release_date_from_metadata('{}', "release_date") is None
    True
    >>> release_date_from_metadata(None, "release_date") is None
    True
    >>> release_date_from_metadata("not json", "release_date") is None
    True
    >>> release_date_from_metadata('{"release_date": 20260904}', "release_date") is None
    True
    """
    if metadata_json is None:
        return None
    try:
        metadata = json.loads(metadata_json)
    except ValueError:
        return None
    if not isinstance(metadata, dict):
        return None
    # Metadata is whatever the file holds, so a field that is not text records no date.
    recorded = metadata.get(field)
    return format_release_date(recorded) if isinstance(recorded, str) else None


def _read_object(s3_client: BaseClient, bucket: str, key: str) -> bytes | None:
    """
    Return the body of an S3 object, or None if it cannot be read.

    Reading the body is part of reading the object, because the request can
    succeed and the body still arrive short. Either way the caller has no
    metadata from this file and goes on to the next place a date is recorded.

    >>> class TruncatedBody:
    ...     def read(self):
    ...         raise BotoCoreError()
    >>> class Truncated:
    ...     def get_object(self, Bucket, Key):
    ...         return {"Body": TruncatedBody()}
    >>> _read_object(Truncated(), "bucket", "key") is None
    True
    """
    try:
        # boto3 generates client methods at runtime, so type checkers cannot see get_object.
        response = s3_client.get_object(Bucket=bucket, Key=key)  # type: ignore[attr-defined]
        body: bytes = response["Body"].read()
    except (ClientError, BotoCoreError):
        return None
    return body


def release_date_for_prefix(s3_client: BaseClient, bucket: str, prefix: str) -> str | None:
    """
    Return the real date of the release held in prefix, or None if there is none.

    Returns None for any prefix that is not a single release, so callers keep
    using LastModified everywhere else. A release named after its date reports
    that date even when its metadata is missing.
    """
    version = release_version_from_prefix(prefix)
    if version is None:
        return None
    for filename, field in _METADATA_DATE_FIELDS:
        metadata_json = _read_object(s3_client, bucket, prefix + filename)
        recorded = release_date_from_metadata(metadata_json, field)
        if recorded:
            return recorded
    return format_release_date(version)
