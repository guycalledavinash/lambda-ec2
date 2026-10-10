# lambda-ec2

This AWS Lambda function identifies EBS snapshots that have explicitly opted in
to automated cleanup. It is deliberately conservative: source-volume attachment
state and missing volumes are never reasons to delete a snapshot.

## Safety model

Every invocation is a **dry run** by default. In dry-run mode the function logs
each snapshot it *would* delete and why, but never calls `DeleteSnapshot`.

A snapshot is eligible only when all of these conditions hold:

- It has the tag `AutoCleanup=true` (the value is case-insensitive).
- It is at least the configured minimum age.
- It is not referenced by any currently registered AMI owned by the account.
- The function could retrieve a complete snapshots and AMIs inventory. Any AWS
  API error or malformed response causes the invocation to skip all deletions.

## Configuration and invocation

| Control | Default | Effect |
| --- | --- | --- |
| `ENABLE_DELETION` | `false` | Must be explicitly set to `true` (also accepts `1`, `yes`, or `on`) before a live deletion is possible. |
| `MIN_SNAPSHOT_AGE_DAYS` | `30` | Non-negative minimum age in days. Invalid values safely fall back to 30. |
| Event `dry_run` | `true` | Must be the Boolean `false` to request a live run. |

Both controls are mandatory for deletion. For example, an event of
`{"dry_run": false}` still runs dry if `ENABLE_DELETION` is unset or `false`.
No event value can override a disabled `ENABLE_DELETION` environment setting.

To enable deletion deliberately, first tag only the intended snapshots:

```
AutoCleanup=true
```

Review the dry-run logs, then deploy with `ENABLE_DELETION=true` and invoke the
function with this exact JSON event:

```json
{"dry_run": false}
```

Keep `MIN_SNAPSHOT_AGE_DAYS` at its conservative default unless a shorter
retention period has been reviewed. Returning to dry-run mode only requires
unsetting `ENABLE_DELETION`, setting it to `false`, or omitting `dry_run: false`
from the event.

## IAM permissions

Grant the Lambda execution role only the permissions it needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeSnapshots",
        "ec2:DescribeImages",
        "ec2:DeleteSnapshot"
      ],
      "Resource": "*"
    }
  ]
}
```

`ec2:DeleteSnapshot` is needed only for intentionally enabled live deletion;
omit it for a permanently dry-run deployment. AWS does not support resource-level
permissions for the describe actions, so these actions require `"Resource": "*"`.
Scope `DeleteSnapshot` further with organization-specific IAM conditions where
available, and test those conditions first.

## Local tests

The unit tests use mocked Boto3 clients and do not contact AWS or need AWS
credentials. With Python 3 and the Lambda dependency available locally:

```bash
python -m pip install boto3
python -m unittest discover -s tests -v
python -m py_compile lambda_function.py
```

The handler entry point remains `lambda_function.lambda_handler`.

