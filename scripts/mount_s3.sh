#!/bin/bash
# mount_s3.sh
# Mount an S3 File System (NFS) on the EC2 host.
# Run once after EC2 launch, before starting ECS tasks.
#
# Usage:
#   chmod +x scripts/mount_s3.sh
#   sudo ./scripts/mount_s3.sh <mount-target-dns> <mount-point>
#
# Example:
#   sudo ./scripts/mount_s3.sh fs-0abc1234.s3-files.us-east-1.amazonaws.com /mnt/artifacts

set -euo pipefail

MOUNT_TARGET_DNS="${1:?Usage: $0 <mount-target-dns> <mount-point>}"
MOUNT_POINT="${2:-/mnt/artifacts}"

echo "[1/3] Installing NFS client..."
yum install -y nfs-utils 2>/dev/null || apt-get install -y nfs-common

echo "[2/3] Creating mount point: $MOUNT_POINT"
mkdir -p "$MOUNT_POINT"

echo "[3/3] Mounting S3 File System..."
mount -t nfs4 \
  -o nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2 \
  "${MOUNT_TARGET_DNS}:/" \
  "$MOUNT_POINT"

echo "✓ Mounted at $MOUNT_POINT"
df -h "$MOUNT_POINT"

# Persist across reboots
FSTAB_ENTRY="${MOUNT_TARGET_DNS}:/ ${MOUNT_POINT} nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,_netdev 0 0"
if ! grep -qF "$MOUNT_TARGET_DNS" /etc/fstab; then
  echo "$FSTAB_ENTRY" >> /etc/fstab
  echo "✓ Added to /etc/fstab"
fi
