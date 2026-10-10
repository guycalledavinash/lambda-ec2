"""Safely identify EBS snapshots that have opted into automated cleanup.

Deletion is deliberately gated by both deployment configuration and invocation
input. A snapshot's source volume state is not used as a deletion signal.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

DEFAULT_MIN_SNAPSHOT_AGE_DAYS = 30
AUTO_CLEANUP_TAG_KEY = "AutoCleanup"
AUTO_CLEANUP_TAG_VALUE = "true"


def _is_enabled(value):
    """Return True only for an explicit, conventional true value."""
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _minimum_snapshot_age_days():
    """Read a non-negative age threshold, falling back safely when invalid."""
    value = os.environ.get("MIN_SNAPSHOT_AGE_DAYS", str(DEFAULT_MIN_SNAPSHOT_AGE_DAYS))
    try:
        age_days = int(value)
        if age_days < 0:
            raise ValueError
        return age_days
    except (TypeError, ValueError):
        LOGGER.warning(
            "Invalid MIN_SNAPSHOT_AGE_DAYS value; using conservative default of %s days",
            DEFAULT_MIN_SNAPSHOT_AGE_DAYS,
        )
        return DEFAULT_MIN_SNAPSHOT_AGE_DAYS


def _snapshot_tags(snapshot):
    """Return tags as a dictionary, or None when tag data is malformed."""
    tags = snapshot.get("Tags", [])
    if not isinstance(tags, list):
        return None
    result = {}
    for tag in tags:
        if not isinstance(tag, dict) or "Key" not in tag or "Value" not in tag:
            return None
        result[tag["Key"]] = tag["Value"]
    return result


def _snapshot_has_opt_in_tag(snapshot):
    tags = _snapshot_tags(snapshot)
    return tags is not None and str(tags.get(AUTO_CLEANUP_TAG_KEY, "")).lower() == AUTO_CLEANUP_TAG_VALUE


def _iter_paginated(ec2, operation_name, **kwargs):
    """Read every page, treating an incomplete page as unsafe rather than empty."""
    paginator = ec2.get_paginator(operation_name)
    for page in paginator.paginate(**kwargs):
        if not isinstance(page, dict):
            raise ValueError("AWS returned a malformed response page")
        yield page


def _list_snapshots(ec2):
    snapshots = []
    for page in _iter_paginated(ec2, "describe_snapshots", OwnerIds=["self"]):
        page_snapshots = page.get("Snapshots")
        if not isinstance(page_snapshots, list):
            raise ValueError("describe_snapshots response did not include a Snapshots list")
        if not all(isinstance(snapshot, dict) for snapshot in page_snapshots):
            raise ValueError("describe_snapshots returned malformed snapshot data")
        snapshots.extend(page_snapshots)
    return snapshots


def _referenced_ami_snapshot_ids(ec2):
    """Return snapshots referenced by this account's currently registered AMIs."""
    snapshot_ids = set()
    for page in _iter_paginated(ec2, "describe_images", Owners=["self"]):
        images = page.get("Images")
        if not isinstance(images, list):
            raise ValueError("describe_images response did not include an Images list")
        for image in images:
            if not isinstance(image, dict):
                raise ValueError("describe_images returned a malformed image")
            mappings = image.get("BlockDeviceMappings", [])
            if not isinstance(mappings, list):
                raise ValueError("describe_images returned malformed block device mappings")
            for mapping in mappings:
                if not isinstance(mapping, dict):
                    raise ValueError("describe_images returned a malformed block device mapping")
                ebs = mapping.get("Ebs")
                if ebs is None:
                    continue
                if not isinstance(ebs, dict):
                    raise ValueError("describe_images returned malformed EBS mapping data")
                snapshot_id = ebs.get("SnapshotId")
                if not snapshot_id:
                    raise ValueError("describe_images returned an EBS mapping without a SnapshotId")
                snapshot_ids.add(snapshot_id)
    return snapshot_ids


