import base64
import json
import os
import uuid
from random import randint

import boto3
from aws_lambda_powertools import Logger, Tracer

from aws_lambda_powertools.utilities.typing import LambdaContext

tracer = Tracer()
logger = Logger()
bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
s3 = boto3.client("s3")
bucket_name = os.environ.get("BUCKET_NAME")  # set this in Lambda env variables

prefix = "results/"


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context: LambdaContext):
    prompt = event["user_prompt"]
    logger.info(f"Received event: {json.dumps(prompt, indent=2)}")
    id = str(uuid.uuid4())

    try:
        uploaded_files = []

        # Configure the inference parameters.
        inference_params = {
            "taskType": "TEXT_IMAGE",
            "textToImageParams": {
                "text": prompt,
                "negativeText": "clouds, waves",  # List things to avoid
            },
            "imageGenerationConfig": {
                "numberOfImages": 2,  # Number of variations to generate. 1 to 5.
                "quality": "standard",  # Allowed values are "standard" and "premium"
                "width": 1280,  # See README for supported output resolutions
                "height": 720,  # See README for supported output resolutions
                "cfgScale": 7.0,  # How closely the prompt will be followed
                "seed": randint(0, 858993459),  # Use a random seed
            },
        }
        body_json = json.dumps(inference_params, indent=2)
        # Make the API call
        response = bedrock_client.invoke_model(
            body=body_json,
            modelId="amazon.nova-canvas-v1:0",
            accept="application/json",
            contentType="application/json",
        )

        response_body = json.loads(response.get("body").read())
        logger.info(response_body)
        logger.info(response_body["images"])
        logger.info(response_body["images"][0])

        for base64_str in response_body["images"]:
            try:
                # Optional: Clean up any data URI prefix
                if base64_str.startswith("data:image"):
                    base64_str = base64_str.split(",")[1]

                image_data = base64.b64decode(base64_str)
                file_id = str(uuid.uuid4())
                file_name = f"{prefix}image_{file_id}.png"
                logger.info(f"file is {file_name}")
                s3.put_object(
                    Bucket=bucket_name,
                    Key=file_name,
                    Body=image_data,
                    ContentType="image/jpeg",
                )
                presigned_url = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket_name, "Key": file_name},
                    ExpiresIn=3600,
                )
                uploaded_files.append(
                    {
                        "prompt": prompt,
                        "fileName": file_name,
                        "presigned_url": presigned_url,
                    }
                )
            except Exception as e:
                logger.error(f"Error saving image: {e}")

        return {"id": id, "uploaded_files": json.dumps(uploaded_files)}

    except Exception as e:
        logger.error(f"Unhandled error: {e}")
        return f"Unhandled error: {e}"
