import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timedelta, timezone

def lambda_handler(event, context):
    s3 = boto3.client('s3')

    # Configuration
    if "Records" in event:
        bucket_name = event['Records'][0]['s3']['bucket']['name']
    else:
        bucket_name = event['target_bucket_name'] # e.g., 'my-secure-bucket'

    try:
        # 1. Get existing tags
        response = s3.get_bucket_tagging(Bucket=bucket_name)
        tag_set = response['TagSet']
    except ClientError as e:
        # If no tags exist, get_bucket_tagging raises an error
        if e.response['Error']['Code'] == 'NoSuchTagSet':
            tag_set = []
        else:
            raise
    tag_config = {tag['Key']: tag['Value'] for tag in tag_set if tag['Key'].startswith('rolling_lock_')}

    prefix = event.get('test_prefix', tag_config.get('rolling_lock_test_prefix', ''))
    retention_days = event.get('retention_time', int(tag_config.get('rolling_lock_retention_time', 30)))
    mode = event.get('mode', tag_config.get('rolling_lock_mode', 'GOVERNANCE'))

    # Calculate new retention date
    now_date = datetime.now(timezone.utc)
    until_date = now_date + timedelta(days=retention_days)

    # Log the action
    print(f"Setting {mode} mode retention until {until_date} for objects with prefix '{prefix}' in bucket '{bucket_name}'.")

    # Use Paginator to list all objects (handles > 1000 objects)
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

    count = 0
    for page in pages:
        if 'Contents' in page:
            for obj in page['Contents']:
                try:
                    # Apply retention to each object
                    s3.put_object_retention(
                        Bucket=bucket_name,
                        Key=obj['Key'],
                        Retention={
                            'Mode': mode,
                            'RetainUntilDate': until_date
                        }
                    )
                except ClientError as e:
                    print(f"Error updating retention for {obj['Key']}: {e}")
                count += 1

    tag_set.append({'Key': f'LockTime{now_date.isoformat()}', 'Value': f'Locked until {until_date.isoformat()}'})
    s3.put_bucket_tagging(
        Bucket=bucket_name,
        Tagging={
            'TagSet': tag_set
        }
    )

    return_value = {
        'statusCode': 200,
        'body': f"Successfully updated retention for {count} objects in bucket {bucket_name}."
    }
    print(return_value)
    return return_value

