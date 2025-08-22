# Virtual Try-On CDK Project

This project implements a serverless architecture for a virtual try-on
application using AWS CDK. The architecture includes:

1. S3 bucket for image uploads
2. Lambda function triggered by S3 events
3. Step Functions workflow for orchestration
4. Lambda function within the Step Functions workflow
5. EventBridge for event-driven communication
6. AppSync API with API Key authentication
7. CloudWatch for monitoring and logging

## Architecture Overview

```
┌─────────┐     ┌──────────┐     ┌───────────────┐     ┌──────────────┐
│  S3     │────▶│  Lambda  │────▶│ Step Functions│────▶│ Lambda       │
│ Bucket  │     │ Trigger  │     │   Workflow    │     │ (Processing) │
└─────────┘     └──────────┘     └───────────────┘     └──────┬───────┘
                                                              │
                                                              ▼
┌─────────┐     ┌──────────┐                           ┌──────────────┐
│ AppSync │◀────│EventBridge│◀──────────────────────────│ EventBridge  │
│   API   │     │  Target   │                           │    Events    │
└─────────┘     └──────────┘                           └──────────────┘
                      │                                       │
                      │                                       │
                      ▼                                       ▼
                ┌──────────┐                           ┌──────────────┐
                │CloudWatch│                           │ CloudWatch   │
                │  Logs    │                           │    Logs      │
                └──────────┘                           └──────────────┘
```

## Project Structure

- `virtual_tryon_cdk/` - CDK stack definition
- `lambda/s3_trigger/` - Lambda function triggered by S3 events
- `lambda/workflow/` - Lambda function invoked by Step Functions
- `graphql/` - AppSync GraphQL schema

## Deployment

1. Install dependencies:

```
pip install -r requirements.txt
```

2. Deploy the stack:

```
cdk deploy
```

## Testing the Application

1. Upload an image to the S3 bucket
2. The S3 trigger Lambda will start the Step Functions workflow
3. The workflow Lambda will process the image and send events to EventBridge
4. EventBridge will forward events to AppSync and CloudWatch
5. Query the AppSync API to get the processing results

## Useful Commands

- `cdk ls` list all stacks in the app
- `cdk synth` emits the synthesized CloudFormation template
- `cdk deploy` deploy this stack to your default AWS account/region
- `cdk diff` compare deployed stack with current state
- `cdk docs` open CDK documentation
