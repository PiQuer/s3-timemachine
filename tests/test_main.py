"""Tests for main S3 TimeMachine functionality."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from s3_timemachine.main import (
    ObjectVersionRef,
    RestoreStatus,
    S3TimeMachine,
)


@pytest.fixture
def mock_s3_client():
    """Mock S3 client + resource constructed by boto3."""
    with patch("s3_timemachine.main.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_resource = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_boto3.resource.return_value = mock_resource
        yield mock_client


# -------------------------------------------------------------- construction


def test_s3_timemachine_init(mock_s3_client):
    machine = S3TimeMachine("test-bucket", "us-west-2")
    assert machine.bucket_name == "test-bucket"
    assert machine.region == "us-west-2"


def test_s3_timemachine_init_default_region(mock_s3_client):
    machine = S3TimeMachine("test-bucket")
    assert machine.region == "eu-west-1"


def test_s3_timemachine_init_destination(mock_s3_client):
    machine = S3TimeMachine("src", destination_bucket="dst")
    assert machine.destination_bucket == "dst"


# -------------------------------------------------------------- versioning


def test_get_bucket_versioning_enabled(mock_s3_client):
    mock_s3_client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    assert S3TimeMachine("test-bucket").get_bucket_versioning() is True


def test_get_bucket_versioning_disabled(mock_s3_client):
    mock_s3_client.get_bucket_versioning.return_value = {}
    assert S3TimeMachine("test-bucket").get_bucket_versioning() is False


# -------------------------------------------------------------- listing


def test_list_object_versions(mock_s3_client):
    paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = paginator
    paginator.paginate.return_value = [
        {
            "Versions": [
                {"Key": "file1.txt", "VersionId": "v1", "LastModified": datetime.now()}
            ]
        }
    ]

    versions = S3TimeMachine("test-bucket").list_object_versions()
    assert len(versions) == 1
    assert versions[0]["Key"] == "file1.txt"


def test_list_all_entries_returns_versions_and_markers(mock_s3_client):
    paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = paginator
    paginator.paginate.return_value = [
        {
            "Versions": [
                {"Key": "k", "VersionId": "v", "LastModified": datetime.now()}
            ],
            "DeleteMarkers": [
                {"Key": "k", "VersionId": "dm", "LastModified": datetime.now()}
            ],
        }
    ]

    versions, markers = S3TimeMachine("test-bucket").list_all_entries()
    assert len(versions) == 1 and len(markers) == 1


# -------------------------------------------------------------- versions_at


def _paginator_with(versions, markers, mock_s3_client):
    paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = paginator
    paginator.paginate.return_value = [{"Versions": versions, "DeleteMarkers": markers}]


def test_versions_at_picks_most_recent_version_before_target(mock_s3_client):
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2024, 6, 1, tzinfo=timezone.utc)
    t2 = datetime(2024, 12, 1, tzinfo=timezone.utc)
    target = datetime(2024, 9, 1, tzinfo=timezone.utc)

    _paginator_with(
        versions=[
            {
                "Key": "a.txt",
                "VersionId": "v-old",
                "LastModified": t0,
                "StorageClass": "STANDARD",
                "Size": 1,
            },
            {
                "Key": "a.txt",
                "VersionId": "v-mid",
                "LastModified": t1,
                "StorageClass": "STANDARD",
                "Size": 2,
            },
            {
                "Key": "a.txt",
                "VersionId": "v-future",
                "LastModified": t2,
                "StorageClass": "STANDARD",
                "Size": 3,
            },
        ],
        markers=[],
        mock_s3_client=mock_s3_client,
    )

    refs = S3TimeMachine("b").versions_at(target)
    assert len(refs) == 1
    assert refs[0].version_id == "v-mid"


def test_versions_at_skips_keys_deleted_at_target(mock_s3_client):
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2024, 6, 1, tzinfo=timezone.utc)
    target = datetime(2024, 9, 1, tzinfo=timezone.utc)

    _paginator_with(
        versions=[
            {
                "Key": "gone.txt",
                "VersionId": "v0",
                "LastModified": t0,
                "StorageClass": "STANDARD",
                "Size": 1,
            }
        ],
        markers=[{"Key": "gone.txt", "VersionId": "dm", "LastModified": t1}],
        mock_s3_client=mock_s3_client,
    )

    refs = S3TimeMachine("b").versions_at(target)
    assert refs == []


def test_versions_at_returns_glacier_class(mock_s3_client):
    target = datetime(2024, 12, 1, tzinfo=timezone.utc)
    _paginator_with(
        versions=[
            {
                "Key": "frozen.txt",
                "VersionId": "v",
                "LastModified": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "StorageClass": "DEEP_ARCHIVE",
                "Size": 10,
            }
        ],
        markers=[],
        mock_s3_client=mock_s3_client,
    )

    refs = S3TimeMachine("b").versions_at(target)
    assert refs[0].storage_class == "DEEP_ARCHIVE"


# -------------------------------------------------------------- restore status


def _ref(storage_class: str = "STANDARD") -> ObjectVersionRef:
    return ObjectVersionRef(
        key="k",
        version_id="v",
        storage_class=storage_class,
        last_modified=datetime.now(timezone.utc),
        size=0,
    )


def test_restore_status_non_archive_is_available(mock_s3_client):
    status = S3TimeMachine("b").get_restore_status(_ref("STANDARD"))
    assert status == RestoreStatus(
        available=True, ongoing=False, requires_restore=False
    )
    mock_s3_client.head_object.assert_not_called()


def test_restore_status_archive_not_yet_restored(mock_s3_client):
    mock_s3_client.head_object.return_value = {}
    status = S3TimeMachine("b").get_restore_status(_ref("DEEP_ARCHIVE"))
    assert status.requires_restore is True
    assert status.available is False


def test_restore_status_archive_ongoing(mock_s3_client):
    mock_s3_client.head_object.return_value = {"Restore": 'ongoing-request="true"'}
    status = S3TimeMachine("b").get_restore_status(_ref("GLACIER"))
    assert status.ongoing is True
    assert status.available is False


def test_restore_status_archive_complete(mock_s3_client):
    mock_s3_client.head_object.return_value = {
        "Restore": 'ongoing-request="false", expiry-date="Fri, 23 Dec 2099 00:00:00 GMT"'
    }
    status = S3TimeMachine("b").get_restore_status(_ref("GLACIER"))
    assert status.available is True


# -------------------------------------------------------------- ensure_restored


def test_ensure_restored_triggers_restore_request(mock_s3_client):
    mock_s3_client.head_object.return_value = {}
    archived = _ref("DEEP_ARCHIVE")

    ready, pending = S3TimeMachine("src").ensure_restored(
        [archived], tier="Bulk", days=5
    )

    assert ready == []
    assert pending == [archived]
    mock_s3_client.restore_object.assert_called_once_with(
        Bucket="src",
        Key="k",
        VersionId="v",
        RestoreRequest={"Days": 5, "GlacierJobParameters": {"Tier": "Bulk"}},
    )


def test_ensure_restored_skips_when_ongoing(mock_s3_client):
    mock_s3_client.head_object.return_value = {"Restore": 'ongoing-request="true"'}
    ready, pending = S3TimeMachine("src").ensure_restored(
        [_ref("GLACIER")], tier="Standard", days=1
    )
    assert ready == [] and len(pending) == 1
    mock_s3_client.restore_object.assert_not_called()


def test_ensure_restored_passes_standard_tier(mock_s3_client):
    standard = _ref("STANDARD")
    ready, pending = S3TimeMachine("src").ensure_restored(
        [standard], tier="Standard", days=1
    )
    assert ready == [standard] and pending == []


# -------------------------------------------------------------- copy


def test_copy_versions_to_destination(mock_s3_client):
    machine = S3TimeMachine("src", destination_bucket="dst")
    ref = _ref()
    copied = machine.copy_versions_to_destination([ref])
    assert copied == [ref]
    mock_s3_client.copy.assert_called_once_with(
        Bucket="dst",
        Key="k",
        CopySource={"Bucket": "src", "Key": "k", "VersionId": "v"},
    )


def test_copy_versions_requires_destination(mock_s3_client):
    machine = S3TimeMachine("src")
    with pytest.raises(ValueError):
        machine.copy_versions_to_destination([_ref()])


# -------------------------------------------------------------- run pipeline

# A lock time in the past (snapshot point) with a far-future expiry (not expired).
_LOCK_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
_LOCK_EXPIRY = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _mock_lock_tags(
    mock_s3_client,
    lock_time: datetime = _LOCK_TIME,
    expiry: datetime = _LOCK_EXPIRY,
) -> None:
    """Make get_bucket_tagging return a single non-expired lock tag."""
    mock_s3_client.get_bucket_tagging.return_value = {
        "TagSet": [
            {
                "Key": f"LockTime{lock_time.isoformat()}",
                "Value": f"Locked until {expiry.isoformat()}",
            }
        ]
    }


def test_run_no_lock_times(mock_s3_client):
    mock_s3_client.get_bucket_tagging.return_value = {"TagSet": []}
    result = S3TimeMachine("src", destination_bucket="dst").run()
    assert result["status"] == "no-lock-times"


def test_run_target_time_before_all_locks_returns_no_lock_times(mock_s3_client):
    # Target is earlier than the only available lock time.
    _mock_lock_tags(mock_s3_client, lock_time=datetime(2026, 6, 1, tzinfo=timezone.utc))
    _paginator_with(versions=[], markers=[], mock_s3_client=mock_s3_client)
    target = datetime(2025, 1, 1, tzinfo=timezone.utc)  # before 2026-06-01
    result = S3TimeMachine("src", destination_bucket="dst").run(
        target_time=target, prompt=False
    )
    assert result["status"] == "no-lock-times"


def test_run_selects_closest_lock_time_at_or_before_target(mock_s3_client):
    # Two lock times: 2026-01-01 and 2027-01-01.  Target 2026-06-01 → selects 2026-01-01.
    expiry = datetime(2099, 1, 1, tzinfo=timezone.utc)
    mock_s3_client.get_bucket_tagging.return_value = {
        "TagSet": [
            {
                "Key": "LockTime2026-01-01T00:00:00+00:00",
                "Value": f"Locked until {expiry.isoformat()}",
            },
            {
                "Key": "LockTime2027-01-01T00:00:00+00:00",
                "Value": f"Locked until {expiry.isoformat()}",
            },
        ]
    }
    _paginator_with(versions=[], markers=[], mock_s3_client=mock_s3_client)
    target = datetime(2026, 6, 1, tzinfo=timezone.utc)
    result = S3TimeMachine("src", destination_bucket="dst").run(
        target_time=target, prompt=False
    )
    assert result["status"] == "no-objects"
    assert result["target_time"] == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_run_copies_when_all_available(mock_s3_client):
    # target_time is after the lock time → lock time is selected.
    target = datetime(2030, 1, 1, tzinfo=timezone.utc)
    machine = S3TimeMachine("src", destination_bucket="dst")
    _mock_lock_tags(mock_s3_client)

    _paginator_with(
        versions=[
            {
                "Key": "a",
                "VersionId": "v1",
                "LastModified": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "StorageClass": "STANDARD",
                "Size": 1,
            }
        ],
        markers=[],
        mock_s3_client=mock_s3_client,
    )

    result = machine.run(target_time=target, prompt=False)
    assert result["status"] == "copied"
    assert len(result["copied"]) == 1
    # target_time in result is the selected lock time, not the raw arg.
    assert result["target_time"] == _LOCK_TIME
    mock_s3_client.copy.assert_called_once()


def test_run_pending_when_archive_not_ready(mock_s3_client):
    target = datetime(2030, 1, 1, tzinfo=timezone.utc)
    machine = S3TimeMachine("src", destination_bucket="dst")
    _mock_lock_tags(mock_s3_client)

    _paginator_with(
        versions=[
            {
                "Key": "a",
                "VersionId": "v1",
                "LastModified": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "StorageClass": "DEEP_ARCHIVE",
                "Size": 1,
            }
        ],
        markers=[],
        mock_s3_client=mock_s3_client,
    )
    mock_s3_client.head_object.return_value = {}

    result = machine.run(target_time=target, prompt=False, tier="Bulk", days=3)
    assert result["status"] == "pending"
    assert len(result["pending"]) == 1
    mock_s3_client.restore_object.assert_called_once()
    mock_s3_client.copy.assert_not_called()


def test_run_dry_run_no_mutations(mock_s3_client):
    target = datetime(2030, 1, 1, tzinfo=timezone.utc)
    machine = S3TimeMachine("src", destination_bucket="dst")
    _mock_lock_tags(mock_s3_client)

    _paginator_with(
        versions=[
            {
                "Key": "a",
                "VersionId": "v1",
                "LastModified": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "StorageClass": "DEEP_ARCHIVE",
                "Size": 1,
            },
            {
                "Key": "b",
                "VersionId": "v2",
                "LastModified": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "StorageClass": "STANDARD",
                "Size": 1,
            },
        ],
        markers=[],
        mock_s3_client=mock_s3_client,
    )
    mock_s3_client.head_object.return_value = {}

    result = machine.run(target_time=target, prompt=False, dry_run=True)
    # Pending because archived object would need restore.
    assert result["status"] == "pending"
    # No mutating calls in dry-run mode.
    mock_s3_client.restore_object.assert_not_called()
    mock_s3_client.copy.assert_not_called()


def test_run_dry_run_copies_logged_not_executed(mock_s3_client):
    target = datetime(2030, 1, 1, tzinfo=timezone.utc)
    machine = S3TimeMachine("src", destination_bucket="dst")
    _mock_lock_tags(mock_s3_client)

    _paginator_with(
        versions=[
            {
                "Key": "a",
                "VersionId": "v1",
                "LastModified": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "StorageClass": "STANDARD",
                "Size": 1,
            }
        ],
        markers=[],
        mock_s3_client=mock_s3_client,
    )

    result = machine.run(target_time=target, prompt=False, dry_run=True)
    assert result["status"] == "copied"
    assert result["dry_run"] is True
    assert len(result["copied"]) == 1
    mock_s3_client.copy.assert_not_called()


def test_initiate_restore_dry_run_skips_api(mock_s3_client):
    machine = S3TimeMachine("src")
    machine.initiate_restore(_ref("DEEP_ARCHIVE"), tier="Bulk", days=5, dry_run=True)
    mock_s3_client.restore_object.assert_not_called()


def test_copy_versions_dry_run_skips_api(mock_s3_client):
    machine = S3TimeMachine("src", destination_bucket="dst")
    copied = machine.copy_versions_to_destination([_ref()], dry_run=True)
    assert len(copied) == 1
    mock_s3_client.copy.assert_not_called()


def test_run_no_objects(mock_s3_client):
    target = datetime(2030, 1, 1, tzinfo=timezone.utc)
    machine = S3TimeMachine("src", destination_bucket="dst")
    _mock_lock_tags(mock_s3_client)
    _paginator_with(versions=[], markers=[], mock_s3_client=mock_s3_client)
    result = machine.run(target_time=target, prompt=False)
    assert result["status"] == "no-objects"


def test_run_prompts_for_target_time(mock_s3_client):
    # Lock time is 30 days from now; expiry is 60 days from now.
    lock_time = datetime.now(timezone.utc) + timedelta(days=30)
    expiry = datetime.now(timezone.utc) + timedelta(days=60)
    mock_s3_client.get_bucket_tagging.return_value = {
        "TagSet": [
            {
                "Key": f"LockTime{lock_time.isoformat()}",
                "Value": f"Locked until {expiry.isoformat()}",
            }
        ]
    }
    _paginator_with(versions=[], markers=[], mock_s3_client=mock_s3_client)

    machine = S3TimeMachine("src", destination_bucket="dst")
    with patch("builtins.input", return_value="1"):
        result = machine.run()
    # User selected the only available lock time.
    assert result["status"] == "no-objects"
    assert result["target_time"] == lock_time
