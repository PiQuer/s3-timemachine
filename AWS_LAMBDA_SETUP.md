# AWS Lambda Setup for Lock Creation

This guide explains how to set up AWS Lambda to automatically create lock tags on your S3 bucket, which are required by `s3-timemachine` for point-in-time restores.

The lock creation Lambda is a prerequisite that runs on a schedule or on-demand. Once locks are in place, you invoke `s3-timemachine` manually from the CLI whenever disaster recovery is needed.

---

## Overview

**Lock tags** record snapshots of your bucket at specific points in time. They serve two purposes:

1. **Configuration source** — tags on the bucket can store default settings (retention period, object lock mode, prefix filter)
2. **Restore points** — tags record when snapshots were taken, allowing `s3-timemachine` to discover available restore times

The lock creation Lambda (`aws_example/lambda_function.py`) automates the process of:
- Reading configuration from tags or event payload
- Listing all objects (optionally filtered by prefix)
- Setting S3 Object Lock retention on each object
- Creating a new lock tag with today's timestamp

---

## Lock Creation Lambda

See the complete example in `aws_example/lambda_function.py`.

### What it does

1. Accepts configuration from bucket tags or Lambda event payload
2. Lists all objects in the bucket (with optional prefix filter)
3. Sets S3 Object Lock retention on each object with the specified mode (GOVERNANCE or COMPLIANCE) and duration
4. Creates a new bucket tag: `LockTime<timestamp>` → `Locked until <timestamp>`

### Configuration

Configuration can come from two sources; **event payload overrides tags**:

| Parameter | Tag Key | Event Key | Default |
|-----------|---------|-----------|---------|
| Bucket name | — | `target_bucket_name` | from S3 trigger event |
| Prefix (filter) | `rolling_lock_test_prefix` | `test_prefix` | empty (all objects) |
| Retention days | `rolling_lock_retention_time` | `retention_time` | `30` |
| Lock mode | `rolling_lock_mode` | `mode` | `GOVERNANCE` |

#### Tag-based configuration example

Set these tags on your bucket to configure the Lambda with defaults:

```/dev/null/json#L1-5
rolling_lock_retention_time        : 30
rolling_lock_mode                  : GOVERNANCE
rolling_lock_test_prefix           : data/
```

Then the Lambda can be triggered with just the bucket name, and it will use these defaults.

#### Event payload configuration example

```/dev/null/json#L1-5
{
  "target_bucket_name": "my-source-bucket",
  "retention_time": 30,
  "mode": "GOVERNANCE",
  "test_prefix": "data/"
}
```

---

## IAM Policy

Your Lambda execution role needs these permissions:

```/dev/null/json#L1-45
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetBucketObjectLockConfiguration",
        "s3:ListBucket",
        "s3:PutBucketTagging",
        "s3:GetBucketTagging"
      ],
      "Resource": [
        "arn:aws:s3:::my-source-bucket-1",
        "arn:aws:s3:::my-source-bucket-2",
        "arn:aws:s3:::my-source-bucket-3"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObjectRetention"
      ],
      "Resource": [
        "arn:aws:s3:::my-source-bucket-1/*",
        "arn:aws:s3:::my-source-bucket-2/*",
        "arn:aws:s3:::my-source-bucket-3/*"
      ]
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

Replace `my-source-bucket-1`, `my-source-bucket-2`, `my-source-bucket-3` with your actual bucket names. Make sure to enable versioning and object locking on your buckets.

---

## Deployment

### 1. Create Lambda execution role

```/dev/null/bash#L1-20
# Create trust policy
cat > trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# Create role
aws iam create-role \
  --role-name s3-timemachine-lock-role \
  --assume-role-policy-document file://trust-policy.json
```

### 2. Attach inline policy

```/dev/null/bash#L1-10
aws iam put-role-policy \
  --role-name s3-timemachine-lock-role \
  --policy-name s3-timemachine-lock-permissions \
  --policy-document file://aws_example/lambda_policy.json
```

### 3. Create Lambda function

```/dev/null/bash#L1-15
zip -j lambda_function.zip aws_example/lambda_function.py
aws lambda create-function \
  --function-name s3-timemachine-lock-creator \
  --runtime python3.11 \
  --role arn:aws:iam::123456789012:role/s3-timemachine-lock-role \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://lambda_function.zip \
  --timeout 600 \
  --memory-size 256 \
  --region eu-west-1
