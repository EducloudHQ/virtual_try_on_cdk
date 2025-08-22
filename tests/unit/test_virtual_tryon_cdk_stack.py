import aws_cdk as core
from aws_cdk import assertions

from virtual_tryon_cdk.virtual_tryon_cdk_stack import VirtualTryonCdkStack


# example tests. To run these tests, uncomment this file along with the example
# resource in virtual_tryon_cdk/virtual_tryon_cdk_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = VirtualTryonCdkStack(app, "virtual-tryon-cdk")
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties("AWS::SQS::Queue", {"VisibilityTimeout": 300})
