import json

from aws_cdk import (
    Duration,
    Stack,
    RemovalPolicy,
    aws_sqs as sqs,
    aws_s3 as s3,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_iam as iam,
    aws_events as events,
    aws_appsync as appsync,
    aws_logs as logs,
    aws_dynamodb as dynamodb,
)
import aws_cdk as cdk
from aws_cdk.aws_lambda import Tracing
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from aws_cdk.aws_stepfunctions import DefinitionBody, StateMachineType
from constructs import Construct

INPUT_PREFIX = "incoming/"


class VirtualTryonCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # define prefixes

        # OUTPUT_PREFIX = "results/"
        COMMON_LAMBDA_ENV_VARS = {
            "POWERTOOLS_SERVICE_NAME": "virtual-try-on",
            "POWERTOOLS_LOGGER_LOG_LEVEL": "WARN",
            "POWERTOOLS_LOGGER_SAMPLE_RATE": "0.01",
            "POWERTOOLS_LOGGER_LOG_EVENT": "true",
            "POWERTOOLS_METRICS_NAMESPACE": "VirtualTryOnApp",
        }
        nova_creative_db = dynamodb.Table(
            self,
            "NovaCreativeApplications",
            table_name="nova-creative-db",
            partition_key=dynamodb.Attribute(
                name="id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,  # Use RETAIN for production
        )

        # Create S3 bucket for image uploads
        upload_bucket = s3.Bucket(
            self,
            "VirtualTryonUploadBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Create AppSync API with API Key auth
        api = appsync.GraphqlApi(
            self,
            "VirtualTryonAPI",
            name="virtual-tryon-api",
            definition=appsync.Definition.from_schema(
                appsync.SchemaFile.from_asset("graphql/schema.graphql"),
            ),
            authorization_config=appsync.AuthorizationConfig(
                default_authorization=appsync.AuthorizationMode(
                    authorization_type=appsync.AuthorizationType.API_KEY,
                    api_key_config=appsync.ApiKeyConfig(
                        expires=cdk.Expiration.after(Duration.days(365))
                    ),
                ),
                additional_authorization_modes=[
                    appsync.AuthorizationMode(
                        authorization_type=appsync.AuthorizationType.IAM  # IAM Auth
                    )
                ],
            ),
            log_config=appsync.LogConfig(field_log_level=appsync.FieldLogLevel.ALL),
            xray_enabled=True,
        )

        # Create EventBridge bus
        event_bus = events.EventBus(
            self, "VirtualTryonEventBus", event_bus_name="virtual-tryon-events"
        )

        # Create Lambda function for Step Functions workflow
        workflow_lambda = PythonFunction(
            self,
            "WorkflowFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            entry="./lambda/workflow",
            index="handler.py",
            handler="handler",
            environment={
                "EVENT_BUS_NAME": event_bus.event_bus_name,
                "BUCKET_NAME": upload_bucket.bucket_name,
            },
            timeout=Duration.minutes(15),
        )

        # Create Step Functions workflow
        workflow_task = tasks.LambdaInvoke(
            self,
            "InvokeWorkflowLambda",
            lambda_function=workflow_lambda,
        )
        invoke = workflow_task.add_retry(
            errors=[
                "Bedrock.ServiceUnavailableException",
                "Bedrock.ThrottlingException",
                "States.TaskFailed",
            ],
            interval=Duration.seconds(2),
            backoff_rate=2.5,
            max_attempts=6,
        )

        # 3) Single catch-all (States.ALL) — only once, and effectively last
        failed = sfn.Fail(self, "Failed")
        invoke = invoke.add_catch(
            failed,
            result_path="$.error",  # no errors=... => States.ALL
        )
        # 4) Success
        done = sfn.Succeed(self, "Done")

        # 5) Chain
        chain = invoke.next(done)

        state_machine = sfn.StateMachine(
            self,
            "VirtualTryonWorkflow",
            definition_body=DefinitionBody.from_chainable(chain),
            state_machine_type=StateMachineType.EXPRESS,
        )

        # Create Lambda function that will be triggered by S3
        s3_trigger_lambda = PythonFunction(
            self,
            "S3TriggerFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            entry="./lambda/s3_trigger",
            index="handler.py",
            handler="handler",
            tracing=Tracing.ACTIVE,
            environment={
                **COMMON_LAMBDA_ENV_VARS,
                "STATE_MACHINE_ARN": state_machine.state_machine_arn,  # Will be set after Step Function creation
                "BUCKET_NAME": upload_bucket.bucket_name,
            },
            timeout=Duration.seconds(30),
        )

        # Create Lambda function that will be triggered by S3
        text_2_image_lambda = PythonFunction(
            self,
            "Text2ImageLambdaFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            entry="./lambda/text_2_image",
            index="handler.py",
            handler="handler",
            tracing=Tracing.ACTIVE,
            environment={
                **COMMON_LAMBDA_ENV_VARS,
                "BUCKET_NAME": upload_bucket.bucket_name,
            },
            timeout=Duration.seconds(30),
        )

        # Create Lambda function that will be triggered by S3
        get_imageList_lambda = PythonFunction(
            self,
            "GetImageListLambdaFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            entry="./lambda/retrieve_images",
            index="handler.py",
            handler="handler",
            tracing=Tracing.ACTIVE,
            environment={
                **COMMON_LAMBDA_ENV_VARS,
                "TABLE_NAME": nova_creative_db.table_name,
            },
            timeout=Duration.seconds(30),
        )
        log_group = logs.LogGroup(
            self,
            "ExpressSmLogs",
            retention=logs.RetentionDays.ONE_WEEK,  # adjust as needed
            removal_policy=RemovalPolicy.DESTROY,  # keep RETAIN for prod
        )
        # Load the ASL definition for text 2 image from the JSON file
        with open("./state_machine/text_2_image.asl.json", "r") as file:
            state_machine_definition = json.load(file)
        # Load the ASL definition from the JSON file
        with open("./state_machine/text_2_video.asl.json", "r") as file:
            text_2_video_state_machine_definition = json.load(file)
        text_2_video_state_machine = sfn.StateMachine(
            self,
            "Text2VideoStateMachineWorkflow",
            definition_body=sfn.DefinitionBody.from_string(
                json.dumps(text_2_video_state_machine_definition)
            ),
            definition_substitutions={"DYNAMO_DB_TABLE": nova_creative_db.table_name},
            logs=sfn.LogOptions(
                destination=log_group,
                level=sfn.LogLevel.ALL,  # OFF | ERROR | ALL
                include_execution_data=True,  # include input/output in log events
            ),
            tracing_enabled=True,
            state_machine_type=StateMachineType.EXPRESS,
        )

        text_2_image_state_machine = sfn.StateMachine(
            self,
            "Text2ImageStateMachineWorkflow",
            definition_body=sfn.DefinitionBody.from_string(
                json.dumps(state_machine_definition)
            ),
            definition_substitutions={
                "DYNAMO_DB_TABLE": nova_creative_db.table_name,
                "INVOKE_LAMBDA_FUNCTION_ARN": text_2_image_lambda.function_arn,
            },
            logs=sfn.LogOptions(
                destination=log_group,
                level=sfn.LogLevel.ALL,  # OFF | ERROR | ALL
                include_execution_data=True,  # include input/output in log events
            ),
            tracing_enabled=True,
            state_machine_type=StateMachineType.EXPRESS,
        )

        # Create Lambda function that will invoke the text 2 image state machine
        invoke_text_2_video_state_machine = PythonFunction(
            self,
            "InvokeText2VideoStateMachineFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            entry="./lambda/text_2_video",
            index="invoke_state_machine.py",
            tracing=Tracing.ACTIVE,
            environment={
                **COMMON_LAMBDA_ENV_VARS,
                "TEXT_2_VIDEO_STATE_MACHINE_ARN": text_2_video_state_machine.state_machine_arn,
                "BUCKET_NAME": upload_bucket.bucket_name,
            },
            timeout=Duration.seconds(30),
        )

        # Create Lambda function that will invoke the text 2 image state machine
        invoke_state_machine_lambda = PythonFunction(
            self,
            "InvokeText2ImageStateMachineFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            entry="./lambda/text_2_image",
            index="invoke_state_machine.py",
            tracing=Tracing.ACTIVE,
            environment={
                **COMMON_LAMBDA_ENV_VARS,
                "TEXT_2_IMAGE_STATE_MACHINE_ARN": text_2_image_state_machine.state_machine_arn,
                "BUCKET_NAME": upload_bucket.bucket_name,
            },
            timeout=Duration.seconds(30),
        )
        text_2_image_lambda.grant_invoke(text_2_image_state_machine)
        text_2_video_state_machine.grant_start_execution(
            invoke_text_2_video_state_machine
        )
        nova_creative_db.grant_write_data(text_2_video_state_machine)
        upload_bucket.grant_read_write(text_2_video_state_machine)
        nova_creative_db.grant_read_data(get_imageList_lambda)

        text_2_video_state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:StartAsyncInvoke",
                    "bedrock:GetAsyncInvoke",
                ],
                resources=["*"],
            )
        )

        text_2_image_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeAgent",
                    "bedrock:RetrieveAndGenerate",
                    "bedrock:Retrieve",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=["*"],
                # Grant access to all Bedrock models
            )
        )

        workflow_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeAgent",
                    "bedrock:RetrieveAndGenerate",
                    "bedrock:Retrieve",
                    "bedrock:ListAgents",
                    "bedrock:GetAgent",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=["*"],
                # Grant access to all Bedrock models
            )
        )
        text_2_image_state_machine.grant_start_execution(invoke_state_machine_lambda)
        upload_bucket.grant_write(text_2_image_lambda)
        nova_creative_db.grant_write_data(text_2_image_state_machine)

        # Grant S3 bucket permissions to the s3 Lambda function
        upload_bucket.grant_read(s3_trigger_lambda)

        # Add S3 event source to Lambda
        s3_trigger_lambda.add_event_source(
            lambda_event_sources.S3EventSource(
                upload_bucket,
                events=[s3.EventType.OBJECT_CREATED],
                filters=[s3.NotificationKeyFilter(prefix=INPUT_PREFIX)],
            )
        )

        # Add Lambda as a DataSource for AppSync
        text_2_image_lambda_ds = api.add_lambda_data_source(
            "Text2ImageLambdaDataSource", invoke_state_machine_lambda
        )
        text_2_video_lambda_ds = api.add_lambda_data_source(
            "Text2VideoLambdaDataSource", invoke_text_2_video_state_machine
        )
        retrieve_images_lambda_ds = api.add_lambda_data_source(
            "RetrieveImagesLambdaDataSource", get_imageList_lambda
        )

        none_ds = api.add_none_data_source("NoneDatasource")

        # none_ds is a NoneDataSource from: none_ds = api.add_none_data_source("NoneDataSource")

        none_ds.create_resolver(
            "GenerateImageResponseResolver",
            type_name="Mutation",
            field_name="generateImageResponse",
            # For a NONE data source, the request template isn't used to call anything.
            # Keep it minimal and do the real return in the response template.
            request_mapping_template=appsync.MappingTemplate.from_string(
                """
                {
                  "version": "2017-02-28",
                  "payload": {}
                }
                """
            ),
            response_mapping_template=appsync.MappingTemplate.from_string(
                "$util.toJson($context.arguments.input)"
            ),
        )

        # Define Resolvers
        text_2_image_lambda_ds.create_resolver(
            "Text2ImageLambdaResolver",
            type_name="Mutation",
            field_name="text2Image",
            request_mapping_template=appsync.MappingTemplate.lambda_request(),
            response_mapping_template=appsync.MappingTemplate.lambda_result(),
        )

        # Define Resolvers
        text_2_video_lambda_ds.create_resolver(
            "Text2VideoLambdaResolver",
            type_name="Mutation",
            field_name="text2Video",
            request_mapping_template=appsync.MappingTemplate.lambda_request(),
            response_mapping_template=appsync.MappingTemplate.lambda_result(),
        )

        # Define Resolvers
        retrieve_images_lambda_ds.create_resolver(
            "RetrieveImagesLambdaResolver",
            type_name="Query",
            field_name="getImageList",
            request_mapping_template=appsync.MappingTemplate.lambda_request(),
            response_mapping_template=appsync.MappingTemplate.lambda_result(),
        )

        # Grant permissions to the workflow Lambda
        upload_bucket.grant_read_write(workflow_lambda)
        event_bus.grant_put_events_to(workflow_lambda)

        # Update the S3 trigger Lambda with the State Machine ARN
        s3_trigger_lambda.add_environment(
            "STATE_MACHINE_ARN", state_machine.state_machine_arn
        )
        target_dlq = sqs.Queue(
            self,
            "VirtualTryOnAppTargetDLQ",
            queue_name="virtual_try-on-appsync-dlq",
            retention_period=Duration.days(14),
        )
        # Create a CloudWatch Log Group for logging events
        log_group = logs.LogGroup(
            self,
            "VirtualTryOnEventLogs",
            removal_policy=RemovalPolicy.DESTROY,
            log_group_name="/aws/events/VirtualTryOnEventLogs",
            retention=logs.RetentionDays.ONE_WEEK,  # Adjust retention as needed
        )

        # Grant permission to invoke Step Functions
        state_machine.grant_start_execution(s3_trigger_lambda)

        # Create an IAM Role for invoking the AppSync API
        appsync_invocation_role = iam.Role(
            self,
            "AppSyncInvocationRole",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
        )
        api_arn: appsync.CfnGraphQLApi = api.node.default_child

        # Grant the AppSync Invocation Role permissions to invoke the AppSync API
        appsync_invocation_role.add_to_policy(
            iam.PolicyStatement(
                actions=["appsync:GraphQL"],
                resources=[
                    f"{api.arn}/types/Mutation/*"
                ],  # Replace with your AppSync API ARN
            )
        )

        events.CfnRule(
            self,
            "CatchAllEventsVirtualTryOnEventRule",
            event_bus_name=event_bus.event_bus_name,
            event_pattern=events.EventPattern(
                source=events.Match.prefix(""),  # requires CDK with Match helpers
            ),
            targets=[
                events.CfnRule.TargetProperty(
                    id="VirtualTryOnAppRuleCloudWatchLogs",
                    arn=log_group.log_group_arn,
                ),
            ],
        )

        # Create an EventBridge Rule using CfnRule
        events.CfnRule(
            self,
            "VirtualTryOnEventRule",
            event_bus_name=event_bus.event_bus_name,
            event_pattern={
                "source": ["virtual-tryon-service"],
                "detail-type": ["ProcessingResult"],
            },
            targets=[
                # AppSync target
                events.CfnRule.TargetProperty(
                    id="AppsyncTarget",
                    arn=api_arn.attr_graph_ql_endpoint_arn,  # Replace with your AppSync API ARN
                    role_arn=appsync_invocation_role.role_arn,
                    dead_letter_config=events.CfnRule.DeadLetterConfigProperty(
                        arn=target_dlq.queue_arn,
                    ),
                    input_transformer=events.CfnRule.InputTransformerProperty(
                        input_paths_map={
                            "input": "$.detail",
                        },
                        input_template='{"input": <input>}',
                    ),
                    app_sync_parameters=events.CfnRule.AppSyncParametersProperty(
                        graph_ql_operation="mutation GenerateImageResponse($input: ProcessingResultInput) { "
                        "generateImageResponse(input: $input) { "
                        "id imageKey status resultUrl presigned_url processingType"
                        "} }",
                    ),
                ),
            ],
        )
        cdk.CfnOutput(self, "GraphQLEndpoint", value=api.graphql_url)
        cdk.CfnOutput(self, "GraphQLApiKey", value=api.api_key)
        cdk.CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