```

Replace `123456789012` with your AWS account ID.

---

## Triggering the Lock Lambda

You have two options for triggering lock creation:

### Option 1: S3 trigger on upload of a marker file

Watch for uploads of a specific file (e.g. `rolling_lock_trigger`) in your bucket. When detected, the Lambda runs and creates a lock tag.

**Advantages:**
- On-demand: you control when locks are created
- Can be triggered manually by uploading a file
- Flexible: supports multiple buckets via the same Lambda
- Audit-proof timestamp metadata: The marker file and its version history serve as an independent, tamper-evident record of when snapshots occurred. Even if a malicious actor successfully deletes the bucket tags (the snapshot metadata), you only lose the retention period information — the precise historical timeline of when snapshots were taken remains securely preserved via the marker file's history.

**Setup:**

1. **Add S3 trigger to Lambda:**

```/dev/null/bash#L1-15
aws lambda add-permission \
  --function-name s3-timemachine-lock-creator \
  --statement-id s3-invoke \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn arn:aws:s3:::my-source-bucket \
  --region eu-west-1
```

2. **Create S3 bucket notification configuration:**

```/dev/null/bash#L1-30
cat > notification.json << 'EOF'
{
  "LambdaFunctionConfigurations": [
    {
      "LambdaFunctionArn": "arn:aws:lambda:eu-west-1:123456789012:function:s3-timemachine-lock-creator",
      "Events": ["s3:ObjectCreated:*"],
      "Filter": {
        "Key": {
          "FilterRules": [
            {
              "Name": "suffix",
              "Value": "rolling_lock_trigger"
            }
          ]
        }
      }
    }
  ]
}
EOF

aws s3api put-bucket-notification-configuration \
  --bucket my-source-bucket \
  --notification-configuration file://notification.json \
  --region eu-west-1
```

3. **Trigger manually by uploading the marker file:**

```/dev/null/bash#L1-3
echo "" | aws s3 cp - s3://my-source-bucket/rolling_lock_trigger
```

The Lambda will automatically read the bucket's tags for configuration and create a lock.

---

### Option 2: EventBridge Scheduler (recommended for regular automation)

Schedule the Lambda to run automatically at fixed intervals (e.g. daily at midnight) using the modern **EventBridge Scheduler**. This is the recommended approach; the older "Scheduled Rules" are legacy and should not be used for new setups.

**Advantages:**
- Fully automated — no manual intervention needed
- Runs on a schedule you control
- Modern, actively maintained (older "Scheduled Rules" are legacy)
- Can pass configuration via event payload
- Better UI and filtering in AWS Console

**Setup:**

1. **Create an IAM role for the scheduler to assume:**

```/dev/null/bash#L1-30
# Create trust policy for EventBridge Scheduler
cat > scheduler-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "scheduler.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# Create the role
aws iam create-role \
  --role-name s3-timemachine-scheduler-role \
  --assume-role-policy-document file://scheduler-trust-policy.json
```

2. **Attach a policy allowing the scheduler to invoke the Lambda:**

```/dev/null/bash#L1-20
cat > scheduler-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:eu-west-1:123456789012:function:s3-timemachine-lock-creator"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name s3-timemachine-scheduler-role \
  --policy-name s3-timemachine-scheduler-policy \
  --policy-document file://scheduler-policy.json
```

Replace `123456789012` with your AWS account ID and `eu-west-1` with your region.

3. **Create an EventBridge Schedule:**

```/dev/null/bash#L1-35
aws scheduler create-schedule \
  --name s3-timemachine-daily-lock \
  --schedule-expression "cron(0 0 * * ? *)" \
  --timezone "UTC" \
  --state ENABLED \
  --target "Arn=arn:aws:lambda:eu-west-1:123456789012:function:s3-timemachine-lock-creator,RoleArn=arn:aws:iam::123456789012:role/s3-timemachine-scheduler-role" \
  --flexible-time-window Mode=OFF \
  --region eu-west-1
```

This creates a schedule that runs every day at 00:00 UTC. Adjust the cron expression as needed:
- `cron(0 0 * * ? *)` = Every day at 00:00 UTC
- `cron(0 12 * * ? *)` = Every day at 12:00 UTC
- `cron(0 6 ? * MON *)` = Every Monday at 06:00 UTC
- `cron(0 0 ? * MON-FRI *)` = Weekdays (Mon–Fri) at 00:00 UTC

4. **Add configuration via input payload:**

To pass configuration to the Lambda (instead of reading from bucket tags), update the schedule:

```/dev/null/bash#L1-40
cat > target.json << 'EOF'
{
  "Arn": "arn:aws:lambda:eu-west-1:123456789012:function:s3-timemachine-lock-creator",
  "RoleArn": "arn:aws:iam::123456789012:role/s3-timemachine-scheduler-role",
  "Input": "{\"target_bucket_name\":\"my-source-bucket\",\"retention_time\":30,\"mode\":\"GOVERNANCE\"}"
}
EOF

