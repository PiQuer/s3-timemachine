"""Tests for utility functions."""

from datetime import datetime, timedelta, timezone

from s3_timemachine.utils import (
    parse_datetime,
    parse_iso,
    parse_lock_tags,
    select_lock_time_for_target,
    validate_bucket_name,
)


class TestParseDateTime:
    """Tests for parse_datetime function."""

    def test_parse_valid_datetime(self):
        """Test parsing valid datetime string."""
        result = parse_datetime("2024-01-15 10:30:45")
        assert result == datetime(2024, 1, 15, 10, 30, 45)

    def test_parse_invalid_datetime(self):
        """Test parsing invalid datetime string."""
        result = parse_datetime("invalid-date")
        assert result is None

    def test_parse_custom_format(self):
        """Test parsing with custom format."""
        result = parse_datetime("15/01/2024", fmt="%d/%m/%Y")
        assert result == datetime(2024, 1, 15)


class TestValidateBucketName:
    """Tests for validate_bucket_name function."""

    def test_valid_bucket_name(self):
        """Test valid bucket name."""
        assert validate_bucket_name("my-bucket-123") is True

    def test_bucket_name_too_short(self):
        """Test bucket name that is too short."""
        assert validate_bucket_name("ab") is False

    def test_bucket_name_too_long(self):
        """Test bucket name that is too long."""
        assert validate_bucket_name("a" * 64) is False

    def test_bucket_name_uppercase(self):
        """Test bucket name with uppercase letters."""
        assert validate_bucket_name("MyBucket") is False

    def test_bucket_name_starts_with_dash(self):
        """Test bucket name starting with dash."""
        assert validate_bucket_name("-mybucket") is False

    def test_bucket_name_ends_with_dash(self):
        """Test bucket name ending with dash."""
        assert validate_bucket_name("mybucket-") is False

    def test_bucket_name_empty(self):
        """Test empty bucket name."""
        assert validate_bucket_name("") is False


class TestParseIso:
    def test_parse_iso_with_tz(self):
        dt = parse_iso("2026-04-16T19:07:35.427621+00:00")
        assert dt == datetime(2026, 4, 16, 19, 7, 35, 427621, tzinfo=timezone.utc)

    def test_parse_iso_naive_assumes_utc(self):
        dt = parse_iso("2026-04-16T19:07:35")
        assert dt is not None and dt.tzinfo == timezone.utc

    def test_parse_iso_invalid(self):
        assert parse_iso("nope") is None


class TestParseLockTags:
    def test_returns_key_timestamps_not_value(self):
        """Lock times come from the key, not the value."""
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        tags = [
            {
                "Key": "LockTime2026-04-16T19:07:35.427621+00:00",
                "Value": "Locked until 2026-05-16T19:07:35.427621+00:00",
            },
            {
                "Key": "LockTime2026-04-19T19:07:35.098821+00:00",
                "Value": "Locked until 2026-05-19T19:07:35.098821+00:00",
            },
        ]
        result = parse_lock_tags(tags, now=now)
        # Should be the KEY timestamps, not the value timestamps.
        assert result == [
            datetime(2026, 4, 16, 19, 7, 35, 427621, tzinfo=timezone.utc),
            datetime(2026, 4, 19, 19, 7, 35, 98821, tzinfo=timezone.utc),
        ]

    def test_skips_expired_locks(self):
        """Tags whose value expiry is in the past are excluded."""
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        tags = [
            {
                "Key": "LockTime2026-04-16T19:07:35+00:00",
                "Value": "Locked until 2026-05-16T19:07:35+00:00",
            },
        ]
        assert parse_lock_tags(tags, now=now) == []

    def test_ignores_unrelated_tags(self):
        lock_time = datetime.now(timezone.utc) + timedelta(days=10)
        expiry = datetime.now(timezone.utc) + timedelta(days=30)
        tags = [
            {"Key": "Owner", "Value": "team-a"},
            {"Key": "LockTimeWhatever", "Value": "not a lock value"},
            {
                "Key": f"LockTime{lock_time.isoformat()}",
                "Value": f"Locked until {expiry.isoformat()}",
            },
        ]
        assert parse_lock_tags(tags) == [lock_time]

    def test_deduplicates(self):
        lock_time = datetime.now(timezone.utc) + timedelta(days=10)
        expiry = datetime.now(timezone.utc) + timedelta(days=30)
        tags = [
            {
                "Key": f"LockTime{lock_time.isoformat()}",
                "Value": f"Locked until {expiry.isoformat()}",
            },
            {
                "Key": f"LockTime{lock_time.isoformat()}",
                "Value": f"Locked until {expiry.isoformat()}",
            },
        ]
        assert parse_lock_tags(tags) == [lock_time]


class TestSelectLockTimeForTarget:
    t1: datetime
    t2: datetime
    t3: datetime
    lock_times: list[datetime]

    def setup_method(self) -> None:
        self.t1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.t2 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.t3 = datetime(2027, 1, 1, tzinfo=timezone.utc)
        self.lock_times = [self.t1, self.t2, self.t3]

    def test_returns_closest_at_or_before(self):
        target = datetime(2026, 6, 1, tzinfo=timezone.utc)
        assert select_lock_time_for_target(self.lock_times, target) == self.t2

    def test_exact_match(self):
        assert select_lock_time_for_target(self.lock_times, self.t2) == self.t2

    def test_target_before_all_returns_none(self):
        target = datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert select_lock_time_for_target(self.lock_times, target) is None

    def test_target_after_all_returns_latest(self):
        target = datetime(2030, 1, 1, tzinfo=timezone.utc)
        assert select_lock_time_for_target(self.lock_times, target) == self.t3

    def test_empty_list_returns_none(self):
        assert (
            select_lock_time_for_target([], datetime(2025, 1, 1, tzinfo=timezone.utc))
            is None
        )
