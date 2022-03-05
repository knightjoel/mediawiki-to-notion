#
# Copyright 2022 Joel Knight <knight.joel@gmail.com>
#
#
# upload_notion_blocks.py
#
# An AWS Lambda function which uploads blocks to Notion. The blocks were
# created by a separate Lambda function and stored in a DynamoDB table. The
# blocks are retrieved from the table, deserialized, and then uploaded
# using Notion's non-public API (the same API the apps use).
#
# The page to add the blocks to is looked up in a separate DynamoDB table. If
# no record is found, a new page is created and its ID stored in the table.
#
# This function processes a fixed number of blocks before exiting. This is to
# avoid the function hitting its timeout limit before it can process all of
# the blocks of large pages. An external process is responsible for running
# the function in a loop to work through all of the blocks in the database.
#
# The function retrieves the Notion token_v2 API secret from AWS Secrets
# Manager.
#
#
# Joel Knight
# www.joelknight.ca


import json
import os
import pickle
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Dict, Union

import boto3
from aws_lambda_powertools import Logger, Metrics as PTMetrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.metrics.base import MAX_METRICS
from aws_lambda_powertools.utilities import parameters
from boto3.dynamodb.conditions import Attr, Key
from md2notion.upload import uploadBlock
from notion.block import PageBlock
from notion.client import NotionClient
from requests.packages.urllib3.util.retry import Retry


