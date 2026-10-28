import json
import base64
import pulumi
import pulumi_aws as aws
config = pulumi.Config()
notification_email = config.require("notificationEmail")

project = "hello-world"
region = aws.config.region or "us-east-1"

#vpc
vpc = aws.ec2.Vpc(
    "hello-vpc",
    cidr_block="10.20.0.0/16",
    enable_dns_hostnames=True,
    enable_dns_support=True,
    tags={"Name": f"{project}-vpc"},
)

internet_gateway = aws.ec2.InternetGateway(
    "hello-igw",
    vpc_id=vpc.id,
    tags={"Name": f"{project}-igw"},
)

public_subnets = []

for i, cidr in enumerate(["10.20.1.0/24", "10.20.2.0/24"]):
    subnet = aws.ec2.Subnet(
        f"public-subnet-{i + 1}",
        vpc_id=vpc.id,
        cidr_block=cidr,
        availability_zone=f"{region}{'a' if i == 0 else 'b'}",
        map_public_ip_on_launch=True,
        tags={"Name": f"{project}-public-{i + 1}"},
    )
    public_subnets.append(subnet)

public_route_table = aws.ec2.RouteTable(
    "public-route-table",
    vpc_id=vpc.id,
    routes=[
        aws.ec2.RouteTableRouteArgs(
            cidr_block="0.0.0.0/0",
            gateway_id=internet_gateway.id,
        )
    ],
    tags={"Name": f"{project}-public-rt"},
)

for i, subnet in enumerate(public_subnets):
    aws.ec2.RouteTableAssociation(
        f"public-route-association-{i + 1}",
        subnet_id=subnet.id,
        route_table_id=public_route_table.id,
    )

#security_grps
alb_sg = aws.ec2.SecurityGroup(
    "alb-security-group",
    vpc_id=vpc.id,
    description="Allow public HTTP traffic to the load balancer",
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=80,
            to_port=80,
            cidr_blocks=["0.0.0.0/0"],
        )
    ],
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1",
            from_port=0,
            to_port=0,
            cidr_blocks=["0.0.0.0/0"],
        )
    ],
    tags={"Name": f"{project}-alb-sg"},
)

ec2_sg = aws.ec2.SecurityGroup(
    "ec2-security-group",
    vpc_id=vpc.id,
    description="Allow HTTP only from the load balancer",
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=80,
            to_port=80,
            security_groups=[alb_sg.id],
        )
    ],
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1",
            from_port=0,
            to_port=0,
            cidr_blocks=["0.0.0.0/0"],
        )
    ],
    tags={"Name": f"{project}-ec2-sg"},
)

#alb
alb = aws.lb.LoadBalancer(
    "hello-alb",
    internal=False,
    load_balancer_type="application",
    security_groups=[alb_sg.id],
    subnets=[subnet.id for subnet in public_subnets],
    tags={"Name": f"{project}-alb"},
)

target_group = aws.lb.TargetGroup(
    "hello-target-group",
    port=80,
    protocol="HTTP",
    vpc_id=vpc.id,
    target_type="instance",
    health_check=aws.lb.TargetGroupHealthCheckArgs(
        path="/",
        protocol="HTTP",
        matcher="200",
    ),
    tags={"Name": f"{project}-tg"},
)

listener = aws.lb.Listener(
    "hello-http-listener",
    load_balancer_arn=alb.arn,
    port=80,
    protocol="HTTP",
    default_actions=[
        aws.lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=target_group.arn,
        )
    ],
)

ami = aws.ec2.get_ami(
    most_recent=True,
    owners=["137112412989"],
    filters=[
        aws.ec2.GetAmiFilterArgs(
            name="name",
            values=["al2023-ami-2023.*-x86_64"],
        ),
        aws.ec2.GetAmiFilterArgs(
            name="architecture",
            values=["x86_64"],
        ),
        aws.ec2.GetAmiFilterArgs(
            name="state",
            values=["available"],
        ),
    ],
)

user_data = """#!/bin/bash
dnf install -y httpd
systemctl enable --now httpd
echo "Hello World" > /var/www/html/index.html
"""

launch_template = aws.ec2.LaunchTemplate(
    "hello-launch-template",
    image_id=ami.id,
    instance_type="t2.micro",
    user_data=base64.b64encode(user_data.encode()).decode(),
    vpc_security_group_ids=[ec2_sg.id],
    tag_specifications=[
        aws.ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags={
                "Name": f"{project}-instance",
                "Project": project,
            },
        )
    ],
    tags={"Name": f"{project}-launch-template"},
)

#asg
asg = aws.autoscaling.Group(
    "hello-asg",
    min_size=2,
    max_size=2,
    desired_capacity=2,
    vpc_zone_identifiers=[subnet.id for subnet in public_subnets],
    target_group_arns=[target_group.arn],
    launch_template=aws.autoscaling.GroupLaunchTemplateArgs(
        id=launch_template.id,
        version="$Latest",
    ),
    health_check_type="ELB",
    health_check_grace_period=180,
    tags=[
        aws.autoscaling.GroupTagArgs(
            key="Project",
            value=project,
            propagate_at_launch=True,
        )
    ],
    opts=pulumi.ResourceOptions(depends_on=[listener]),
)

#sns
notification_topic = aws.sns.Topic(
    "cpu-alarm-topic",
    name="hello-world-cpu-alarm-topic",
)

