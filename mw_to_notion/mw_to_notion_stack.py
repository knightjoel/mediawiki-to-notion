#
# Copyright 2022 Joel Knight <knight.joel@gmail.com>
#
#
# This file defines the CDK stack which contains the infrastructure to run
# the MediaWiki-to-Notion import pipeline.
#
# Joel Knight
# www.joelknight.ca


import json
import subprocess

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_cloudwatch as cloudwatch,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lse,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secret,
    aws_sqs as sqs,
    aws_ssm as ssm,
    aws_stepfunctions as stepfunctions,
)
from aws_cdk.aws_s3_notifications import SqsDestination
from cdk_nag import NagSuppressions
from constructs import Construct


class MwToNotionStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self._powertools_layer_version = 11
        self._max_blocks_default = 200
        self._metric_namespace = "MediaWikiToNotionApp"
        self._store_blocks_tmout = 300
        self._store_page_fails_tmout = 300
        self._upload_blocks_tmout = 600

        # SSM and ParameterStore parameters
        ###################################
        api_secret = secret.Secret(
            self,
            "NotionApiSecret",
            description="Notion token_v2 API key",
            removal_policy=RemovalPolicy.DESTROY,
        )
        NagSuppressions.add_resource_suppressions(
            api_secret,
            [
                {"id": "AwsSolutions-SMG4", "reason": "Secret must be rotated by hand"},
            ],
        )

        max_blocks_param = ssm.StringParameter(
            self,
            "MaxBlocksParam",
            string_value=str(self._max_blocks_default),
            allowed_pattern=r"^\d+$",
            description="Maximum number of blocks to process per Lambda invocation",
            parameter_name="/MwToNotion/MaxBlocks",
        )

        # SQS Queues
        ############
        dlqueue = sqs.Queue(
            self,
            "MwToNotionQueueDlq",
            encryption=sqs.QueueEncryption.KMS_MANAGED,
            queue_name="MediawikiToNotionUploadQueueDlq",
            retention_period=Duration.days(14),
            visibility_timeout=Duration.seconds(self._store_page_fails_tmout + 120),
        )
        dlqueue.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=["*"],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
                effect=iam.Effect.DENY,
            )
        )
        NagSuppressions.add_resource_suppressions(
            dlqueue,
            [
                {"id": "AwsSolutions-SQS3", "reason": "This is a dead letter queue"},
            ],
        )

        queue = sqs.Queue(
            self,
            "MwToNotionQueue",
            # XXX The S3 API throws an error when encryption is enabled on the
            # queue (Unable to validate the following destination
            # configurations).
            # encryption=sqs.QueueEncryption.KMS_MANAGED,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=dlqueue),
            queue_name="MediawikiToNotionUploadQueue",
            # One day seems sane to me. I would expect a batch of pages to
            # upload in much less time and if the batch does approach 1 day,
            # I need to build a smaller batch or investigate why the pages
            # aren't leaving within the retention period.
            retention_period=Duration.days(1),
            visibility_timeout=Duration.seconds(self._store_blocks_tmout + 120),
        )
        queue.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=["*"],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
                effect=iam.Effect.DENY,
            )
        )
        NagSuppressions.add_resource_suppressions(
            queue,
            [
                {
                    "id": "AwsSolutions-SQS2",
                    "reason": "Encryption conflicts with S3's ability to send a test event",  # noqa: E501
                },
            ],
        )

        # DynamoDB tables
        #################
        notion_blocks_table = ddb.Table(
            self,
            "NotionBlocksTable",
            partition_key=ddb.Attribute(
                name="BlockBatch", type=ddb.AttributeType.STRING
            ),
            sort_key=ddb.Attribute(name="BlockIndex", type=ddb.AttributeType.NUMBER),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            table_name="NotionBlocks",
        )
        NagSuppressions.add_resource_suppressions(
            notion_blocks_table,
            [
                {"id": "AwsSolutions-DDB3", "reason": "PiT recovery not required"},
            ],
        )

        notion_pages_table = ddb.Table(
            self,
            "NotionPagesTable",
            partition_key=ddb.Attribute(
                name="BlockBatch", type=ddb.AttributeType.STRING
            ),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            table_name="NotionPages",
        )
        NagSuppressions.add_resource_suppressions(
            notion_pages_table,
            [
                {"id": "AwsSolutions-DDB3", "reason": "PiT recovery not required"},
            ],
        )

        notion_page_failures_table = ddb.Table(
            self,
            "NotionPageFailuresTable",
            partition_key=ddb.Attribute(
                name="S3ObjectKey", type=ddb.AttributeType.STRING
            ),
            sort_key=ddb.Attribute(name="EventTime", type=ddb.AttributeType.NUMBER),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            table_name="NotionPageFailures",
        )
        NagSuppressions.add_resource_suppressions(
            notion_page_failures_table,
            [
                {"id": "AwsSolutions-DDB3", "reason": "PiT recovery not required"},
            ],
        )

        semaphore_table = ddb.Table(
            self,
            "SemaphoreTable",
            partition_key=ddb.Attribute(name="LockName", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            table_name="MwToNotionStateMachineLock",
        )
        NagSuppressions.add_resource_suppressions(
            semaphore_table,
            [
                {"id": "AwsSolutions-DDB3", "reason": "PiT recovery not required"},
            ],
        )

        # Lambda functons
        #################
        subprocess.check_call(
            "pip install -r lambdas/requirements.txt -t lambdas --upgrade".split()
        )

        powertools_layer = lambda_.LayerVersion.from_layer_version_arn(
            self,
            "PowerToolsLayer",
            "arn:aws:lambda:{}:017000801446:layer:AWSLambdaPowertoolsPython:{}".format(
                self.region, self._powertools_layer_version
            ),
        )

        block_store_function = lambda_.Function(
            self,
            "StoreNotionBlocks",
            code=lambda_.Code.from_asset("lambdas/"),
            runtime=lambda_.Runtime.PYTHON_3_8,
            handler="store_notion_blocks.handler",
            architecture=lambda_.Architecture.ARM_64,
            description="MediaWiki-to-Notion - render and store Notion blocks",
            environment={
                "NOTION_BLOCKS_TABLE": notion_blocks_table.table_name,
                "NOTION_DATA_DIR": "/tmp/notion-py",
                "POWERTOOLS_METRICS_NAMESPACE": self._metric_namespace,
                # Service name must match function name: the dashboard infers
                # the service name from the function name.
                "POWERTOOLS_SERVICE_NAME": "StoreNotionBlocks",
            },
            events=[lse.SqsEventSource(queue)],
            function_name="StoreNotionBlocks",
            layers=[powertools_layer],
            timeout=Duration.seconds(self._store_blocks_tmout),
        )
        NagSuppressions.add_resource_suppressions(
            block_store_function,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "Uses policy/service-role/AWSLambdaBasicExecutionRole",
                },
            ],
            True,
        )

        queue.grant_consume_messages(block_store_function.grant_principal)
        notion_blocks_table.grant(
            block_store_function.grant_principal, "dynamodb:PutItem"
        )

        block_upload_function = lambda_.Function(
            self,
            "UploadNotionBlocks",
            code=lambda_.Code.from_asset("lambdas/"),
            runtime=lambda_.Runtime.PYTHON_3_8,
            handler="upload_notion_blocks.handler",
            architecture=lambda_.Architecture.ARM_64,
            description="MediaWiki-to-Notion - upload blocks to Notion",
            environment={
                "MAX_BLOCKS_PARAM": max_blocks_param.parameter_name,
                "API_SECRET_NAME": api_secret.secret_name,
                "NOTION_BLOCKS_TABLE": notion_blocks_table.table_name,
                "NOTION_PAGES_TABLE": notion_pages_table.table_name,
                "NOTION_DATA_DIR": "/tmp/notion-py",
                "POWERTOOLS_METRICS_NAMESPACE": self._metric_namespace,
                # Service name must match function name: the dashboard infers
                # the service name from the function name.
                "POWERTOOLS_SERVICE_NAME": "UploadNotionBlocks",
            },
            function_name="UploadNotionBlocks",
            layers=[powertools_layer],
            timeout=Duration.seconds(self._upload_blocks_tmout),
        )
        NagSuppressions.add_resource_suppressions(
            block_upload_function,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "Uses policy/service-role/AWSLambdaBasicExecutionRole",
                },
            ],
            True,
        )

        api_secret.grant_read(block_upload_function.grant_principal)
        max_blocks_param.grant_read(block_upload_function.grant_principal)
        notion_blocks_table.grant(
            block_upload_function.grant_principal,
            "dynamodb:DeleteItem",
            "dynamodb:Query",
        )
        notion_pages_table.grant(
            block_upload_function.grant_principal,
            "dynamodb:GetItem",
            "dynamodb:PutItem",
        )

        page_fails_function = lambda_.Function(
            self,
            "StoreNotionPageFails",
            code=lambda_.Code.from_asset("lambdas/"),
            runtime=lambda_.Runtime.PYTHON_3_8,
            handler="store_notion_page_fails.handler",
            architecture=lambda_.Architecture.ARM_64,
            description=(
                "MediaWiki-to-Notion - record pages which were not"
                " successfully processed from the queue"
            ),
            environment={
                "NOTION_PAGE_FAILS_TABLE": notion_page_failures_table.table_name
            },
            events=[lse.SqsEventSource(dlqueue)],
            function_name="StoreNotionPageFails",
            layers=[powertools_layer],
            timeout=Duration.seconds(self._store_page_fails_tmout),
        )
        NagSuppressions.add_resource_suppressions(
            page_fails_function.grant_principal,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "Uses policy/service-role/AWSLambdaBasicExecutionRole",
                },
            ],
        )

        dlqueue.grant_consume_messages(page_fails_function.grant_principal)
        notion_page_failures_table.grant(
            page_fails_function.grant_principal, "dynamodb:PutItem"
        )

        # CloudWatch Log Groups
        #######################

        # Be explicit about the Lambda function log groups so that when the
        # stack is destroyed, it takes the log group with it.
        logs.LogGroup(
            self,
            "BlockStoreFunctionLogGroup",
            log_group_name="/aws/lambda/" + block_store_function.function_name,
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.INFINITE,
        )
        logs.LogGroup(
            self,
            "BlockUploadFunctionLogGroup",
            log_group_name="/aws/lambda/" + block_upload_function.function_name,
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.INFINITE,
        )
        logs.LogGroup(
            self,
            "StorePageFailsFunctionLogGroup",
            log_group_name="/aws/lambda/" + page_fails_function.function_name,
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.INFINITE,
        )

        # S3 bucket
        ###########
        bucket = s3.Bucket(
            self,
            "ContentStagingBucket",
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            bucket_name="mw-to-notion-content-staging-" + self.account,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            SqsDestination(queue),
            s3.NotificationKeyFilter(suffix=".md"),
        )
        NagSuppressions.add_resource_suppressions(
            bucket,
            [
                {"id": "AwsSolutions-S1", "reason": "Access logs not required"},
            ],
        )
        NagSuppressions.add_resource_suppressions_by_path(
            self,
            "/MwToNotionStack/BucketNotificationsHandler050a0587b7544547bf325f094a3db834/Role",  # noqa: E501
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "Role created by CDK bucket construct",
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Policy created by CDK bucket construct",
                },
            ],
            True,
        )

        bucket.grant_read(block_store_function.grant_principal)
        NagSuppressions.add_resource_suppressions(
            block_store_function,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Granted read to all objects in the staging bucket",
                    "applies_to": "Action::s3:GetObject*",
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Granted read to all objects in the staging bucket",
                    "applies_to": "Action::s3:GetBucket*",
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Granted read to all objects in the staging bucket",
                    "applies_to": "Action::s3:List*",
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Permissions granted to all objects in the bucket",
                    "applies_to": "Resource::<ContentStagingBucket8FD54405.Arn>/*",
                },
            ],
            True,
        )

        bucket.grant_read(block_upload_function.grant_principal)
        NagSuppressions.add_resource_suppressions(
            block_upload_function,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Granted read to all objects in the staging bucket",
                    "applies_to": "Action::s3:GetObject*",
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Granted read to all objects in the staging bucket",
                    "applies_to": "Action::s3:GetBucket*",
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Granted read to all objects in the staging bucket",
                    "applies_to": "Action::s3:List*",
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Permissions granted to all objects in the bucket",
                    "applies_to": "Resource::<ContentStagingBucket8FD54405.Arn>/*",
                },
            ],
            True,
        )

        # Step Functions
        ################
        cleanup_state_machine_role = iam.Role(
            self,
            "CleanupStateMachineRole",
            assumed_by=iam.ServicePrincipal("states." + self.region + ".amazonaws.com"),
        )
        with open("state_machines/cleanup-state-machine.asl.json", "r") as fh:
            cleanup_asl = json.load(fh)
        cleanup_state_machine = stepfunctions.CfnStateMachine(
            self,
            "CleanupStateMachine",
            definition_string=json.dumps(cleanup_asl),
            definition_substitutions={
                "LockName": "Semaphore",
                "TableSemaphore": semaphore_table.table_name,
            },
            role_arn=cleanup_state_machine_role.role_arn,
            state_machine_name="NotionUploadLockCleaner",
        )
        NagSuppressions.add_resource_suppressions(
            cleanup_state_machine,
            [
                {
                    "id": "AwsSolutions-SF1",
                    "reason": "CW Logs not needed for Standard Workflows",
                },
                {"id": "AwsSolutions-SF2", "reason": "X-Ray not needed by default"},
            ],
        )

        semaphore_table.grant(
            cleanup_state_machine_role.grant_principal,
            "dynamodb:GetItem",
            "dynamodb:UpdateItem",
        )

        upload_state_machine_role = iam.Role(
            self,
            "UploadStateMachineRole",
            assumed_by=iam.ServicePrincipal("states." + self.region + ".amazonaws.com"),
        )
        with open("state_machines/upload-state-machine.asl.json", "r") as fh:
            upload_asl = json.load(fh)
        upload_state_machine = stepfunctions.CfnStateMachine(
            self,
            "UploadStateMachine",
            definition_string=json.dumps(upload_asl),
            definition_substitutions={
                "ConcurrentAccessLimit": "1",
                "LockName": "Semaphore",
                "TableBlocks": notion_blocks_table.table_name,
                "TableSemaphore": semaphore_table.table_name,
                "UploadFunction": block_upload_function.function_name,
            },
            role_arn=upload_state_machine_role.role_arn,
            state_machine_name="NotionUpload",
        )
        NagSuppressions.add_resource_suppressions(
            upload_state_machine,
            [
                {
                    "id": "AwsSolutions-SF1",
                    "reason": "CW Logs not needed for Standard Workflows",
                },
                {"id": "AwsSolutions-SF2", "reason": "X-Ray not needed by default"},
            ],
        )

        block_upload_function.grant_invoke(upload_state_machine_role.grant_principal)
        notion_blocks_table.grant_read_data(upload_state_machine_role.grant_principal)
        semaphore_table.grant(
            upload_state_machine_role.grant_principal,
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:Scan",
            "dynamodb:UpdateItem",
        )

        # EventBridge
        #############
        events_role = iam.Role(
            self,
            "MwToNotionEventsRole",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
            inline_policies=[
                iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["states:StartExecution"],
                            resources=[
                                cleanup_state_machine.attr_arn,
                                upload_state_machine.attr_arn,
                            ],
                            effect=iam.Effect.ALLOW,
                        )
                    ]
                )
            ],
        )

        cleanup_state_rule = events.CfnRule(
            self,
            "CleanupNotionUploadState",
            description=(
                "Cleans up dangling locks not released by the"
                " {} state machine".format(upload_state_machine.attr_name)
            ),
            event_pattern={
                "detail": {
                    "stateMachineArn": [upload_state_machine.attr_arn],
                    "status": ["ABORTED", "FAILED", "TIMED_OUT"],
                },
                "source": ["aws.states"],
            },
            targets=[
                events.CfnRule.TargetProperty(
                    arn=cleanup_state_machine.attr_arn,
                    id=cleanup_state_machine.attr_name,
                    role_arn=events_role.role_arn,
                )
            ],
        )

        event_bus = events.EventBus(
            self, "EventBus", event_bus_name="MwToNotionEventBus"
        )
        event_bus.grant_put_events_to(block_store_function.grant_principal)
        block_store_function.add_environment("EVENT_BUS_NAME", event_bus.event_bus_name)

        events.CfnRule(
            self,
            "TriggerNotionUploads",
            description=(
                "Triggers the {} state machine".format(upload_state_machine.attr_name)
            ),
            event_bus_name=event_bus.event_bus_name,
            event_pattern={
                "detail": {"status": ["SUCCESS"]},
                "source": [block_store_function.function_name],
            },
            targets=[
                events.CfnRule.TargetProperty(
                    arn=upload_state_machine.attr_arn,
                    id=upload_state_machine.attr_name,
                    role_arn=events_role.role_arn,
                )
            ],
        )

        # CloudWatch Dashboard
        ######################
        colors = {
            "blue": "#1f77b4",
            "grey": "#aec7e8",
            "green": "#2ca02c",
            "orange": "#ff7f0e",
            "purple": "#9467bd",
            "red": "#d62728",
            "yellow": "#dbdb8d",
        }
        graph_widget_height = 6
        graph_widget_width = 12
        heading_widget_height = 1
        heading_widget_width = 24
        sv_widget_height = 3
        sv_widget_width = 4

        dashboard = cloudwatch.Dashboard(
            self, "Dashboard", dashboard_name="MediaWikiToNotion"
        )

        # Single value widgets
        dashboard.add_widgets(
            cloudwatch.SingleValueWidget(
                title="Blocks Stored",
                height=sv_widget_height,
                width=sv_widget_width,
                set_period_to_time_range=True,
                metrics=[
                    cloudwatch.Metric(
                        namespace=self._metric_namespace,
                        metric_name="BlocksStored",
                        dimensions_map={"service": block_store_function.function_name},
                        period=Duration.minutes(1),
                        statistic="Sum",
                    )
                ],
            ),
            cloudwatch.SingleValueWidget(
                title="Blocks Uploaded",
                height=sv_widget_height,
                width=sv_widget_width,
                set_period_to_time_range=True,
                metrics=[
                    cloudwatch.Metric(
                        namespace=self._metric_namespace,
                        metric_name="SuccessfulBlockUploads",
                        dimensions_map={"service": block_upload_function.function_name},
                        period=Duration.minutes(1),
                        statistic="Sum",
                    )
                ],
            ),
            cloudwatch.SingleValueWidget(
                title="Upload State Machine",
                height=sv_widget_height,
                width=sv_widget_width * 3,
                set_period_to_time_range=True,
                metrics=[
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsFailed",
                        label="Failures",
                        color=colors["red"],
                        dimensions_map={
                            "StateMachineArn": upload_state_machine.attr_arn
                        },
                        period=Duration.minutes(1),
                        statistic="Sum",
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsTimedOut",
                        label="Time Outs",
                        color=colors["purple"],
                        dimensions_map={
                            "StateMachineArn": upload_state_machine.attr_arn
                        },
                        period=Duration.minutes(1),
                        statistic="Sum",
                    ),
                ],
            ),
            cloudwatch.SingleValueWidget(
                title="Store Fails",
                height=sv_widget_height,
                width=sv_widget_width,
                set_period_to_time_range=True,
                metrics=[
                    page_fails_function.metric_invocations(
                        color=colors["red"], period=Duration.minutes(1)
                    )
                ],
            ),
        )

        # State machines header
        dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown="# State Machines",
                height=heading_widget_height,
                width=heading_widget_width,
            )
        )

        # State machines
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="NotionUpload",
                height=graph_widget_height,
                width=graph_widget_width,
                period=Duration.minutes(1),
                statistic="Sum",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsStarted",
                        dimensions_map={
                            "StateMachineArn": upload_state_machine.attr_arn
                        },
                        color=colors["blue"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsSucceeded",
                        dimensions_map={
                            "StateMachineArn": upload_state_machine.attr_arn
                        },
                        color=colors["green"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsFailed",
                        dimensions_map={
                            "StateMachineArn": upload_state_machine.attr_arn
                        },
                        color=colors["red"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsThrottled",
                        dimensions_map={
                            "StateMachineArn": upload_state_machine.attr_arn
                        },
                        color=colors["orange"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsAborted",
                        dimensions_map={
                            "StateMachineArn": upload_state_machine.attr_arn
                        },
                        color=colors["grey"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsTimedOut",
                        dimensions_map={
                            "StateMachineArn": upload_state_machine.attr_arn
                        },
                        color=colors["purple"],
                    ),
                ],
                left_y_axis=cloudwatch.YAxisProps(label="Blocks", show_units=False),
            ),
            cloudwatch.GraphWidget(
                title="NotionUploadLockCleaner",
                height=graph_widget_height,
                width=graph_widget_width,
                period=Duration.minutes(1),
                statistic="Sum",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsStarted",
                        dimensions_map={
                            "StateMachineArn": cleanup_state_machine.attr_arn
                        },
                        color=colors["blue"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsSucceeded",
                        dimensions_map={
                            "StateMachineArn": cleanup_state_machine.attr_arn
                        },
                        color=colors["green"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsFailed",
                        dimensions_map={
                            "StateMachineArn": cleanup_state_machine.attr_arn
                        },
                        color=colors["red"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsThrottled",
                        dimensions_map={
                            "StateMachineArn": cleanup_state_machine.attr_arn
                        },
                        color=colors["orange"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsAborted",
                        dimensions_map={
                            "StateMachineArn": cleanup_state_machine.attr_arn
                        },
                        color=colors["grey"],
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/States",
                        metric_name="ExecutionsTimedOut",
                        dimensions_map={
                            "StateMachineArn": cleanup_state_machine.attr_arn
                        },
                        color=colors["purple"],
                    ),
                ],
                left_y_axis=cloudwatch.YAxisProps(label="Blocks", show_units=False),
            ),
        )

        # Lambda header
        dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown="# Lambda Functions",
                height=heading_widget_height,
                width=heading_widget_width,
            )
        )

        # StoreNotionBlocks graphs
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="StoreNotionBlocks: Operations",
                height=graph_widget_height,
                width=graph_widget_width,
                period=Duration.minutes(1),
                statistic="Sum",
                left=[
                    cloudwatch.Metric(
                        namespace=self._metric_namespace,
                        metric_name="BlocksStored",
                        dimensions_map={"service": block_store_function.function_name},
                        color=colors["blue"],
                    )
                ],
                left_y_axis=cloudwatch.YAxisProps(label="Blocks", show_units=False),
                right=[
                    cloudwatch.Metric(
                        namespace=self._metric_namespace,
                        metric_name="SuccessfulPageConversions",
                        dimensions_map={"service": block_store_function.function_name},
                        color=colors["green"],
                    ),
                    cloudwatch.Metric(
                        namespace=self._metric_namespace,
                        metric_name="UnsuccessfulPageConversions",
                        dimensions_map={"service": block_store_function.function_name},
                        color=colors["red"],
                    ),
                ],
                right_y_axis=cloudwatch.YAxisProps(label="Pages", show_units=False),
            ),
            cloudwatch.GraphWidget(
                title="StoreNotionBlocks: Lambda Metrics",
                height=graph_widget_height,
                width=graph_widget_width,
                period=Duration.minutes(1),
                statistic="Sum",
                left=[
                    block_store_function.metric_invocations(
                        color=colors["green"], period=Duration.minutes(1)
                    ),
                    block_store_function.metric_errors(
                        color=colors["red"], period=Duration.minutes(1)
                    ),
                    block_store_function.metric_throttles(
                        color=colors["grey"], period=Duration.minutes(1)
                    ),
                    block_store_function.metric(
                        "ConcurrentExecutions",
                        color=colors["purple"],
                        period=Duration.minutes(1),
                    ),
                ],
                right=[
                    block_store_function.metric_duration(
                        color=colors["yellow"], period=Duration.minutes(1)
                    )
                ],
            ),
        )

        # UploadNotionBlocks graphs
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="UploadNotionBlocks: Operations",
                height=graph_widget_height,
                width=graph_widget_width,
                period=Duration.minutes(1),
                statistic="Sum",
                left=[
                    cloudwatch.Metric(
                        namespace=self._metric_namespace,
                        metric_name="SuccessfulBlockUploads",
                        dimensions_map={"service": block_upload_function.function_name},
                        color=colors["green"],
                    ),
                    cloudwatch.Metric(
                        namespace=self._metric_namespace,
                        metric_name="UnsuccessfulBlockUploads",
                        dimensions_map={"service": block_upload_function.function_name},
                        color=colors["red"],
                    ),
                ],
                left_y_axis=cloudwatch.YAxisProps(label="Blocks", show_units=False),
            ),
            cloudwatch.GraphWidget(
                title="UploadNotionBlocks: Lambda Metrics",
                height=graph_widget_height,
                width=graph_widget_width,
                period=Duration.minutes(1),
                statistic="Sum",
                left=[
                    block_upload_function.metric_invocations(
                        color=colors["green"], period=Duration.minutes(1)
                    ),
                    block_upload_function.metric_errors(
                        color=colors["red"], period=Duration.minutes(1)
                    ),
                    block_upload_function.metric_throttles(
                        color=colors["grey"], period=Duration.minutes(1)
                    ),
                    block_upload_function.metric(
                        "ConcurrentExecutions",
                        color=colors["purple"],
                        period=Duration.minutes(1),
                    ),
                ],
                right=[
                    block_upload_function.metric_duration(
                        color=colors["yellow"], period=Duration.minutes(1)
                    )
                ],
            ),
        )

        # Queues header
        dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown="# Queues",
                height=heading_widget_height,
                width=heading_widget_width,
            )
        )

        # File upload queue
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="File Upload Queue",
                height=graph_widget_height,
                width=graph_widget_width,
                statistic="Sum",
                period=Duration.minutes(1),
                left=[
                    queue.metric_approximate_number_of_messages_visible(
                        color=colors["blue"], period=Duration.minutes(1)
                    ),
                    queue.metric_approximate_number_of_messages_not_visible(
                        color=colors["green"], period=Duration.minutes(1)
                    ),
                ],
                left_y_axis=cloudwatch.YAxisProps(label="Messages", show_units=False),
            )
        )

        # Admin stuff
        #############
        Tags.of(self).add("Workload", "MediaWiki-to-Notion")

        CfnOutput(self, "ApiKeySecretArn", value=api_secret.secret_arn)
        CfnOutput(self, "BucketArn", value=bucket.bucket_arn)
        CfnOutput(
            self, "BlockStoreFunctionArn", value=block_store_function.function_arn
        )
        CfnOutput(
            self, "BlockUploadFunctionArn", value=block_upload_function.function_arn
        )
        CfnOutput(self, "CleanupStateRule", value=cleanup_state_rule.attr_arn)
        CfnOutput(self, "MaxBlocksParamArn", value=max_blocks_param.parameter_arn)
        CfnOutput(self, "QueueArn", value=queue.queue_arn)
        CfnOutput(self, "DlQueueArn", value=dlqueue.queue_arn)
        CfnOutput(self, "UploadStateMachineArn", value=upload_state_machine.attr_arn)
