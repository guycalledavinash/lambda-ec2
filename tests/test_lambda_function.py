import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

try:
    from botocore.exceptions import BotoCoreError, ClientError
except ModuleNotFoundError:
    # Allow the tests to run on a stock Python installation. Lambda itself gets
    # boto3 and botocore from the managed AWS Lambda Python runtime.
    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        def __init__(self, error_response, operation_name):
            self.response = error_response
            self.operation_name = operation_name
            super().__init__(operation_name)

    botocore_module = types.ModuleType("botocore")
    exceptions_module = types.ModuleType("botocore.exceptions")
    exceptions_module.BotoCoreError = BotoCoreError
    exceptions_module.ClientError = ClientError
    sys.modules["botocore"] = botocore_module
    sys.modules["botocore.exceptions"] = exceptions_module

try:
    import boto3  # noqa: F401
except ModuleNotFoundError:
    # The client is patched in every test, so this stub cannot contact AWS.
    sys.modules["boto3"] = types.SimpleNamespace(client=None)

import lambda_function


class FakePaginator:
    def __init__(self, pages_or_error):
        self.pages_or_error = pages_or_error
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.pages_or_error, Exception):
            raise self.pages_or_error
        return iter(self.pages_or_error)


class FakeEC2:
    def __init__(self, snapshot_pages, image_pages):
        self.paginators = {
            "describe_snapshots": FakePaginator(snapshot_pages),
            "describe_images": FakePaginator(image_pages),
        }
        self.deleted = []

    def get_paginator(self, operation_name):
        return self.paginators[operation_name]

    def delete_snapshot(self, **kwargs):
        self.deleted.append(kwargs["SnapshotId"])


def snapshot(snapshot_id="snap-eligible", age_days=31, tags=None):
    return {
        "SnapshotId": snapshot_id,
        "StartTime": datetime.now(timezone.utc) - timedelta(days=age_days),
        "Tags": tags if tags is not None else [{"Key": "AutoCleanup", "Value": "true"}],
    }


class SnapshotCleanupTests(unittest.TestCase):
    def setUp(self):
        self.ec2 = FakeEC2([{"Snapshots": [snapshot()]}], [{"Images": []}])
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = patch.object(lambda_function.boto3, "client", return_value=self.ec2)
        self.client.start()

    def tearDown(self):
        self.client.stop()
        self.environment.stop()

    def invoke(self, event=None):
        return lambda_function.lambda_handler(event or {}, None)

    def test_default_dry_run_never_deletes_snapshots(self):
        result = self.invoke()
        self.assertTrue(result["dry_run"])
        self.assertEqual(["snap-eligible"], result["eligible"])
        self.assertEqual([], self.ec2.deleted)

    def test_disabled_environment_cannot_be_overridden_by_event(self):
        os.environ["ENABLE_DELETION"] = "false"
        result = self.invoke({"dry_run": False})
        self.assertTrue(result["dry_run"])
        self.assertEqual([], self.ec2.deleted)

    def test_deletion_requires_both_explicit_controls(self):
        os.environ["ENABLE_DELETION"] = "true"
        result = self.invoke({"dry_run": False})
        self.assertFalse(result["dry_run"])
        self.assertEqual(["snap-eligible"], self.ec2.deleted)
        self.assertEqual(["snap-eligible"], result["deleted"])

    def test_snapshot_without_opt_in_tag_is_skipped(self):
        self.ec2 = FakeEC2(
            [{"Snapshots": [snapshot(tags=[{"Key": "Name", "Value": "backup"}])]}],
            [{"Images": []}],
        )
        lambda_function.boto3.client.return_value = self.ec2
        result = self.invoke()
        self.assertEqual([], result["eligible"])
        self.assertEqual("missing_opt_in_tag", result["skipped"][0]["reason"])

    def test_snapshot_younger_than_minimum_age_is_skipped(self):
        os.environ["MIN_SNAPSHOT_AGE_DAYS"] = "30"
        self.ec2 = FakeEC2([{"Snapshots": [snapshot(age_days=29)]}], [{"Images": []}])
        lambda_function.boto3.client.return_value = self.ec2
        result = self.invoke()
        self.assertEqual([], result["eligible"])
        self.assertEqual("too_recent_or_unknown_age", result["skipped"][0]["reason"])

    def test_snapshot_referenced_by_registered_ami_is_skipped(self):
        self.ec2 = FakeEC2(
            [{"Snapshots": [snapshot("snap-ami")]}],
            [{"Images": [{"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-ami"}}]}]}],
        )
        lambda_function.boto3.client.return_value = self.ec2
        result = self.invoke()
        self.assertEqual([], result["eligible"])
        self.assertEqual("referenced_by_ami", result["skipped"][0]["reason"])

    def test_eligible_snapshot_is_identified(self):
        result = self.invoke()
        self.assertEqual(["snap-eligible"], result["eligible"])
        self.assertEqual([], result["deleted"])

    def test_aws_api_error_fails_safely_without_deletion(self):
        error = ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}}, "DescribeImages")
        self.ec2 = FakeEC2([{"Snapshots": [snapshot()]}], error)
        lambda_function.boto3.client.return_value = self.ec2
        os.environ["ENABLE_DELETION"] = "true"
        result = self.invoke({"dry_run": False})
        self.assertEqual("aws_api_error", result["error"])
        self.assertEqual([], self.ec2.deleted)

    def test_pagination_is_used_for_snapshots_and_images(self):
        self.ec2 = FakeEC2(
            [
                {"Snapshots": [snapshot("snap-page-one")]},
                {"Snapshots": [snapshot("snap-page-two")]},
            ],
            [
                {"Images": []},
                {"Images": [{"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-page-two"}}]}]},
            ],
        )
        lambda_function.boto3.client.return_value = self.ec2
        result = self.invoke()
        self.assertEqual(["snap-page-one"], result["eligible"])
        self.assertEqual({"OwnerIds": ["self"]}, self.ec2.paginators["describe_snapshots"].calls[0])
        self.assertEqual({"Owners": ["self"]}, self.ec2.paginators["describe_images"].calls[0])


if __name__ == "__main__":
    unittest.main()

