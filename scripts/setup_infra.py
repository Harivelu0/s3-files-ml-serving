#!/usr/bin/env python3
"""
setup_infra.py  Provisions AWS resources for ML artifact serving via S3 Files.

Run once:
    python scripts/setup_infra.py --region us-east-1 --name ml-serving

What it creates:
    1. S3 bucket (versioning enabled)
    2. IAM role for S3 Files
    3. S3 File System on bucket
    4. Mount Target in VPC subnet
    5. EC2 instance (Ubuntu 22.04)

Outputs:
    - File system ID (use in mount_s3.sh)
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
parser.add_argument("--region", default="us-east-1")
parser.add_argument("--name",   default="ml-serving")
parser.add_argument("--key",    default="ml-serving-key", help="EC2 key pair name")
args = parser.parse_args()

REGION = args.region
NAME   = args.name

# ── Clients ───────────────────────────────────────────────────────────────────
ec2     = boto3.client("ec2",     region_name=REGION)
s3      = boto3.client("s3",      region_name=REGION)
s3files = boto3.client("s3files", region_name=REGION)
iam     = boto3.client("iam",     region_name=REGION)
sts     = boto3.client("sts",     region_name=REGION)

ACCOUNT_ID = sts.get_caller_identity()["Account"]


def log(step, msg):
    print(f"[{step}] {msg}")

def wait(seconds, reason):
    print(f"  waiting {seconds}s — {reason}...")
    time.sleep(seconds)


# ── Step 1: S3 Bucket ─────────────────────────────────────────────────────────
def create_bucket():
    log("1/5", "Creating S3 bucket...")
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
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
            },
        )
        s3.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Enabled"},
        )
        log("1/5", f"✓ Bucket: {bucket_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "BucketAlreadyOwnedByYou":
            log("1/5", f"✓ Bucket exists: {bucket_name}")
        else:
            raise
    return bucket_name


# ── Step 2: IAM Role for S3 Files ─────────────────────────────────────────────
def create_s3files_role(bucket_name):
    log("2/5", "Creating IAM role for S3 Files...")
    role_name = f"{NAME}-s3files-role"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowS3FilesAssumeRole",
            "Effect": "Allow",
            "Principal": {"Service": "elasticfilesystem.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                "ArnLike": {"aws:SourceArn": f"arn:aws:s3files:{REGION}:{ACCOUNT_ID}:file-system/*"},
            },
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject",
                    "s3:DeleteObject", "s3:ListBucket",
                    "s3:GetBucketNotification", "s3:PutBucketNotification",
                    "s3:GetBucketVersioning",
                ],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}",
                    f"arn:aws:s3:::{bucket_name}/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "events:PutRule", "events:DeleteRule", "events:DescribeRule",
                    "events:PutTargets", "events:RemoveTargets", "events:ListRules",
                ],
                "Resource": "*",
            },
        ],
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
    log("2/5", f"✓ IAM role: {role_arn}")
    wait(10, "IAM propagation")
    return role_arn


# ── Step 3: S3 File System ────────────────────────────────────────────────────
def create_file_system(bucket_name, role_arn):
    log("3/5", "Creating S3 File System...")
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    try:
        fs = s3files.create_file_system(
            bucket=bucket_arn,
            roleArn=role_arn,
            acceptBucketWarning=True,
        )
        fs_id = fs.get("fileSystemId")
    except Exception as e:
        if "already exists" in str(e).lower() or "Conflict" in str(e):
            fs_list = s3files.list_file_systems().get("fileSystems", [{}])
            fs_id   = fs_list[0].get("fileSystemId")
            log("3/5", f"✓ File System exists: {fs_id}")
        else:
            raise
    log("3/5", f"✓ File System: {fs_id}")
    print("  waiting for file system to become available...")
    for _ in range(24):
        status = s3files.get_file_system(fileSystemId=fs_id).get("status", "unknown")
        print(f"  status: {status}")
        if status == "available":
            break
        time.sleep(10)
    return fs_id


# ── Step 4: VPC, Security Groups, Mount Target ────────────────────────────────
def get_default_vpc_subnet():
    vpc_id    = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    subnet_id = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"][0]["SubnetId"]
    return vpc_id, subnet_id


def create_security_groups(vpc_id):
    def get_or_create(name, desc):
        try:
            sg = ec2.create_security_group(GroupName=name, Description=desc, VpcId=vpc_id)
            return sg["GroupId"]
        except ClientError as e:
            if "InvalidGroup.Duplicate" in str(e):
                return ec2.describe_security_groups(
                    Filters=[{"Name": "group-name", "Values": [name]}]
                )["SecurityGroups"][0]["GroupId"]
            raise

    nfs_sg_id = get_or_create(f"{NAME}-nfs-sg", "NFS access for S3 Files mount target")
    ec2_sg_id = get_or_create(f"{NAME}-ec2-sg", "EC2 instance for ML serving")

    for port, sg_id, src in [
        (2049, nfs_sg_id, {"UserIdGroupPairs": [{"GroupId": ec2_sg_id}]}),
        (8000, ec2_sg_id, {"IpRanges": [{"CidrIp": "0.0.0.0/0"}]}),
        (22,   ec2_sg_id, {"IpRanges": [{"CidrIp": "0.0.0.0/0"}]}),
    ]:
        try:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{"IpProtocol": "tcp", "FromPort": port, "ToPort": port, **src}],
            )
        except ClientError:
            pass
    return nfs_sg_id, ec2_sg_id


def create_mount_target(fs_id, subnet_id, sg_id):
    log("4/5", "Creating Mount Target...")
    try:
        s3files.create_mount_target(
            fileSystemId=fs_id,
            subnetId=subnet_id,
            securityGroups=[sg_id],
        )
    except Exception as e:
        if "already exists" in str(e).lower() or "Conflict" in str(e):
            log("4/5", "✓ Mount Target exists")
        else:
            raise
    mt_dns = f"{fs_id}.s3-files.{REGION}.amazonaws.com"
    log("4/5", f"✓ Mount Target DNS: {mt_dns}")
    wait(30, "mount target becoming available")
    return mt_dns


# ── Step 5: EC2 Instance ──────────────────────────────────────────────────────
def create_ec2_role():
    role_name = profile_name = "ml-serving-ec2-role"
    try:
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole"}],
            }),
        )
        for policy in [
            "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
            "arn:aws:iam::aws:policy/AmazonS3FilesClientFullAccess",
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
        ]:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=policy)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
    try:
        iam.create_instance_profile(InstanceProfileName=profile_name)
        iam.add_role_to_instance_profile(InstanceProfileName=profile_name, RoleName=role_name)
        wait(10, "instance profile propagation")
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
    return profile_name


def create_ec2_instance(subnet_id, ec2_sg_id):
    log("5/5", "Checking for existing EC2 instance...")
    existing = ec2.describe_instances(Filters=[
        {"Name": "tag:Name",             "Values": [f"{NAME}-instance"]},
        {"Name": "instance-state-name",  "Values": ["running", "pending"]},
    ])
    if existing["Reservations"]:
        inst       = existing["Reservations"][0]["Instances"][0]
        instance_id = inst["InstanceId"]
        pub_ip      = inst.get("PublicIpAddress", "pending")
        log("5/5", f"✓ Reusing existing instance: {instance_id} ({pub_ip})")
        return instance_id, pub_ip

    log("5/5", "Launching EC2 instance (Ubuntu 22.04)...")
    profile_name = create_ec2_role()

    ami = ec2.describe_images(
        Owners=["099720109477"],
        Filters=[
            {"Name": "name",  "Values": ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )
    ami_id = sorted(ami["Images"], key=lambda x: x["CreationDate"], reverse=True)[0]["ImageId"]

    reservation = ec2.run_instances(
        ImageId=ami_id,
        InstanceType="t3.medium",
        MinCount=1, MaxCount=1,
        IamInstanceProfile={"Name": profile_name},
        KeyName=args.key,
        NetworkInterfaces=[{
            "DeviceIndex": 0,
            "SubnetId": subnet_id,
            "Groups": [ec2_sg_id],
            "AssociatePublicIpAddress": True,
        }],
        BlockDeviceMappings=[{
            "DeviceName": "/dev/sda1",
            "Ebs": {"VolumeSize": 20, "VolumeType": "gp3"},
        }],
        UserData="""#!/bin/bash
