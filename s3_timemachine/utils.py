"""Utility functions for S3 TimeMachine."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

LOCK_TAG_KEY_PREFIX = "LockTime"
LOCK_TAG_VALUE_RE = re.compile(r"^\s*Locked until\s+(?P<ts>\S+)\s*$")


def parse_datetime(date_string: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> datetime | None:
    """Parse datetime string to datetime object.

    Args:
        date_string: String representation of date.
        fmt: Format string for parsing (default: YYYY-MM-DD HH:MM:SS).

    Returns:
        Datetime object or None if parsing fails.
    """
    try:
        return datetime.strptime(date_string, fmt)
    except ValueError as e:
        print(f"Error parsing date: {e}")
        return None


def parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 datetime. Returns None on failure.

    Ensures the returned datetime is timezone-aware (assumes UTC if naive).
    """
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_lock_tags(
    tags: Iterable[dict], now: datetime | None = None
) -> list[datetime]:
    """Extract non-expired lock times from a bucket tag set.

    Expects tags shaped like::

        Key:   ``LockTime<iso>``          ← the lock time (point-in-time snapshot)
        Value: ``Locked until <iso>``     ← the expiry time

    A tag is included only when its expiry (value timestamp) is still in the
    future relative to ``now``.  The returned timestamps are the **lock times**
    taken from the tag keys, not the expiry times.

    Args:
        tags: Iterable of tag dicts (TagSet entries from S3 GetBucketTagging).
        now: Reference "current" time used to filter expired tags.  Defaults
            to ``datetime.now(timezone.utc)``.

    Returns:
        Sorted list of unique, non-expired lock times.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    found: set[datetime] = set()
    for tag in tags:
        key = tag.get("Key", "")
        value = tag.get("Value", "")
        if not key.startswith(LOCK_TAG_KEY_PREFIX):
            continue
        # Parse the expiry from the value — used only for filtering.
        match = LOCK_TAG_VALUE_RE.match(value)
        if not match:
            continue
        lock_until = parse_iso(match.group("ts"))
        if lock_until is None or lock_until <= now:
            continue
        # Parse the lock time from the key.
        lock_time = parse_iso(key[len(LOCK_TAG_KEY_PREFIX) :])
        if lock_time is None:
            continue
        found.add(lock_time)

    return sorted(found)


def select_lock_time_for_target(
    lock_times: list[datetime], target: datetime
) -> datetime | None:
    """Return the most recent lock time that is at or before ``target``.

    Returns ``None`` when no lock time satisfies the constraint.
    """
    candidates = [t for t in lock_times if t <= target]
    return max(candidates) if candidates else None


def validate_bucket_name(bucket_name: str) -> bool:
    """Validate S3 bucket name format.

    Args:
        bucket_name: Bucket name to validate.

    Returns:
        True if valid, False otherwise.
    """
    if not bucket_name:
        return False
    if len(bucket_name) < 3 or len(bucket_name) > 63:
        return False
    if not all(c.islower() or c.isdigit() or c == "-" for c in bucket_name):
        return False
    if bucket_name.startswith("-") or bucket_name.endswith("-"):
        return False
    return True
