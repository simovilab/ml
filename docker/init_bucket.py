"""Dev helper: wait for MinIO and create the artifact bucket if missing.

Runs inside the MLflow image (which already contains boto3) because the
hardened MinIO image ships no `mc`. Not used in production, where the bucket
and its restricted access key are managed on the MinIO server itself.
"""

import os
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

secrets = Path(os.environ.get("ML_SECRETS_DIR", "/run/secrets"))
bucket = os.environ["ML_BUCKET"]

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["MLFLOW_S3_ENDPOINT_URL"],
    aws_access_key_id=(secrets / "mlflow_s3_access_key").read_text().strip(),
    aws_secret_access_key=(secrets / "mlflow_s3_secret_key").read_text().strip(),
    region_name="us-east-1",
    config=Config(retries={"max_attempts": 1}, connect_timeout=3, read_timeout=5),
)

deadline = time.monotonic() + 90
while True:
    try:
        try:
            s3.head_bucket(Bucket=bucket)
            print(f"OK: bucket '{bucket}' is ready (it already existed, nothing to do)")
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket", "NotFound"):
                raise
            s3.create_bucket(Bucket=bucket)
            print(f"OK: created bucket '{bucket}'")
        sys.exit(0)
    except (BotoCoreError, ClientError) as exc:
        if time.monotonic() > deadline:
            print(f"giving up: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"waiting for MinIO: {type(exc).__name__}", flush=True)
        time.sleep(2)
