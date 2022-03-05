#!/usr/bin/env python3

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from mw_to_notion.mw_to_notion_stack import MwToNotionStack


app = cdk.App()
MwToNotionStack(app, "MwToNotionStack")

cdk.Aspects.of(app).add(AwsSolutionsChecks())
app.synth()
