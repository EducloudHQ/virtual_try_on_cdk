import os
import uuid
from datetime import datetime, timezone
import base64
import json

import boto3
from aws_lambda_powertools import Logger, Tracer


# Create the Bedrock Runtime client.
bedrock = boto3.client(service_name="bedrock-runtime", region_name="us-east-1")

logger = Logger(service="invoke_agent_lambda")
tracer = Tracer(service="invoke_agent_lambda")

# Initialize AWS clients
s3_client = boto3.client("s3")
events_client = boto3.client("events")

# Get environment variables
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME")
BUCKET_NAME = os.environ.get("BUCKET_NAME")

prefix = "results/"


def s3_image_to_base64(bucket: str, key: str) -> str:
    """
    Download an image object from S3 and return a base-64–encoded string
    suitable for embedding (e.g., in `data:image/jpeg;base64,...`).

    Args:
        bucket:  The S3 bucket name.
        key:     The full object key (path/filename in the bucket).

    Returns:
        A UTF-8 str containing the Base-64 content.
    """
    # 1) Fetch the object
    resp = s3_client.get_object(Bucket=bucket, Key=key)
    binary = resp["Body"].read()  # bytes

    # 2) Base-64-encode
    b64_bytes = base64.b64encode(binary)
    b64_str = b64_bytes.decode("utf-8")

    # 3) Build a data-URI (optional but handy for HTML e-mail, etc.)
    #    If you don’t know the MIME type ahead of time, grab it from resp["ContentType"]
    # mime_type = resp.get("ContentType", "image/jpeg")
    # data_uri = f"data:{mime_type};base64,{b64_str}"

    # return data_uri
    return b64_str


def load_image_as_base64(image_path):
    """Helper function for preparing image data."""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event, context):
    """
    Lambda function invoked by Step Functions workflow.
    This function processes the image and sends events to EventBridge.

    Args:
        event: Input from Step Functions
        context: Lambda context

    Returns:
        Dictionary with processing results
    """
    logger.info(f"Received S3 event: {event}")
    now = datetime.now(timezone.utc)
    try:
        # Log the received event
        logger.info(f"Received event from Step Functions: {json.dumps(event)}")

        # Extract data from the event
        execution_id = event.get("id", str(uuid.uuid4()))
        bucket = event.get("bucket", BUCKET_NAME)
        key = event.get("key")
        reference_img = event.get("reference_img")

        if not key:
            raise ValueError("No image key provided in the event")
        if not reference_img:
            raise ValueError("No reference image was provided")

        if key.startswith("results/"):  # must match OUTPUT_PREFIX
            logger.info("must match output prefix")
            return {"skipped": True}

        logger.info(f"Processing image: {key} from bucket: {bucket}")

        source_base_64 = s3_image_to_base64(bucket, key)
        reference_img_base_64 = s3_image_to_base64(bucket, reference_img)

        logger.info(f"this is the reference base 64 image {reference_img_base_64[:50]}")

        logger.info(f"this is the source base 64 {source_base_64[:50]}")

        inference_params = {
            "taskType": "VIRTUAL_TRY_ON",
            "virtualTryOnParams": {
                "sourceImage": source_base_64,
                "referenceImage": reference_img_base_64,
                "maskType": "GARMENT",
                "garmentBasedMask": {"garmentClass": "UPPER_BODY"},
            },
        }

        # Simulate image processing
        logger.info("Starting image processing...")
        # Prepare the invocation payload.
        body_json = json.dumps(inference_params, indent=2)

        # Invoke Nova Canvas.
        response = bedrock.invoke_model(
            body=body_json,
            modelId="amazon.nova-canvas-v1:0",
            accept="application/json",
            contentType="application/json",
        )

        # Extract the images from the response.
        response_body_json = json.loads(response.get("body").read())
        images = response_body_json.get("images", [])

        logger.info(f"images are {images}")

        # Check for errors.
        if response_body_json.get("error"):
            logger.info(response_body_json.get("error"))

        for index, image_base64 in enumerate(images):
            # 1. Clean + decode base64
            image_bytes = base64.b64decode(image_base64)

            # 2. Construct object key
            key = f"{prefix}image_{reference_img}.png"

            # 3. Upload to S3
            s3_client.put_object(
                Bucket=bucket,
                Key=key,
                Body=image_bytes,
                ContentType="image/png",
            )

            logger.info(f"Uploaded s3://{bucket}/{key}")

        # In a real implementation, this is where you would process the image
        # For example, using Amazon Rekognition, Bedrock, or other services

        # Generate a result URL (in a real scenario, this would be the URL to the processed image)
        result_url = (
            f"https://{bucket}.s3.amazonaws.com/{key.replace('input', 'output')}"
        )
        # Generate pre-signed URL
        presigned_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=3600,
        )

        # Prepare the processing result
        processing_result = {
            "id": execution_id,
            "imageKey": key,
            "status": "COMPLETED",
            "presigned_url": presigned_url,
            "resultUrl": result_url,
            "processingType": "VIRTUAL_TRY_ON",
        }

        # Send event to EventBridge
        logger.info(f"Sending event to EventBridge bus: {EVENT_BUS_NAME}")

        response = events_client.put_events(
            Entries=[
                {
                    "Source": "virtual-tryon-service",
                    "DetailType": "ProcessingResult",
                    "Detail": json.dumps(processing_result),
                    "EventBusName": EVENT_BUS_NAME,
                }
            ]
        )

        logger.info(f"EventBridge response: {json.dumps(response)}")

        # Return the processing result
        return processing_result

    except Exception as e:
        error_message = f"Error processing image: {str(e)}"
        logger.info(error_message)

        # Send failure event to EventBridge
        failure_result = {
            "id": event.get("id", str(uuid.uuid4())),
            "imageKey": event.get("key", "unknown"),
            "status": "FAILED",
            "error": error_message,
            "processingType": "VIRTUAL_TRY_ON",
            "createdAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "updatedAt": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }

        try:
            events_client.put_events(
                Entries=[
                    {
                        "Source": "virtual-tryon-service",
                        "DetailType": "ProcessingResult",
                        "Detail": json.dumps(failure_result),
                        "EventBusName": EVENT_BUS_NAME,
                    }
                ]
            )
        except Exception as event_error:
            logger.info(
                f"Failed to send error event to EventBridge: {str(event_error)}"
            )

        # Re-raise the exception to mark the Lambda as failed
        raise e
