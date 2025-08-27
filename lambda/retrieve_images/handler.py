import json
import os

import boto3
from aws_lambda_powertools import Logger, Tracer

TABLE_NAME = os.environ["TABLE_NAME"]
logger = Logger(service="invoke_agent_lambda")
tracer = Tracer(service="invoke_agent_lambda")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event, context):
    """
    AppSync resolver for: Query.getImageList: ImageListsResult!
    Supports optional pagination with 'nextToken' argument.
    """
    args = event.get("arguments", {}) or {}
    logger.info(f"arguments {args}")

    scan_kwargs = {
        "Limit": 40,
    }

    resp = table.scan(**scan_kwargs)

    items = resp.get("Items", [])

    logger.info(f"retrieved item list {items}")
    out_items = []

    for it in items:
        # Expected table item shape (strings):
        # { "id": "...", "contentType": "Images", "imageListString": "<JSON string>" }
        raw_list_str = it.get("imageListString") or "[]"

        try:
            parsed_list = json.loads(raw_list_str)
            # Ensure each entry has prompt, fileName, presigned_url
            normalized = []
            for e in parsed_list:
                normalized.append(
                    {
                        "prompt": e.get("prompt", ""),
                        "fileName": e.get("fileName", ""),
                        "presigned_url": e.get("presigned_url", ""),
                    }
                )
        except Exception:
            # If the string is malformed, return an empty list instead of failing
            normalized = []

        out_items.append(
            {
                "id": it.get("id", ""),
                "contentType": it.get("contentType", ""),
                # GraphQL field is named imageListString but expects an ARRAY of imageItem
                "imageListString": normalized,
            }
        )
        logger.info(f"out items {out_items}")

    return {"items": out_items, "nextToken": ""}