def _is_old_enough(snapshot, minimum_age, now):
    start_time = snapshot.get("StartTime")
    if not isinstance(start_time, datetime):
        return False
    if start_time.tzinfo is None:
        # AWS returns timezone-aware values; a naive timestamp is ambiguous.
        return False
    return start_time <= now - minimum_age


def lambda_handler(event, context):
    """Plan or perform only explicitly authorized, policy-eligible deletions."""
    event = event or {}
    if not isinstance(event, dict):
        LOGGER.warning("Ignoring malformed event; dry-run remains in effect")
        event = {}

    deletion_enabled = _is_enabled(os.environ.get("ENABLE_DELETION", "false"))
    requested_live_run = event.get("dry_run") is False
    dry_run = not (deletion_enabled and requested_live_run)
    minimum_age_days = _minimum_snapshot_age_days()
    minimum_age = timedelta(days=minimum_age_days)

    if dry_run:
        LOGGER.info(
            "Running in dry-run mode (ENABLE_DELETION=%s, event dry_run=%r)",
            deletion_enabled,
            event.get("dry_run", True),
        )

    ec2 = boto3.client("ec2")
    try:
        # Fetch all policy inputs before deleting anything. A pagination/API failure
        # therefore cannot lead to deletion based on only a partial inventory.
        snapshots = _list_snapshots(ec2)
        ami_snapshot_ids = _referenced_ami_snapshot_ids(ec2)
    except ClientError as error:
        LOGGER.error("AWS API error while determining snapshot eligibility; skipping all deletions: %s", error)
        return {"dry_run": dry_run, "eligible": [], "deleted": [], "error": "aws_api_error"}
    except (BotoCoreError, ValueError, TypeError, KeyError) as error:
        LOGGER.error("Could not safely determine snapshot eligibility; skipping all deletions: %s", error)
        return {"dry_run": dry_run, "eligible": [], "deleted": [], "error": "eligibility_unknown"}

    now = datetime.now(timezone.utc)
    eligible = []
    skipped = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not snapshot.get("SnapshotId"):
            LOGGER.warning("Skipping malformed snapshot data")
            skipped.append({"snapshot_id": None, "reason": "malformed_snapshot"})
            continue
        snapshot_id = snapshot["SnapshotId"]
        tags = _snapshot_tags(snapshot)
        if tags is None:
            LOGGER.info("Skipping snapshot %s: tags are incomplete", snapshot_id)
            skipped.append({"snapshot_id": snapshot_id, "reason": "incomplete_tags"})
        elif not _snapshot_has_opt_in_tag(snapshot):
            LOGGER.info("Skipping snapshot %s: missing %s=true tag", snapshot_id, AUTO_CLEANUP_TAG_KEY)
            skipped.append({"snapshot_id": snapshot_id, "reason": "missing_opt_in_tag"})
        elif snapshot_id in ami_snapshot_ids:
            LOGGER.info("Skipping snapshot %s: referenced by a registered AMI", snapshot_id)
            skipped.append({"snapshot_id": snapshot_id, "reason": "referenced_by_ami"})
        elif not _is_old_enough(snapshot, minimum_age, now):
            LOGGER.info("Skipping snapshot %s: younger than %s days or missing a valid StartTime", snapshot_id, minimum_age_days)
            skipped.append({"snapshot_id": snapshot_id, "reason": "too_recent_or_unknown_age"})
        else:
            eligible.append(snapshot_id)

    deleted = []
    for snapshot_id in eligible:
        reason = "AutoCleanup=true, meets minimum age, and is not AMI-referenced"
        if dry_run:
            LOGGER.info("Would delete snapshot %s: %s", snapshot_id, reason)
            continue
        try:
            ec2.delete_snapshot(SnapshotId=snapshot_id)
            deleted.append(snapshot_id)
            LOGGER.info("Deleted eligible snapshot %s: %s", snapshot_id, reason)
        except ClientError as error:
            # A failed delete leaves the snapshot intact and is safe to retry later.
            LOGGER.error("Could not delete snapshot %s; leaving it intact: %s", snapshot_id, error)

    return {"dry_run": dry_run, "eligible": eligible, "deleted": deleted, "skipped": skipped}

