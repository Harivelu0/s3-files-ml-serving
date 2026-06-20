#!/usr/bin/env python3
"""
setup_infra.py  Provisions all AWS resources for ML artifact serving via S3 Files.

Run once:
    python scripts/setup_infra.py --region us-east-1 --name ml-serving

What it creates:
    1. S3 bucket
    2. IAM role for S3 Files
    3. S3 File System on bucket
    4. Mount Target in VPC subnet
    5. EC2 instance (ECS-optimized)
    6. ECS cluster + task definition + service

Outputs:
    - Mount target DNS (use in mount_s3.sh)
    - EC2 public IP
    - S3 bucket name
"""

import argparse
import json
import time
import boto3
from botocore.exceptions import ClientError

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--region",  default="us-east-1")
parser.add_argument("--name",    default="ml-serving")
parser.add_argument("--image",   default="", help="ECR image URI for ECS task")
args = parser.parse_args()

REGION      = args.region
NAME        = args.name
IMAGE_URI   = args.image

# ── Clients ───────────────────────────────────────────────────────────────────
ec2     = boto3.client("ec2",      region_name=REGION)
s3      = boto3.client("s3",       region_name=REGION)
s3files = boto3.client("s3files",  region_name=REGION)
iam     = boto3.client("iam",      region_name=REGION)
ecs     = boto3.client("ecs",      region_name=REGION)
sts     = boto3.client("sts",      region_name=REGION)

ACCOUNT_ID = sts.get_caller_identity()["Account"]


# ── Helpers ───────────────────────────────────────────────────────────────────
def log(step, msg):
    print(f"[{step}] {msg}")


def wait(seconds, reason):
    print(f"  waiting {seconds}s — {reason}...")
    time.sleep(seconds)



# ── Step 1: S3 Bucket ─────────────────────────────────────────────────────────
def create_bucket():
    log("1/7", f"Creating S3 bucket...")
    bucket_name = f"{NAME}-artifacts-{ACCOUNT_ID}"
    try:
        if REGION == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        s3.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        log("1/7", f"✓ Bucket: {bucket_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "BucketAlreadyOwnedByYou":
            log("1/7", f"✓ Bucket exists: {bucket_name}")
        else:
            raise
    return bucket_name


# ── Step 2: IAM Role for S3 Files ─────────────────────────────────────────────
def create_s3files_role(bucket_name):
    log("2/7", "Creating IAM role for S3 Files...")
    role_name = f"{NAME}-s3files-role"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "s3files.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"],
            "Resource": [
                f"arn:aws:s3:::{bucket_name}",
                f"arn:aws:s3:::{bucket_name}/*",
            ],
        }],
    }
    try:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
        )
        role_arn = role["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        else:
            raise

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="s3-access",
        PolicyDocument=json.dumps(policy),
    )
    log("2/7", f"✓ IAM role: {role_arn}")
    wait(10, "IAM propagation")
    return role_arn



# ── Step 3: S3 File System ────────────────────────────────────────────────────
def create_file_system(bucket_name, role_arn):
    log("3/7", "Creating S3 File System...")
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    fs = s3files.create_file_system(
        Bucket=bucket_arn,
        RoleArn=role_arn,
    )
    fs_id = fs["FileSystemId"]
    log("3/7", f"✓ File System: {fs_id}")
    wait(30, "file system becoming available")
    return fs_id


