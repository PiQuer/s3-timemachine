"""Core functionality for S3 TimeMachine."""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

import boto3

from .utils import parse_iso, parse_lock_tags, select_lock_time_for_target

logger = logging.getLogger("s3_timemachine")

RestoreTier = Literal["Standard", "Bulk", "Expedited"]

# Storage classes that require a restore request before the object is readable.
ARCHIVE_STORAGE_CLASSES = {"GLACIER", "DEEP_ARCHIVE"}

# Pattern for parsing the x-amz-restore header.
# Example: ongoing-request="false", expiry-date="Fri, 23 Dec 2012 00:00:00 GMT"
_RESTORE_HEADER_RE = re.compile(r'ongoing-request="(?P<ongoing>true|false)"')


@dataclass(frozen=True)
class ObjectVersionRef:
    """A specific version of an object in the source bucket."""

    key: str
    version_id: str
    storage_class: str
    last_modified: datetime
    size: int


@dataclass(frozen=True)
class RestoreStatus:
    """The current restore status of an archived object version."""

    available: bool  # Already readable (not archived, or restore complete)
    ongoing: bool  # Restore in progress
    requires_restore: bool  # Archived and not yet restored


class S3TimeMachine:
    """Restore S3 buckets to a specific point in time."""

    def __init__(
        self,
        bucket_name: str,
        region: str | None = None,
        destination_bucket: str | None = None,
    ):
        """Initialize S3TimeMachine client.

        Args:
            bucket_name: Name of the source S3 bucket.
            region: AWS region (defaults to eu-west-1).
            destination_bucket: Name of the destination S3 bucket for restored objects.
        """
        self.bucket_name: str = bucket_name
        self.destination_bucket: str | None = destination_bucket
        self.region = region or "eu-west-1"
        self.s3_client = boto3.client("s3", region_name=self.region)
        self.s3_resource = boto3.resource("s3", region_name=self.region)

    # ---------------------------------------------------------------- bucket meta

    def get_bucket_versioning(self) -> bool:
        """Check if bucket versioning is enabled."""
        try:
            response = self.s3_client.get_bucket_versioning(Bucket=self.bucket_name)
            return response.get("Status") == "Enabled"
        except Exception as e:
            print(f"Error checking versioning: {e}")
            return False

    def get_bucket_tags(self) -> list[dict]:
        """Fetch the source bucket's tag set. Empty list if none."""
        try:
            response = self.s3_client.get_bucket_tagging(Bucket=self.bucket_name)
            return list(response.get("TagSet", []))
        except self.s3_client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "NoSuchTagSet":
                return []
            raise

    def get_lock_times(self, now: datetime | None = None) -> list[datetime]:
        """Return sorted lock timestamps from the bucket tags."""
        return parse_lock_tags(self.get_bucket_tags(), now=now)

    # ---------------------------------------------------------------- listing

    def list_object_versions(self) -> list[dict]:
        """List all object versions in the bucket (no delete markers)."""
        versions: list[dict] = []
        paginator = self.s3_client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket_name):
            if "Versions" in page:
                versions.extend(page["Versions"])
        return versions

    def list_all_entries(self) -> tuple[list[dict], list[dict]]:
        """List all versions and delete markers (paginated)."""
        versions: list[dict] = []
        markers: list[dict] = []
        paginator = self.s3_client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket_name):
            versions.extend(page.get("Versions") or [])
            markers.extend(page.get("DeleteMarkers") or [])
        return versions, markers

    # ---------------------------------------------------------------- core logic

    def versions_at(self, target_time: datetime) -> list[ObjectVersionRef]:
        """Determine the object versions that were current at ``target_time``.

        For each key:
          * collect all versions + delete markers with LastModified <= target_time;
          * the most recent of those is what existed at target_time;
          * if that entry is a delete marker, the object did not exist → skipped.
        """
        versions, markers = self.list_all_entries()

        # Tag each entry with its type, then group by key.
        by_key: dict[str, list[dict]] = {}
        for v in versions:
            by_key.setdefault(v["Key"], []).append({**v, "_type": "version"})
        for m in markers:
            by_key.setdefault(m["Key"], []).append({**m, "_type": "marker"})

        result: list[ObjectVersionRef] = []
        for key, entries in by_key.items():
            # Only entries that existed at or before target_time matter.
            candidates = [e for e in entries if e["LastModified"] <= target_time]
            if not candidates:
                continue
            # The "current" entry at target_time is the most recently modified one.
            current = max(candidates, key=lambda e: e["LastModified"])
            if current["_type"] == "marker":
                # Object was deleted at target_time.
                continue
            result.append(
                ObjectVersionRef(
                    key=key,
                    version_id=current["VersionId"],
                    storage_class=current.get("StorageClass", "STANDARD"),
                    last_modified=current["LastModified"],
                    size=int(current.get("Size", 0)),
                )
            )
        return result

    # ---------------------------------------------------------------- restore

    def _parse_restore_header(self, restore_header: str | None) -> tuple[bool, bool]:
        """Return ``(ongoing, restored_available)`` from x-amz-restore header."""
        if not restore_header:
            return (False, False)
        m = _RESTORE_HEADER_RE.search(restore_header)
        if not m:
            return (False, False)
        ongoing = m.group("ongoing") == "true"
        return (ongoing, not ongoing)

    def get_restore_status(self, ref: ObjectVersionRef) -> RestoreStatus:
        """Inspect whether an archived object version is readable."""
        if ref.storage_class not in ARCHIVE_STORAGE_CLASSES:
            return RestoreStatus(available=True, ongoing=False, requires_restore=False)

        head = self.s3_client.head_object(
            Bucket=self.bucket_name, Key=ref.key, VersionId=ref.version_id
        )
        ongoing, available = self._parse_restore_header(head.get("Restore"))
        return RestoreStatus(
            available=available,
            ongoing=ongoing,
            requires_restore=not (available or ongoing),
        )

    def initiate_restore(
        self,
        ref: ObjectVersionRef,
        tier: RestoreTier,
        days: int,
        dry_run: bool = False,
    ) -> None:
        """Send a restore request for an archived object version.

        If ``dry_run`` is True, only logs the intended action.
        """
        if dry_run:
            logger.info(
                "[DRY RUN] Would initiate restore (%s, %dd) for s3://%s/%s (version %s)",
                tier,
                days,
                self.bucket_name,
                ref.key,
                ref.version_id,
            )
            return
        self.s3_client.restore_object(
            Bucket=self.bucket_name,
            Key=ref.key,
            VersionId=ref.version_id,
            RestoreRequest={
                "Days": days,
                "GlacierJobParameters": {"Tier": tier},
            },
        )

    def ensure_restored(
        self,
        refs: Iterable[ObjectVersionRef],
        tier: RestoreTier,
        days: int,
        dry_run: bool = False,
    ) -> tuple[list[ObjectVersionRef], list[ObjectVersionRef]]:
        """Check restore status of every ref; trigger restore for archived ones.

        Returns:
            (ready, pending) where ``ready`` is immediately copyable and
            ``pending`` is still being restored (caller should retry later).
        """
        ready: list[ObjectVersionRef] = []
        pending: list[ObjectVersionRef] = []
        for ref in refs:
            status = self.get_restore_status(ref)
            logger.debug(
                "Restore status for s3://%s/%s (v=%s, class=%s): %s",
                self.bucket_name,
                ref.key,
                ref.version_id,
                ref.storage_class,
                status,
            )
            if status.available:
                ready.append(ref)
                continue
            if status.requires_restore:
                if not dry_run:
                    logger.info(
                        "Initiating restore (%s, %dd) for s3://%s/%s (version %s)",
                        tier,
                        days,
                        self.bucket_name,
                        ref.key,
                        ref.version_id,
                    )
                self.initiate_restore(ref, tier=tier, days=days, dry_run=dry_run)
            pending.append(ref)
        return ready, pending

    # ---------------------------------------------------------------- copy

    def copy_versions_to_destination(
        self,
        refs: Iterable[ObjectVersionRef],
        destination_bucket: str | None = None,
        dry_run: bool = False,
    ) -> list[ObjectVersionRef]:
        """Copy each (key, version) into the destination bucket under the same key.

        If ``dry_run`` is True, only logs the intended copies.
        """
        dest = destination_bucket or self.destination_bucket
        if not dest:
            raise ValueError("No destination bucket configured.")

        copied: list[ObjectVersionRef] = []
        for ref in refs:
            if dry_run:
                logger.info(
                    "[DRY RUN] Would copy s3://%s/%s (version %s) to s3://%s/%s",
                    self.bucket_name,
                    ref.key,
                    ref.version_id,
                    dest,
                    ref.key,
                )
            else:
                logger.debug(
                    "Copying s3://%s/%s (version %s) to s3://%s/%s",
                    self.bucket_name,
                    ref.key,
                    ref.version_id,
                    dest,
                    ref.key,
                )
                self.s3_client.copy_object(
                    Bucket=dest,
                    Key=ref.key,
                    CopySource={
                        "Bucket": self.bucket_name,
                        "Key": ref.key,
                        "VersionId": ref.version_id,
                    },
                )
            copied.append(ref)
        return copied

    # ---------------------------------------------------------------- pipeline

    def run(
        self,
        destination_bucket: str | None = None,
        tier: RestoreTier = "Standard",
        days: int = 7,
        target_time: datetime | None = None,
        prompt: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """High-level pipeline matching the project plan.

        Args:
            destination_bucket: Where restored objects will be copied to.
            tier: Glacier restore tier ("Standard" or "Bulk", or "Expedited").
            days: Retention period (days) for restored copies.
            target_time: Point-in-time to restore to. If None and ``prompt`` is True,
                the user is offered the bucket's lock-times to choose from.
            prompt: Whether to interactively prompt the user.
            dry_run: If True, no S3 mutations are performed; instead the intended
                actions are logged.

        Returns:
            Result dict including ``status``: one of
            ``"copied" | "pending" | "no-lock-times" | "no-objects"``.
        """
        dest = destination_bucket or self.destination_bucket
        if not dest:
            raise ValueError("No destination bucket configured.")

        if dry_run:
            logger.info("Dry-run mode enabled \u2014 no S3 mutations will occur.")

        # 1. Determine target lock time.
        lock_times = self.get_lock_times()
        if not lock_times:
            logger.info("No non-expired lock times found in bucket tags.")
            return {"status": "no-lock-times"}

        if target_time is not None:
            # Explicit target: pick the most recent lock time at or before it.
            if target_time.tzinfo is None:
                target_time = target_time.replace(tzinfo=timezone.utc)
            chosen = select_lock_time_for_target(lock_times, target_time)
            if chosen is None:
                logger.error(
                    "No lock time found at or before %s. Available: %s",
                    target_time.isoformat(),
                    ", ".join(t.isoformat() for t in lock_times),
                )
                return {"status": "no-lock-times"}
            logger.info(
                "Selected lock time %s (closest at or before %s).",
                chosen.isoformat(),
                target_time.isoformat(),
            )
            target_time = chosen
        else:
            if not prompt:
                raise ValueError("target_time required when prompt=False")
            target_time = _choose_timestamp(lock_times)

        if target_time.tzinfo is None:
            target_time = target_time.replace(tzinfo=timezone.utc)

        logger.info(
            "Determining object versions current at %s ...", target_time.isoformat()
        )

        # 2. Versions current at the chosen time.
        refs = self.versions_at(target_time)
        if not refs:
            logger.info("No object versions existed at the requested point in time.")
            return {"status": "no-objects", "target_time": target_time}

        logger.info("Found %d object versions to restore.", len(refs))

        # 3. Make sure archived versions are restored.
        ready, pending = self.ensure_restored(
            refs, tier=tier, days=days, dry_run=dry_run
        )

        if pending:
            logger.warning(
                "%d object version(s) are not yet available. Waiting on:",
                len(pending),
            )
            for ref in pending:
                logger.warning(
                    "  - s3://%s/%s (version %s, class %s)",
                    self.bucket_name,
                    ref.key,
                    ref.version_id,
                    ref.storage_class,
                )
            logger.info("Re-run this tool once the restores have completed.")
            return {
                "status": "pending",
                "target_time": target_time,
                "ready": ready,
                "pending": pending,
            }

        # 4. Copy all versions to the destination bucket.
        logger.info(
            "%sCopying %d object version(s) to s3://%s/ ...",
            "[DRY RUN] " if dry_run else "",
            len(ready),
            dest,
        )
        copied = self.copy_versions_to_destination(
            ready, destination_bucket=dest, dry_run=dry_run
        )
        logger.info("Done.")
        return {
            "status": "copied",
            "target_time": target_time,
            "copied": copied,
            "dry_run": dry_run,
        }


# -------------------------------------------------------------------- helpers


def _choose_timestamp(timestamps: list[datetime]) -> datetime:
    """Prompt the user to pick one timestamp from ``timestamps``."""
    print("Available restore points (lock-until times):")
    for idx, ts in enumerate(timestamps, start=1):
        print(f"  [{idx}] {ts.isoformat()}")
    while True:
        choice = input(f"Select a restore point [1-{len(timestamps)}]: ").strip()
        if choice.isdigit():
            n = int(choice)
            if 1 <= n <= len(timestamps):
                return timestamps[n - 1]
        print("Invalid selection.")


# -------------------------------------------------------------------- CLI


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s3-timemachine",
        description="Restore a versioned S3 bucket to a point in time.",
    )
    parser.add_argument("--source-bucket", required=True, help="Source bucket name")
    parser.add_argument(
        "--destination-bucket", required=True, help="Destination bucket name"
    )
    parser.add_argument("--region", default=None, help="AWS region (default us-east-1)")
    parser.add_argument(
        "--tier",
        choices=("Standard", "Bulk", "Expedited"),
        default="Standard",
        help="Glacier restore tier",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Retention period (days) for the restored copies",
    )
    parser.add_argument(
        "--target-time",
        default=None,
        help=(
            "ISO-8601 point-in-time to restore to. If omitted, the bucket's "
            " lock tags are listed for interactive selection."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not modify any S3 state; only log what would be done.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug-level logging.",
    )
    return parser


def _configure_logging(debug: bool) -> None:
    """Configure root logging for the CLI."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(level)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _build_arg_parser().parse_args(argv)

    _configure_logging(debug=args.debug)

    target_time: datetime | None = None
    if args.target_time:
        target_time = parse_iso(args.target_time)
        if target_time is None:
            logger.error("Invalid --target-time: %r", args.target_time)
            return 2

    machine = S3TimeMachine(
        bucket_name=args.source_bucket,
        region=args.region,
        destination_bucket=args.destination_bucket,
    )
    result = machine.run(
        destination_bucket=args.destination_bucket,
        tier=args.tier,
        days=args.days,
        target_time=target_time,
        dry_run=args.dry_run,
    )
    return 0 if result["status"] in {"copied", "no-objects", "no-lock-times"} else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
