# s3-timemachine

Restore a versioned S3 bucket to a specific point in time.

Given a source bucket with [versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html) enabled and a set of *lock tags* that record when snapshots were taken (e.g., by applying compliance locks on all current versions of the bucket), `s3-timemachine` identifies which object version was current at that moment and copies it into a destination bucket — effectively rewinding the bucket to that instant.

A second `shorten` sub-command lets you cut short the restore window of objects that were previously thawed from Glacier storage, stopping unnecessary Standard-storage billing without touching anything else.

---

## How it works

### Lock tags

The tool discovers available restore points from the bucket's tag set. Each tag must follow this convention:

| Part  | Format                             | Meaning                                   |
|-------|------------------------------------|-------------------------------------------|
| Key   | `LockTime<ISO-8601 datetime>`      | The point-in-time snapshot to restore to  |
| Value | `Locked until <ISO-8601 datetime>` | When the lock expires (used for filtering) |

Example:

```/dev/null/example.txt#L1-2
Key:   LockTime2026-01-15T12:00:00+00:00
Value: Locked until 2099-01-01T00:00:00+00:00
```

Tags whose expiry has already passed are silently ignored.

### Version selection

For each object key, `s3-timemachine` collects all versions and delete markers whose `LastModified` timestamp is at or before the chosen lock time, then picks the most recent of those. If that entry is a delete marker the object is considered deleted at that point and is excluded from the restore set.

### Glacier / Deep Archive objects

Objects in `GLACIER` or `DEEP_ARCHIVE` storage must be thawed before they can be copied. The tool automatically:

1. Checks the current restore status of every archived object.
2. Initiates a restore request for any that have not yet been thawed.
3. Exits with status `pending` if any objects are still being restored, printing the list so you can re-run once they are ready.

---

## Installation

Requires **Python ≥ 3.10**.

```/dev/null/sh#L1-4
git clone <repo-url>
cd s3-timemachine
pip install .          # or: poetry install
```

AWS credentials must be available in the environment via the standard boto3 credential chain (environment variables, `~/.aws/credentials`, IAM role, etc.).

---

## Usage

### `restore` — copy objects to a destination bucket

```/dev/null/sh#L1-8
s3-timemachine restore \
  --source-bucket      my-source-bucket \
  --destination-bucket my-restore-bucket \
  [--target-time       2026-01-15T12:00:00Z] \
  [--days              7] \
  [--tier              Standard|Bulk] \
  [--max-workers       10] \
  [--dry-run] [--debug]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--source-bucket` | *(required)* | Versioned bucket to restore from. |
| `--destination-bucket` | *(required)* | Bucket to write restored objects into. |
| `--target-time` | interactive | ISO-8601 timestamp to restore to. If omitted, available lock times are listed for selection. |
| `--days` | `7` | Glacier restore-window length in days. |
| `--tier` | `Standard` | Glacier retrieval tier (`Standard` or `Bulk`). |
| `--max-workers` | `10` | Parallel copy threads. |
| `--dry-run` | off | Log intended actions without making any S3 calls. |
| `--debug` | off | Enable debug-level logging. |
| `--region` | `eu-west-1` | AWS region. |

**Exit codes:** `0` = success or nothing to do; `1` = objects still pending Glacier restore (re-run later); `2` = invalid arguments.

#### Example

```/dev/null/sh#L1-6
# Dry-run first to preview what would be copied
s3-timemachine restore \
  --source-bucket  prod-data \
  --destination-bucket prod-data-restored \
  --target-time 2026-01-15T12:00:00Z \
  --dry-run
```

```/dev/null/sh#L1-5
# Real run
s3-timemachine restore \
  --source-bucket  prod-data \
  --destination-bucket prod-data-restored \
  --target-time 2026-01-15T12:00:00Z
```

A progress bar is shown while objects are being copied:

```/dev/null/example.txt#L1-4
2026-05-15 10:00:01 INFO s3_timemachine: Found 1 843 object versions to restore.
2026-05-15 10:00:01 INFO s3_timemachine: Copying s3://prod-data/reports/q1.parquet (version aB3x…) -> s3://prod-data-restored/reports/q1.parquet
Copying:  37%|████████████▌                    | 681/1843 [00:42<01:14, 15.6obj/s]
```

---

### `shorten` — reduce the restore window of thawed objects

When objects were restored from Glacier with a long retention window (e.g. 30 days) but are no longer needed, you can shorten the remaining window to stop paying for Standard-storage billing before it expires naturally.

```/dev/null/sh#L1-7
s3-timemachine shorten \
  --source-bucket  my-source-bucket \
  [--target-time   2026-01-15T12:00:00Z] \
  [--days          1] \
  [--dry-run] [--debug]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--source-bucket` | *(required)* | Bucket containing the restored objects. |
| `--target-time` | interactive | Same lock-time selection as `restore`. |
| `--days` | `1` | New restore-window length (use `1` to expire ASAP). |
| `--dry-run` | off | Log intended actions without making any S3 calls. |
| `--debug` | off | Enable debug-level logging. |
| `--region` | `eu-west-1` | AWS region. |

**Important:** `shorten` never initiates a new restore. Objects that have not yet been thawed (or whose restore is still in progress) are skipped with a warning. No destination bucket is needed or accepted.

#### Example

```/dev/null/sh#L1-5
s3-timemachine shorten \
  --source-bucket prod-data \
  --target-time   2026-01-15T12:00:00Z \
  --days          1 \
  --dry-run
```

---

## Using as a library

```/dev/null/example.py#L1-30
from datetime import datetime, timezone
from s3_timemachine.main import S3TimeMachine

machine = S3TimeMachine(
    bucket_name="prod-data",
    destination_bucket="prod-data-restored",
    region="eu-west-1",
)

# Full restore pipeline
result = machine.run(
    target_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
    tier="Standard",
    days=7,
    dry_run=False,
)
# result["status"]: "copied" | "pending" | "no-objects" | "no-lock-times"

# Shorten restore window only
result = machine.run_shorten(
    target_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
    days=1,
)
# result["status"]: "shortened" | "no-objects" | "no-lock-times"

# Lower-level: work with refs directly
refs = machine.versions_at(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc))
shortened, skipped = machine.shorten_restore_retention(refs, days=1)
```

---

## Development

```/dev/null/sh#L1-6
poetry install
poetry run pytest          # run tests with coverage
poetry run pytest -x -q    # stop on first failure, minimal output
```

Tests use `unittest.mock` to patch `boto3` — no real AWS calls are made.

---

## Requirements

- Python ≥ 3.10
- `boto3` ≥ 1.26
- `tqdm` ≥ 4.0
- Source bucket must have **versioning enabled**
- Source bucket must have **lock tags** in the format described above
