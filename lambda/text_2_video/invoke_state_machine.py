import json
import os
import uuid

import boto3
from aws_lambda_powertools import Logger, Tracer


# Create the Bedrock Runtime client.
bedrock = boto3.client(service_name="bedrock-runtime", region_name="us-east-1")

logger = Logger(service="invoke_agent_lambda")
tracer = Tracer(service="invoke_agent_lambda")

# AWS clients
sfn_client = boto3.client("stepfunctions")

# Env vars
STATE_MACHINE_ARN = os.environ["TEXT_2_VIDEO_STATE_MACHINE_ARN"]
bucket_name = os.environ.get("BUCKET_NAME")  # set this in Lambda env variables


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event, context):
    logger.info(f"input is {event['arguments']['input']}")
    user_prompt = event["arguments"]["input"]

    try:
        execution_id = str(uuid.uuid4())

        workflow_input = {
            "id": execution_id,
            "user_prompt": user_prompt,
            "bucketUri": f"s3://{bucket_name}/",
            "folderUUID": f"vids-{execution_id[:8]}",
        }

        exec_name = f"text-2-video-{execution_id[:8]}"

        resp = sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=exec_name,
            input=json.dumps(workflow_input),
        )

        logger.info(f"Started execution  {resp['executionArn']}")

        return True

    except Exception as e:
        logger.error(f"Error starting state machine(s): {e}")
        # Let Lambda fail so retries/DLQ can catch it
        raise
