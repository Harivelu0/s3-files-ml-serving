#!/bin/bash
# upload_artifacts.sh
# Upload precomputed artifacts to S3 bucket.
# Run after build_index.py has generated artifacts/
#
# Usage:
#   chmod +x scripts/upload_artifacts.sh
#   ./scripts/upload_artifacts.sh <bucket-name>

set -euo pipefail

BUCKET="${1:?Usage: $0 <bucket-name>}"
ARTIFACTS_DIR="artifacts/"

echo "[1/2] Checking artifacts..."
for f in faiss.index faiss_ids.pkl bm25_index.pkl; do
  if [ ! -f "${ARTIFACTS_DIR}${f}" ]; then
    echo "  ✗ Missing: ${ARTIFACTS_DIR}${f} — run precompute/build_index.py first"
    exit 1
  fi
  echo "  ✓ ${f}"
done

echo "[2/2] Uploading to s3://${BUCKET}/artifacts/..."
aws s3 cp "${ARTIFACTS_DIR}" "s3://${BUCKET}/artifacts/" \
  --recursive \
  --exclude "*.py" \
  --exclude "*.jsonl"

echo "✓ Upload complete"
aws s3 ls "s3://${BUCKET}/artifacts/" --human-readable