logger = Logger(service=os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

notion_client = None
dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")

API_SECRET_NAME = os.getenv("API_SECRET_NAME")
MAX_BLOCKS_PARAM = os.getenv("MAX_BLOCKS_PARAM")
NOTION_BLOCKS_TABLE = os.getenv("NOTION_BLOCKS_TABLE")
NOTION_PAGES_TABLE = os.getenv("NOTION_PAGES_TABLE")


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


def get_or_make_page(batch_id: str, title: str, parent_page_url: str) -> PageBlock:
    logger.debug("Querying NotionPages table")
    global notion_client

    table = dynamodb.Table(NOTION_PAGES_TABLE)
    response = table.get_item(Key={"BlockBatch": batch_id})

    page = response.get("Item")
    if page:
        logger.debug("Page found at {}".format(page["PageUrl"]))
        return notion_client.get_block(page["PageUrl"])

    # Page doesn't exist yet. Create it.
    try:
        parent_page = notion_client.get_block(parent_page_url)
    except Exception:
        logger.exception(
            "Couldn't get Notion block for parent {}".format(parent_page_url)
        )
        raise

    logger.info("Creating new page '{}' under '{}'".format(title, parent_page.title))
    try:
        new_page = parent_page.children.add_new(PageBlock, title=title)
    except Exception:
        logger.exception("Couldn't create new page '{}'".format(title))
        raise

    try:
        response = table.put_item(
            Item={"BlockBatch": batch_id, "PageUrl": new_page.get_browseable_url()},
            ConditionExpression=Attr("PageUrl").not_exists(),
        )
    except Exception:
        logger.exception("Failed to store new page URL in the database")

    return new_page


def init_notion_client() -> None:
    global notion_client
    if notion_client:
        return

    logger.debug("Initializing NotionClient")

    retry = Retry(
        total=None,  # Don't retry for connection, redirect, or read errors.
        status=5,  # Retry count for 'status_forcelist' statuses.
        backoff_factor=0.2,
        # Backoff for throttles or server-side errors.
        status_forcelist=(429, 502, 503, 504),
        # CAUTION: adding 'POST' to this list which is not technically idempotent
        method_whitelist=(
            "POST",
            "HEAD",
            "TRACE",
            "GET",
            "PUT",
            "OPTIONS",
            "DELETE",
        ),
        raise_on_status=True,  # Raise MaxRetryError if max retry count exceeded.
    )

    notion_client = NotionClient(
        token_v2=parameters.get_secret(API_SECRET_NAME), client_specified_retry=retry
    )


def record_handler(record: Dict) -> bool:
    logger.debug("Processing {}#{}".format(record["BlockBatch"], record["BlockIndex"]))

    # Storage for images, PDFs, etc, which are embedded in the block.
    tmpdir = "/tmp/" + record["BlockBatch"] + "/"
    try:
        os.mkdir(tmpdir, mode=0o700)
    except FileExistsError:
        pass

    # Custom file getter function which will retrieve embedded files from
    # the content staging bucket.
    def get_image_object(
        relative_path: str, md_file_path: str = None
    ) -> Union[str, Path]:
        if "://" in relative_path:
            return relative_path

        try:
            if "/" in relative_path:
                file_name = relative_path.rsplit("/", 1)[1]
            else:
                file_name = relative_path
            logger.debug("Downloading {}".format(tmpdir + file_name))
            s3_client.download_file(
                record["S3BucketName"], "File/" + relative_path, tmpdir + file_name
            )
        except Exception:
            logger.exception("Unable to download file '{}'".format(relative_path))
            raise

        return Path(tmpdir) / Path(file_name)

    # Download parenturl.txt and pull the URL of the page which is actually
    # the grandparent of this block and the parent of this block's parent
    # page.
    parent_page_filename = record["S3ObjectKey"].rsplit("/", 1)[0] + "/parenturl.txt"
    logger.debug("Downloading {}".format(parent_page_filename))
    r = s3_client.get_object(Bucket=record["S3BucketName"], Key=parent_page_filename)
    parent_page_url = r["Body"].read().decode("utf-8").strip()
    logger.debug("Parent page url: {}".format(parent_page_url))

    # Prepare to talk to the Notion API.
    try:
        init_notion_client()
    except Exception:
        logger.exception("Failed to initialize Notion client")
        raise

    title = os.path.basename(record["S3ObjectKey"])
    if title.endswith(".md"):
        title = title[:-3]
    new_page = get_or_make_page(record["BlockBatch"], title, parent_page_url)

    # Upload the block to its page.
    block = pickle.loads(record["BlockContent"].value)
    try:
        uploadBlock(block, new_page, None, imagePathFunc=get_image_object)
    except Exception:
        logger.exception(
            "Unable to upload block {}#{}".format(
                record["BlockBatch"], record["BlockIndex"]
            )
        )
        raise
    finally:
        # Clean up any embedded files which were downloaded to avoid unbounded
        # use of the limited disk space Lambda functions have.
        shutil.rmtree(tmpdir)

    return True


@metrics.log_metrics
@logger.inject_lambda_context(log_event=True)
def handler(event: Dict, context: Dict) -> None:
    logger.info("Processing BlockBatch {}".format(event["BlockBatch"]))

    max_blocks = int(parameters.get_parameter(MAX_BLOCKS_PARAM))
    table = dynamodb.Table(NOTION_BLOCKS_TABLE)
    response = table.query(
        Limit=max_blocks,
        KeyConditionExpression=Key("BlockBatch").eq(event["BlockBatch"]),
        ScanIndexForward=True,
    )
    logger.info(
        "Retrieved {} record(s) for this batch of blocks".format(response["Count"])
    )

    processed_messages = []
    for record in response["Items"]:
        try:
            r = record_handler(record)
        except Exception:
            logger.exception(
                "Caught an exception for record {}#{}".format(
                    record["BlockBatch"], record["BlockIndex"]
                )
            )
            processed_messages.append(("fail", record))
        else:
            logger.debug(
                "Deleting record {}#{} from database".format(
                    record["BlockBatch"], record["BlockIndex"]
                )
            )
            table.delete_item(
                Key={
                    "BlockBatch": record["BlockBatch"],
                    "BlockIndex": record["BlockIndex"],
                }
            )
            if r:
                processed_messages.append(("success", record))

    success = sum(r[0] == "success" for r in processed_messages)
    fail = sum(r[0] == "fail" for r in processed_messages)

    logger.info(
        "Processed {}/{} blocks successfully".format(success, len(processed_messages))
    )
    metrics.add_metric(
        name="SuccessfulBlockUploads", unit=MetricUnit.Count, value=success
    )
    metrics.add_metric(
        name="UnsuccessfulBlockUploads", unit=MetricUnit.Count, value=fail
    )