apt-get update -y
apt-get install -y git nfs-common docker.io awscli
systemctl enable docker
systemctl start docker
usermod -aG docker ubuntu
mkdir -p /mnt/artifacts
cd /home/ubuntu
git clone https://github.com/Harivelu0/s3-files-ml-serving.git
chown -R ubuntu:ubuntu s3-files-ml-serving
""",
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": f"{NAME}-instance"}],
        }],
    )
    instance_id = reservation["Instances"][0]["InstanceId"]
    log("5/5", f"✓ EC2 launched: {instance_id}")
    wait(30, "instance initializing")
    pub_ip = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0].get("PublicIpAddress", "pending")
    log("5/5", f"✓ Public IP: {pub_ip}")
    return instance_id, pub_ip


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

    print(f"\n{'='*50}")
    print(f"  ✓ Setup complete")
    print(f"{'='*50}")
    print(f"  S3 Bucket:        {bucket_name}")
    print(f"  S3 File System:   {fs_id}")
    print(f"  Mount Target DNS: {mt_dns}")
    print(f"  EC2 Instance:     {instance_id}")
    print(f"  EC2 Public IP:    {pub_ip}")
    print(f"\n  Next steps:")
    print(f"  1. Upload artifacts: ./scripts/upload_artifacts.sh {bucket_name}")
    print(f"  2. SSH:              ssh -i {args.key}.pem ubuntu@{pub_ip}")
    print(f"  3. Mount S3 Files:   sudo bash scripts/mount_s3.sh {fs_id} /mnt/artifacts")
    print(f"  4. Run container:    sudo docker run -d -p 8000:8000 \\")
    print(f"                         -v /mnt/artifacts:/mnt/artifacts:ro \\")
    print(f"                         -e ARTIFACTS_DIR=/mnt/artifacts/artifacts \\")
    print(f"                         <ecr-image-uri>")
    print(f"  5. Open:             http://{pub_ip}:8000")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
