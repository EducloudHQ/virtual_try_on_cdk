import json
import os
import uuid
from urllib.parse import unquote_plus

import boto3

# AWS clients
sfn_client = boto3.client("stepfunctions")

# Env vars
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]  # fail fast if missing

# Reference shirts to try on
T_SHIRTS = [
    "black_t.png",
    "green_t.png",
    "lined_t.png",
    "red_t.png",
    "yello_t.png",
]


def handler(event, context):
    """
    For each S3 ObjectCreated record, invoke the Step Functions workflow once
    per T_SHIRTS item. The workflow input includes:
      - id: unique execution id
      - bucket: source bucket (from event)
      - key:    source key (from event)
      - reference_img: the t-shirt filename from T_SHIRTS
      - timestamp: original S3 event time
    """
    executions = []

    try:
        # Helpful for debugging; keep logs small
        print("Received event with %d record(s)" % len(event.get("Records", [])))

        for record in event.get("Records", []):
            bucket = record["s3"]["bucket"]["name"]
            key = unquote_plus(record["s3"]["object"]["key"])
            timestamp = record.get("eventTime")  # already RFC3339 from S3

            print(f"Processing s3://{bucket}/{key}")

            for ref in T_SHIRTS:
                execution_id = str(uuid.uuid4())

                workflow_input = {
                    "id": execution_id,
                    "bucket": bucket,
                    "key": key,
                    "reference_img": ref,
                    "timestamp": timestamp,
                }

                # Execution names must be unique per 90 days; derive a friendly name
                # (shorten key if very long)
                short_key = key.replace("/", "_")[-80:]
                exec_name = (
                    f"tryon-{short_key}-{os.path.splitext(ref)[0]}-{execution_id[:8]}"
                )

                resp = sfn_client.start_execution(
                    stateMachineArn=STATE_MACHINE_ARN,
                    name=exec_name,
                    input=json.dumps(workflow_input),
                )

                executions.append(
                    {
                        "executionArn": resp["executionArn"],
                        "reference_img": ref,
                        "source_key": key,
                    }
                )

                print(f"Started execution for {ref}: {resp['executionArn']}")

        return {
            "statusCode": 200,
            "executions": executions,
            "count": len(executions),
        }

    except Exception as e:
        print(f"Error starting state machine(s): {e}")
        # Let Lambda fail so retries/DLQ can catch it
        raise
