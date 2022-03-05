#
# Copyright 2022 Joel Knight <knight.joel@gmail.com>
#
#
# store_notion_page_fails.py
#
# An AWS Lambda function which pulls messages from the "new file" dead letter
# queue and stores the events in a DynamoDB table.
#
#
# Joel Knight
# www.joelknight.ca


import json
import os
from datetime import datetime as dt
from decimal import Decimal
from typing import Any, Dict
from urllib.parse import unquote_plus

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType
from aws_lambda_powertools.utilities.data_classes.s3_event import (
    S3Event,
    S3EventRecord,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord


logger = Logger(service=os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
processor = BatchProcessor(event_type=EventType.SQS)

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")

NOTION_PAGE_FAILS_TABLE = os.getenv("NOTION_PAGE_FAILS_TABLE")


def record_handler(record: SQSRecord) -> None:
    s3events = S3Event(json.loads(record.body))
    s3e: S3EventRecord

    if s3events.get("Event") == "s3:TestEvent":
        return

    for s3e in s3events.records:
        object_key = unquote_plus(s3e.s3.get_object.key)
        logger.info(
            "Processing failure for s3://{}/{} at event time {}".format(
                s3e.s3.bucket.name, object_key, s3e.event_time
            )
        )

        table = dynamodb.Table(NOTION_PAGE_FAILS_TABLE)
        event_time_usec = (
            dt.timestamp(dt.strptime(s3e.event_time, "%Y-%m-%dT%H:%M:%S.%fZ")) * 1000
        )
        logger.debug("Storing page fail in Dynamo for {}".format(object_key))
        table.put_item(
            Item={
                "S3ObjectKey": object_key,
                "EventTime": Decimal(event_time_usec),
                "S3BucketName": s3e.s3.bucket.name,
            }
        )


@logger.inject_lambda_context(log_event=True)
def handler(event: Dict, context: Any) -> Dict:
    batch = event["Records"]
    logger.info("Processing {} event record(s)".format(len(batch)))
    with processor(records=batch, handler=record_handler):
        processed_messages = processor.process()

    success = sum(r[0] == "success" for r in processed_messages)
    fail = sum(r[0] == "fail" for r in processed_messages)
    logger.info(
        "Processed {} records successfully; {} are being requeued".format(success, fail)
    )

    return processor.response()
