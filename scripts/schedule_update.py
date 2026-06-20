#!/usr/bin/env python3
"""
schedule_update.py  Sets up weekly EventBridge rule → ECS run-task.

Run once after cdk deploy / setup_infra.py:
    python scripts/schedule_update.py \
        --cluster  ml-serving-cluster \
        --task-def ml-serving-task \
        --region   us-east-1

What it creates:
    - EventBridge rule: every Sunday 00:00 UTC
    - ECS run-task target: runs update_pipeline.sh container
    - IAM role for EventBridge to invoke ECS
"""

import argparse
import json
import boto3
from botocore.exceptions import ClientError

parser = argparse.ArgumentParser()
parser.add_argument("--cluster",    required=True, help="ECS cluster name")
parser.add_argument("--task-def",   required=True, help="ECS task definition family")
parser.add_argument("--subnet",     required=True, help="Subnet ID for task")
parser.add_argument("--region",     default="us-east-1")
parser.add_argument("--schedule",   default="cron(0 0 ? * SUN *)", help="EventBridge cron")
args = parser.parse_args()

REGION      = args.region
RULE_NAME   = "aws-docs-weekly-update"
ROLE_NAME   = "eventbridge-ecs-invoke-role"

iam    = boto3.client("iam",             region_name=REGION)
events = boto3.client("events",          region_name=REGION)
ecs    = boto3.client("ecs",             region_name=REGION)
sts    = boto3.client("sts",             region_name=REGION)

ACCOUNT_ID = sts.get_caller_identity()["Account"]


# ── Step 1: IAM role for EventBridge → ECS ───────────────────────────────────
def create_eventbridge_role() -> str:
    print("[1/3] Creating IAM role for EventBridge...")
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "events.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["ecs:RunTask"],
            "Resource": "*",
        }, {
            "Effect": "Allow",
            "Action": ["iam:PassRole"],
            "Resource": "*",
        }],
    }
    try:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
        )
        role_arn = role["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
            print(f"  → Role exists: {role_arn}")
        else:
            raise

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="ecs-run-task",
        PolicyDocument=json.dumps(policy),
    )
    print(f"  ✓ Role: {role_arn}")
    return role_arn


# ── Step 2: EventBridge rule ──────────────────────────────────────────────────
def create_rule() -> str:
    print(f"[2/3] Creating EventBridge rule ({args.schedule})...")
    resp = events.put_rule(
        Name=RULE_NAME,
        ScheduleExpression=args.schedule,
        State="ENABLED",
        Description="Weekly AWS docs update → ECS run-task",
    )
    rule_arn = resp["RuleArn"]
    print(f"  ✓ Rule ARN: {rule_arn}")
    return rule_arn


# ── Step 3: ECS run-task target ───────────────────────────────────────────────
def create_target(rule_arn: str, role_arn: str):
    print("[3/3] Adding ECS run-task as EventBridge target...")

    # Get latest task def ARN
    task_def = ecs.describe_task_definition(taskDefinition=args.task_def)
    task_arn = task_def["taskDefinition"]["taskDefinitionArn"]

    events.put_targets(
        Rule=RULE_NAME,
        Targets=[{
            "Id":      "weekly-updater",
            "Arn":     f"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:cluster/{args.cluster}",
            "RoleArn": role_arn,
            "EcsParameters": {
                "TaskDefinitionArn": task_arn,
                "TaskCount":         1,
                "LaunchType":        "EC2",
                "NetworkConfiguration": {
                    "awsvpcConfiguration": {
                        "Subnets":        [args.subnet],
                        "AssignPublicIp": "DISABLED",
                    }
                },
                "Overrides": {
                    "containerOverrides": [{
                        "name":    "app",
                        "command": ["bash", "scripts/update_pipeline.sh"],
                    }]
                },
            },
        }],
    )
    print(f"  ✓ Target set → runs every Sunday 00:00 UTC")


def main():
    print("\n========================================")
    print("  Weekly Update Scheduler Setup")
    print(f"  Cluster : {args.cluster}")
    print(f"  Schedule: {args.schedule}")
    print("========================================\n")

    role_arn = create_eventbridge_role()
    rule_arn = create_rule()
    create_target(rule_arn, role_arn)

    print("\n========================================")
    print("  ✓ Weekly update scheduled")
    print("  Runs: every Sunday 00:00 UTC")
    print("  Flow: EventBridge → ECS run-task")
    print("        → crawl --update")
    print("        → rebuild indexes")
    print("        → atomic swap on S3 mount")
    print("        → serving container live instantly")
    print("========================================\n")


if __name__ == "__main__":
    main()
