#!/bin/bash
# mount_s3.sh
# Mount an S3 File System using amazon-efs-utils (mount -t s3files)
#
# Usage:
#   sudo bash scripts/mount_s3.sh <file-system-id> <mount-point>
#
# Example:
#   sudo bash scripts/mount_s3.sh fs-01ceba8a32badb25b /mnt/artifacts

set -euo pipefail

FS_ID="${1:?Usage: $0 <file-system-id> <mount-point>}"
MOUNT_POINT="${2:-/mnt/artifacts}"

echo "[1/3] Installing amazon-efs-utils (S3 Files client)..."
curl -s https://amazon-efs-utils.aws.com/efs-utils-installer.sh | sh

echo "[2/3] Creating mount point: $MOUNT_POINT"
mkdir -p "$MOUNT_POINT"

echo "[3/3] Mounting S3 File System..."
mount -t s3files "${FS_ID}:/" "$MOUNT_POINT"

echo "✓ Mounted at $MOUNT_POINT"
df -h "$MOUNT_POINT"

# Persist across reboots
FSTAB_ENTRY="${FS_ID}:/ ${MOUNT_POINT} s3files _netdev 0 0"
if ! grep -qF "$FS_ID" /etc/fstab; then
    echo "$FSTAB_ENTRY" >> /etc/fstab
    echo "✓ Added to /etc/fstab"
fi