# ── Step 4: VPC + Mount Target ───────────────────────────────────────────────
def get_default_vpc_subnet():
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    vpc_id = vpcs["Vpcs"][0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    subnet_id = subnets["Subnets"][0]["SubnetId"]
    return vpc_id, subnet_id


def create_mount_target(fs_id, subnet_id, sg_id):
    log("4/7", "Creating Mount Target...")
    mt = s3files.create_mount_target(
        FileSystemId=fs_id,
        SubnetId=subnet_id,
        SecurityGroups=[sg_id],
    )
    mt_dns = mt["DNSName"]
    log("4/7", f"✓ Mount Target DNS: {mt_dns}")
    wait(60, "mount target becoming available")
    return mt_dns


def create_security_groups(vpc_id):
    # NFS SG
    try:
        nfs_sg = ec2.create_security_group(
            GroupName=f"{NAME}-nfs-sg",
            Description="NFS access for S3 Files mount target",
            VpcId=vpc_id,
        )
        nfs_sg_id = nfs_sg["GroupId"]
    except ClientError as e:
        if "InvalidGroup.Duplicate" in str(e):
            nfs_sg_id = ec2.describe_security_groups(
                Filters=[{"Name": "group-name", "Values": [f"{NAME}-nfs-sg"]}]
            )["SecurityGroups"][0]["GroupId"]
        else:
            raise

    # EC2 SG
    try:
        ec2_sg = ec2.create_security_group(
            GroupName=f"{NAME}-ec2-sg",
            Description="EC2 instance for ML serving",
            VpcId=vpc_id,
        )
        ec2_sg_id = ec2_sg["GroupId"]
    except ClientError as e:
        if "InvalidGroup.Duplicate" in str(e):
            ec2_sg_id = ec2.describe_security_groups(
                Filters=[{"Name": "group-name", "Values": [f"{NAME}-ec2-sg"]}]
            )["SecurityGroups"][0]["GroupId"]
        else:
            raise

    # Allow NFS from EC2
    try:
        ec2.authorize_security_group_ingress(
            GroupId=nfs_sg_id,
            IpPermissions=[{
                "IpProtocol": "tcp",
                "FromPort": 2049,
                "ToPort": 2049,
                "UserIdGroupPairs": [{"GroupId": ec2_sg_id}],
            }],
        )
    except ClientError:
        pass  # rule already exists

    # Allow inbound 8000 for FastAPI
    try:
        ec2.authorize_security_group_ingress(
            GroupId=ec2_sg_id,
            IpPermissions=[{
                "IpProtocol": "tcp",
                "FromPort": 8000,
                "ToPort": 8000,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }],
        )
    except ClientError:
        pass

    return nfs_sg_id, ec2_sg_id



# ── Step 5: EC2 Instance ──────────────────────────────────────────────────────
def create_ec2_instance(subnet_id, ec2_sg_id):
    log("5/7", "Launching EC2 instance (ECS-optimized AMI)...")

    # ECS-optimized Amazon Linux 2 AMI (latest)
    ami = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["amzn2-ami-ecs-hvm-*-x86_64-ebs"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )
    latest_ami = sorted(ami["Images"], key=lambda x: x["CreationDate"], reverse=True)[0]
    ami_id = latest_ami["ImageId"]

    # IAM instance profile for ECS
    instance_profile_arn = f"arn:aws:iam::{ACCOUNT_ID}:instance-profile/ecsInstanceRole"

    reservation = ec2.run_instances(
        ImageId=ami_id,
        InstanceType="t3.medium",
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet_id,
        SecurityGroupIds=[ec2_sg_id],
        IamInstanceProfile={"Arn": instance_profile_arn},
        AssociatePublicIpAddress=True,
        UserData=f"""#!/bin/bash
echo ECS_CLUSTER={NAME}-cluster >> /etc/ecs/ecs.config
yum install -y nfs-utils
mkdir -p /mnt/artifacts
""",
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": f"{NAME}-instance"}],
        }],
    )
    instance_id = reservation["Instances"][0]["InstanceId"]
    log("5/7", f"✓ EC2 instance launched: {instance_id}")
    wait(30, "instance initializing")

    instance = ec2.describe_instances(InstanceIds=[instance_id])
    public_ip = instance["Reservations"][0]["Instances"][0].get("PublicIpAddress", "pending")
    log("5/7", f"✓ Public IP: {public_ip}")
    return instance_id, public_ip


# ── Step 6: ECS Cluster + Task + Service ──────────────────────────────────────
def create_ecs_resources():
    log("6/7", "Creating ECS cluster, task definition, service...")

    # Cluster
    ecs.create_cluster(clusterName=f"{NAME}-cluster")

    if not IMAGE_URI:
        log("6/7", "⚠ No --image provided. Task definition skipped. Add ECR image URI and rerun.")
        return

    # Task definition with bind mount
    task = ecs.register_task_definition(
        family=f"{NAME}-task",
        networkMode="bridge",
        requiresCompatibilities=["EC2"],
        containerDefinitions=[{
            "name": "app",
            "image": IMAGE_URI,
            "memory": 1024,
            "portMappings": [{"containerPort": 8000, "hostPort": 8000}],
            "environment": [
                {"name": "ARTIFACTS_DIR", "value": "/mnt/artifacts"},
                {"name": "TOP_K", "value": "10"},
            ],
            "mountPoints": [{
                "sourceVolume": "s3-artifacts",
                "containerPath": "/mnt/artifacts",
                "readOnly": True,
            }],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": f"/ecs/{NAME}",
                    "awslogs-region": REGION,
                    "awslogs-stream-prefix": "app",
                },
            },
        }],
        volumes=[{
            "name": "s3-artifacts",
            "host": {"sourcePath": "/mnt/artifacts"},
        }],
    )
    task_arn = task["taskDefinition"]["taskDefinitionArn"]

    # ECS Service
    ecs.create_service(
        cluster=f"{NAME}-cluster",
        serviceName=f"{NAME}-service",
        taskDefinition=task_arn,
        desiredCount=1,
        launchType="EC2",
    )
    log("6/7", "✓ ECS cluster + task + service created")



# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"  ML Artifact Serving — Infra Setup")
    print(f"  Region: {REGION}  |  Name: {NAME}")
    print(f"{'='*50}\n")

    bucket_name          = create_bucket()
    role_arn             = create_s3files_role(bucket_name)
    fs_id                = create_file_system(bucket_name, role_arn)
    vpc_id, subnet_id    = get_default_vpc_subnet()
    nfs_sg_id, ec2_sg_id = create_security_groups(vpc_id)
    mt_dns               = create_mount_target(fs_id, subnet_id, nfs_sg_id)
    instance_id, pub_ip  = create_ec2_instance(subnet_id, ec2_sg_id)
    create_ecs_resources()

    print(f"\n{'='*50}")
    print(f"  ✓ Setup complete")
    print(f"{'='*50}")
    print(f"  S3 Bucket:        {bucket_name}")
    print(f"  S3 File System:   {fs_id}")
    print(f"  Mount Target DNS: {mt_dns}")
    print(f"  EC2 Instance:     {instance_id}")
    print(f"  EC2 Public IP:    {pub_ip}")
    print(f"\n  Next steps:")
    print(f"  1. SSH into EC2:  ssh -i your-key.pem ec2-user@{pub_ip}")
    print(f"  2. Mount S3:      sudo ./scripts/mount_s3.sh {mt_dns} /mnt/artifacts")
    print(f"  3. Upload artifacts: ./scripts/upload_artifacts.sh {bucket_name}")
    print(f"  4. ECS will pull image and start serving at http://{pub_ip}:8000")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
