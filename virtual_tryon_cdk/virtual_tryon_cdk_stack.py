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
)
import aws_cdk as cdk
from aws_cdk.aws_lambda import Tracing
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from aws_cdk.aws_stepfunctions import DefinitionBody
from constructs import Construct


class VirtualTryonCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # define prefixes
        INPUT_PREFIX = "incoming/"
        # OUTPUT_PREFIX = "results/"
        COMMON_LAMBDA_ENV_VARS = {
            "POWERTOOLS_SERVICE_NAME": "virtual-try-on",
            "POWERTOOLS_LOGGER_LOG_LEVEL": "WARN",
            "POWERTOOLS_LOGGER_SAMPLE_RATE": "0.01",
            "POWERTOOLS_LOGGER_LOG_EVENT": "true",
            "POWERTOOLS_METRICS_NAMESPACE": "VirtualTryOnApp",
        }

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
            timeout=Duration.days(365),
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

        # Grant S3 bucket permissions to the Lambda function
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
        lambda_ds = api.add_lambda_data_source("LambdaDataSource", s3_trigger_lambda)

        # Define None DataSource
        none_data_source = appsync.CfnDataSource(
            self,
            "VirtualTryOnNoneDatasource",
            api_id=api.api_id,
            name="NoneDataSource",
            type="NONE",
            description="None",
        )

        # Define Mutation Resolver
        appsync.CfnResolver(
            self,
            "GenerateImageResponseMutationResolver",
            api_id=api.api_id,
            type_name="Mutation",
            field_name="generateImageResponse",
            data_source_name=none_data_source.name,
            request_mapping_template="""
                       {
                         "version": "2017-02-28",
                         "payload": {

                         }
                       }
                   """,
            response_mapping_template="$util.toJson($context.arguments.input)",
        )

        lambda_ds.create_resolver(
            id="StartStateMachineWorkflowResolver",
            type_name="Mutation",
            field_name="startProcessing",
            request_mapping_template=appsync.MappingTemplate.lambda_request(),
            response_mapping_template=appsync.MappingTemplate.lambda_result(),
        )

        # Define

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
                        "id imageKey status resultUrl processingType"
                        "} }",
                    ),
                ),
            ],
        )
        cdk.CfnOutput(self, "GraphQLEndpoint", value=api.graphql_url)
        cdk.CfnOutput(self, "GraphQLApiKey", value=api.api_key)
        cdk.CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
