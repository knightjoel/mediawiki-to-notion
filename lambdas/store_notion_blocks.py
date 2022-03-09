#
# Copyright 2022 Joel Knight <knight.joel@gmail.com>
#
#
# store_notion_blocks.py
#
# An AWS Lambda function which renders Markdown files into Notion blocks and
# stores the rendered blocks in DynamoDB. The function is triggered by an
# Amazon S3 PutObject event for an object matching the pattern '*.md'.
#
#
# Joel Knight
# www.joelknight.ca


import json
import os
import pickle
import shutil
import uuid
from collections import defaultdict
from typing import Any, Dict, Union
from urllib.parse import unquote_plus

import boto3
from aws_lambda_powertools import Logger, Metrics as PTMetrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.metrics.base import MAX_METRICS
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType
from aws_lambda_powertools.utilities.data_classes.s3_event import (
    S3Event,
    S3EventRecord,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from boto3.dynamodb.conditions import Attr
from md2notion.upload import convert
from notion.block import TextBlock


logger = Logger(service=os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
processor = BatchProcessor(event_type=EventType.SQS)

dynamodb = boto3.resource("dynamodb")
events = boto3.client("events")
s3_client = boto3.client("s3")

EVENT_BUS_NAME = os.getenv("EVENT_BUS_NAME")
NOTION_BLOCKS_TABLE = os.getenv("NOTION_BLOCKS_TABLE")


class Metrics(PTMetrics):
    def add_metric(self, name: str, unit: Union[MetricUnit, str], value: float) -> None:
        # Work around a bug in Lambda Powertools: if the number of metric values
        # reaches a threshold, output the metrics. If the number of values is
        # allowed to cross the threshold (which Powertools does allow),
        # CloudWatch will silently discard all metrics logged in that EMF blob.
        metric: Dict = self.metric_set.get(name, defaultdict(list))
        if len(metric["Value"]) == MAX_METRICS:
            logger.debug(
                f"Exceeded maximum of {MAX_METRICS} metric values"
                f" for metric {name} - Publishing existing values"
            )
            metrics = self.serialize_metric_set()
            print(json.dumps(metrics))
            self.metric_set.clear()
        super().add_metric(name=name, unit=unit, value=value)


metrics = Metrics()


def record_handler(record: SQSRecord) -> None:
    s3events = S3Event(json.loads(record.body))
    s3e: S3EventRecord

    if s3events.get("Event") == "s3:TestEvent":
        return

    for s3e in s3events.records:
        batch_id = str(uuid.uuid4())
        unquote_key = unquote_plus(s3e.s3.get_object.key)

        logger.info(
            "Processing s3://{}/{} as batch {}".format(
                s3e.s3.bucket.name, unquote_key, batch_id
            )
        )

        if "/" in unquote_key:
            md_filename = unquote_key.rsplit("/", 1)[1]
        else:
            md_filename = unquote_key
        tmpdir = "/tmp/" + batch_id + "/"
        try:
            os.mkdir(tmpdir, mode=0o700)
        except FileExistsError:
            pass

        try:
            # Download .md object.
            logger.debug("Downloading {}".format(tmpdir + md_filename))
            s3_client.download_file(
                s3e.s3.bucket.name,
                unquote_key,
                tmpdir + md_filename,
            )

            logger.debug("Opening {}".format(tmpdir + md_filename))
            with open(tmpdir + md_filename, "r", encoding="utf-8") as md_file:
                rendered = convert(md_file)

            table = dynamodb.Table(NOTION_BLOCKS_TABLE)
            logger.debug("Converting {} to Notion blocks".format(tmpdir + md_filename))
            for idx, block in enumerate(rendered):
                # In certain cases, pandoc inserts HTML comments into the Markdown
                # document which Notion then renders as literal text on the page.
                # If we suppress creating a block for these comments, they don't go
                # into the Notion page and there are no side effects to how Notion
                # renders the document.
                # ref: https://pandoc.org/MANUAL.html#ending-a-list
                if block.get("type") is TextBlock and block.get("title") == "<!-- -->":
                    continue
                table.put_item(
                    Item={
                        "BlockBatch": batch_id,
                        "BlockIndex": idx,
                        "S3BucketName": s3e.s3.bucket.name,
                        "S3ObjectKey": unquote_key,
                        "BlockContent": pickle.dumps(block),
                    },
                    ConditionExpression=Attr("BlockBatch").not_exists(),
                )
                metrics.add_metric(name="BlocksStored", unit=MetricUnit.Count, value=1)

        finally:
            # Delete Markdown object.
            shutil.rmtree(tmpdir)

        # Signal that a new batch of blocks is ready. This event will trigger
        # the upload step function.
        events.put_events(
            Entries=[
                {
                    "DetailType": "StoreNotionBlocks",
                    "Detail": json.dumps(
                        {"blockBatch": batch_id, "status": ["SUCCESS"]}
                    ),
                    "EventBusName": EVENT_BUS_NAME,
                    "Source": os.getenv("AWS_LAMBDA_FUNCTION_NAME"),
                }
            ]
        )


@metrics.log_metrics
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
    metrics.add_metric(
        name="SuccessfulPageConversions", unit=MetricUnit.Count, value=success
    )
    metrics.add_metric(
        name="UnsuccessfulPageConversions", unit=MetricUnit.Count, value=fail
    )

    return processor.response()