aws scheduler update-schedule \
  --name s3-timemachine-daily-lock \
  --schedule-expression "cron(0 0 * * ? *)" \
  --state ENABLED \
  --target file://target.json \
  --flexible-time-window Mode=OFF \
  --region eu-west-1
```

Replace:
- `my-source-bucket` with your bucket name
- `123456789012` with your AWS account ID

5. **Verify the schedule is working:**

```/dev/null/bash#L1-5
aws scheduler get-schedule --name s3-timemachine-daily-lock
```

View execution history in CloudWatch Logs:

```/dev/null/bash#L1-5
aws logs tail /aws/lambda/s3-timemachine-lock-creator --follow
```

---

## Manual Invocation for Testing

Test the Lambda without setting up a trigger:

```/dev/null/bash#L1-15
aws lambda invoke \
  --function-name s3-timemachine-lock-creator \
  --payload '{"target_bucket_name":"my-source-bucket","retention_time":30,"mode":"GOVERNANCE"}' \
  --cli-binary-format raw-in-base64-out \
  --region eu-west-1 \
  response.json

cat response.json
```

Or use the bucket's tag configuration:

```/dev/null/bash#L1-10
aws lambda invoke \
  --function-name s3-timemachine-lock-creator \
  --payload '{"target_bucket_name":"my-source-bucket"}' \
  --region eu-west-1 \
  response.json
```

---

## Using s3-timemachine to Restore

Once lock tags exist on your bucket, restore objects manually from the CLI whenever needed:

### Dry-run first

```/dev/null/bash#L1-10
s3-timemachine restore \
  --source-bucket my-source-bucket \
  --destination-bucket my-restore-bucket \
  --target-time 2026-01-15T00:00:00Z \
  --days 7 \
  --dry-run
```

### Real restore

```/dev/null/bash#L1-10
s3-timemachine restore \
  --source-bucket my-source-bucket \
  --destination-bucket my-restore-bucket \
  --target-time 2026-01-15T00:00:00Z \
  --days 7
```

### List available restore points interactively

Omit `--target-time` to see available lock times:

```/dev/null/bash#L1-5
s3-timemachine restore \
  --source-bucket my-source-bucket \
  --destination-bucket my-restore-bucket
```

For more details, see the [s3-timemachine README](./README.md).

---

## Monitoring and Logging

The Lambda logs to CloudWatch under `/aws/lambda/s3-timemachine-lock-creator`.

**Key log messages:**
- `"Setting GOVERNANCE mode retention until ..."` — lock is being created
- `"Successfully updated retention for N objects"` — success
- `"Error updating retention for <key>"` — individual object failure (logs and continues)

Monitor these logs to confirm locks are being created successfully:

```/dev/null/bash#L1-5
aws logs tail /aws/lambda/s3-timemachine-lock-creator --follow
```

---

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| `NoSuchTagSet` when creating locks | Bucket has no tags yet | Normal; Lambda creates the first tag automatically |
| `AccessDenied: PutObjectRetention` | Missing IAM permission | Verify `s3:PutObjectRetention` is in the policy |
| S3 trigger not firing | Notification config not set | Check `aws s3api get-bucket-notification-configuration` |
| EventBridge rule not triggering | Rule disabled or wrong schedule | Check rule status: `aws events describe-rule --name s3-timemachine-daily-lock` |
| Lambda times out | Too many objects or slow network | Increase `Timeout` to 600 seconds |
| `NoSuchBucket` | Wrong bucket name | Verify bucket name in tag config or event payload |

---

## Cost Considerations

- **Bucket tagging**: No cost
- **Object Lock retention**: No cost (it's metadata)
- **Lambda invocations**: ~$0.20 per million invocations
- **S3 API calls**: ~1 `PUT` per object (via `PutObjectRetention`) + 1 `GET` per bucket
  - For 1 million objects: ~$5–10 per lock creation
  - For daily locks: ~$150–300/month per bucket

---

## References

- [AWS Lambda Developer Guide](https://docs.aws.amazon.com/lambda/)
- [EventBridge cron expressions](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-cron-expressions.html)
- [S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
- [s3-timemachine README](./README.md)
