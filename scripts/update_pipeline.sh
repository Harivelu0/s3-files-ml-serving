#!/bin/bash
# update_pipeline.sh
# Runs inside updater container — crawl changed pages + rebuild indexes.
# Triggered weekly by EventBridge → ECS run-task.
#
# Environment vars required:
#   ARTIFACTS_DIR   path to S3 Files mount (e.g. /mnt/artifacts)
#   CORPUS_PATH     path to corpus.jsonl (e.g. /mnt/artifacts/corpus.jsonl)

set -euo pipefail

ARTIFACTS_DIR="${ARTIFACTS_DIR:-/mnt/artifacts}"
CORPUS_PATH="${CORPUS_PATH:-${ARTIFACTS_DIR}/corpus.jsonl}"
TMP_DIR="/tmp/update_$$"

echo "========================================"
echo "  AWS Docs Weekly Update Pipeline"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "========================================"

# ── Step 1: Crawl changed pages ───────────────────────────────────────────────
echo ""
echo "[1/4] Crawling updated AWS docs..."
python scripts/crawl_aws_docs.py --update
echo "  ✓ Corpus updated at ${CORPUS_PATH}"

# ── Step 2: Rebuild indexes into tmp dir ──────────────────────────────────────
echo ""
echo "[2/4] Rebuilding indexes..."
mkdir -p "${TMP_DIR}"
python precompute/build_index.py \
    --corpus "${CORPUS_PATH}" \
    --out    "${TMP_DIR}"
echo "  ✓ New indexes built at ${TMP_DIR}"

# ── Step 3: Verify new artifacts exist ───────────────────────────────────────
echo ""
echo "[3/4] Verifying new artifacts..."
for f in faiss.index faiss_ids.pkl bm25_index.pkl corpus_meta.pkl; do
    if [ ! -f "${TMP_DIR}/${f}" ]; then
        echo "  ✗ Missing: ${f} — aborting"
        exit 1
    fi
    echo "  ✓ ${f} $(du -h ${TMP_DIR}/${f} | cut -f1)"
done

# ── Step 4: Atomic swap ───────────────────────────────────────────────────────
echo ""
echo "[4/4] Swapping artifacts (atomic)..."
for f in faiss.index faiss_ids.pkl bm25_index.pkl corpus_meta.pkl; do
    cp "${TMP_DIR}/${f}" "${ARTIFACTS_DIR}/${f}.new"
    mv "${ARTIFACTS_DIR}/${f}.new" "${ARTIFACTS_DIR}/${f}"
    echo "  ✓ ${f} swapped"
done

rm -rf "${TMP_DIR}"

echo ""
echo "========================================"
echo "  ✓ Update complete"
echo "  Serving container picks up new"
echo "  indexes on next query — zero downtime"
echo "========================================"
