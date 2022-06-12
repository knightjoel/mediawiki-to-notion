#
# Copyright 2022 Joel Knight <knight.joel@gmail.com>
#
#
# notion_pages_custom_widget.py
#
# A Lambda function which implements a custom CloudWatch dashboard widget. The function
# pulls stats from the NotionPages table and returns them for CloudWatch to display.
#
#
# Joel Knight
# www.joelknight.ca


import os
from datetime import datetime as dt, timedelta, timezone
from typing import Any, Dict, List, Union

import boto3
from boto3.dynamodb.conditions import Attr
from aws_lambda_powertools import Logger


logger = Logger(service=os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

dynamodb = boto3.resource("dynamodb")

NOTION_PAGES_TABLE = os.getenv("NOTION_PAGES_TABLE")
DOCS = """
## Page Upload Status
Displays the status of pages being processed by the pipeline.

This widget takes no parameters.
"""


@logger.inject_lambda_context(log_event=True)
def handler(event: Dict, context: Any) -> Union[Dict, str]:
    if "describe" in event:
        return DOCS

    widget_context: Dict = event["widgetContext"]
    time_range: int = (
        widget_context["timeRange"]["zoom"]
        if "zoom" in widget_context["timeRange"]
        else widget_context["timeRange"]
    )

    if widget_context["timezone"]["label"] == "UTC":
        dashboard_tz = timezone.utc
    else:
        offset_ms = widget_context["timezone"]["offsetInMinutes"] * 60 * 1000
        offset_ms *= (
            -1 if widget_context["timezone"]["offsetISO"].startswith("-") else 1
        )
        dashboard_tz = timezone(timedelta(milliseconds=offset_ms), "Local")

    start = time_range["start"]
    end = time_range["end"]

    logger.debug(f"{dashboard_tz=}; {start=}; {end=}")

    table = dynamodb.Table(NOTION_PAGES_TABLE)
    response: Dict = table.scan(
        FilterExpression=Attr("StatusTime").between(start, end),
    )
    logger.debug(
        "Found {} items between {} and {}; LastEvaluatedKey is {}".format(
            response["Count"], start, end, response.get("LastEvaluatedKey")
        )
    )

    # Sort such that items which have a recent status time are at the top of the table.
    sorted_items: List = sorted(
        response["Items"], key=lambda item: item["StatusTime"], reverse=True
    )

    output: List = []
    output.append("| Time | Page | Status")
    output.append("|-----|-----|-----")
    for item in sorted_items:
        # StatusTime is in milliseconds.
        status_time_dt = dt.fromtimestamp(int(item["StatusTime"] / 1000), dashboard_tz)
        # Jan 01 01:02:03
        status_time = status_time_dt.strftime("%b %d %H:%M:%S")
        logger.debug(f'{item["S3ObjectKey"]}: {status_time_dt=}; {status_time=}')
        output.append(
            "| {} | {} | {} |".format(status_time, item["S3ObjectKey"], item["Status"])
        )

    return {"markdown": "\n".join(output)}