email_subscription = aws.sns.TopicSubscription(
    "cpu-alarm-email-subscription",
    topic=notification_topic.arn,
    protocol="email",
    endpoint=notification_email,
)

# Allow CloudWatch to publish alarm notifications to SNS.

notification_topic_policy = aws.sns.TopicPolicy(
    "cpu-alarm-topic-policy",
    arn=notification_topic.arn,
    policy=notification_topic.arn.apply(
        lambda topic_arn: json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "cloudwatch.amazonaws.com"
                    },
                    "Action": "sns:Publish",
                    "Resource": topic_arn,
                }
            ],
        })
    ),
)

#iam roles for lambda
lambda_role = aws.iam.Role(
    "alarm-lambda-role",
    assume_role_policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "lambda.amazonaws.com"
                },
                "Action": "sts:AssumeRole",
            }
        ],
    }),
)

lambda_basic_policy = aws.iam.RolePolicyAttachment(
    "lambda-basic-execution",
    role=lambda_role.name,
    policy_arn=(
        "arn:aws:iam::aws:policy/service-role/"
        "AWSLambdaBasicExecutionRole"
    ),
)

lambda_permissions = aws.iam.RolePolicy(
    "alarm-lambda-permissions",
    role=lambda_role.id,
    policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:DescribeInstances",
                    "cloudwatch:PutMetricAlarm",
                    "cloudwatch:DeleteAlarms",
                ],
                "Resource": "*",
            }
        ],
    }),
)

#lambda
alarm_lambda_code = r'''
import boto3
import os

ec2 = boto3.client("ec2")
cloudwatch = boto3.client("cloudwatch")

PROJECT = "hello-world"

def handler(event, context):
    detail = event.get("detail", {})
    instance_id = detail.get("instance-id")
    state = detail.get("state")

    if not instance_id:
        return {"message": "Missing instance ID"}

    alarm_name = f"{PROJECT}-{instance_id}-cpu"

    if state == "terminated":
        cloudwatch.delete_alarms(
            AlarmNames=[alarm_name]
        )
        return {
            "message": "Alarm cleanup requested",
            "instanceId": instance_id,
        }

    if state != "running":
        return {"message": "Ignoring unrelated state"}

    response = ec2.describe_instances(
        InstanceIds=[instance_id]
    )

    instances = [
        instance
        for reservation in response["Reservations"]
        for instance in reservation["Instances"]
    ]

    if not instances:
        return {"message": "Instance not found"}

    tags = {
        tag["Key"]: tag["Value"]
        for tag in instances[0].get("Tags", [])
    }

    if tags.get("Project") != PROJECT:
        return {"message": "Not part of this project"}

    cloudwatch.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=(
            f"CPU utilization exceeds 15% on {instance_id}"
        ),
        ActionsEnabled=True,
        AlarmActions=[os.environ["SNS_TOPIC_ARN"]],
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[
            {
                "Name": "InstanceId",
                "Value": instance_id,
            }
        ],
        Statistic="Average",
        Period=300,
        EvaluationPeriods=1,
        Threshold=15.0,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
    )

    return {
        "message": "CPU alarm created",
        "instanceId": instance_id,
        "alarmName": alarm_name,
    }
'''

alarm_lambda = aws.lambda_.Function(
    "create-instance-cpu-alarm",
    runtime="python3.12",
    handler="alarm_lambda.handler",
    role=lambda_role.arn,
    code=pulumi.AssetArchive({
        "alarm_lambda.py": pulumi.StringAsset(alarm_lambda_code)
    }),
    timeout=30,
    environment=aws.lambda_.FunctionEnvironmentArgs(
        variables={
            "SNS_TOPIC_ARN": notification_topic.arn,
        }
    ),
    opts=pulumi.ResourceOptions(
        depends_on=[
            lambda_basic_policy,
            lambda_permissions,
        ]
    ),
)

# Handles EC2 running/terminated events.
# Lambda creates/deletes per-instance CPU alarms.
# CloudWatch sends alarm notifications directly to SNS.

ec2_state_rule = aws.cloudwatch.EventRule(
    "ec2-instance-state-rule",
    description=(
        "Create and clean up per-instance CPU alarms"
    ),
    event_pattern=json.dumps({
        "source": ["aws.ec2"],
        "detail-type": [
            "EC2 Instance State-change Notification"
        ],
        "detail": {
            "state": ["running", "terminated"]
        },
    }),
)

lambda_permission = aws.lambda_.Permission(
    "allow-ec2-eventbridge",
    action="lambda:InvokeFunction",
    function=alarm_lambda.name,
    principal="events.amazonaws.com",
    source_arn=ec2_state_rule.arn,
)

ec2_state_target = aws.cloudwatch.EventTarget(
    "ec2-state-event-target",
    rule=ec2_state_rule.name,
    arn=alarm_lambda.arn,
    #here i cant delete the existing tags so used ingore_changes for tags
    opts=pulumi.ResourceOptions(
        depends_on=[
            lambda_basic_policy,
            lambda_permissions,
        ],
        ignore_changes=["tags"],
    ),
)

#output
pulumi.export(
    "application_url",
    pulumi.Output.concat("http://", alb.dns_name),
)

pulumi.export("load_balancer_dns_name", alb.dns_name)
pulumi.export("auto_scaling_group_name", asg.name)
pulumi.export("vpc_id", vpc.id)
pulumi.export("notification_topic_arn", notification_topic.arn)
