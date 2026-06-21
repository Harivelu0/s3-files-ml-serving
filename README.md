# s3-files-ml-serving

Semantic search engine over AWS documentation using FAISS + BM25 hybrid retrieval, served via Amazon S3 Files NFS mount on EC2.

Built to test and demonstrate Amazon S3 Files (GA April 2026) for serving large ML artifacts without cold start downloads or container redeployments.

---

## Stack

| | |
|---|---|
| Embeddings | all-MiniLM-L6-v2 |
| Semantic search | FAISS (IndexFlatIP) |
| Keyword search | BM25Okapi |
| Serving | FastAPI + Jinja2 |
| Artifact storage | Amazon S3 + S3 Files (NFS) |
| Compute | EC2 t3.medium (Ubuntu 22.04) |
| Container | Docker |
| Weekly updates | EventBridge → EC2 run |

---

## How it works

```
AWS docs crawled via sitemaps (5048 pages)
        ↓
FAISS + BM25 indexes built locally
        ↓
Artifacts uploaded to S3 bucket
        ↓
S3 File System mounted on EC2 via NFS
        ↓
FastAPI reads indexes directly from mount
No download. No boto3. Just open().
        ↓
User queries → hybrid search → ranked docs
```

---

## Structure

```
├── app/
│   ├── main.py              hybrid search endpoint + UI
│   ├── templates/index.html search UI
│   ├── Dockerfile
│   └── requirements.txt
├── precompute/
│   ├── build_index.py       builds FAISS + BM25 + corpus_meta
│   └── requirements.txt
├── scripts/
│   ├── crawl_aws_docs.py    sitemap crawler with lastmod tracking
│   ├── setup_infra.py       provisions S3, S3 Files, EC2 via boto3
│   ├── upload_artifacts.sh  uploads indexes to S3
│   ├── mount_s3.sh          mounts S3 File System on EC2 (amazon-efs-utils)
│   ├── update_pipeline.sh   crawl + rebuild + atomic artifact swap
│   └── schedule_update.py   sets up EventBridge weekly cron
```

---

## Local setup

```bash
# Install dependencies
pip install -r precompute/requirements.txt
pip install -r app/requirements.txt

# Crawl AWS docs
python scripts/crawl_aws_docs.py

# Build indexes
python precompute/build_index.py --corpus data/corpus.jsonl --out artifacts/

# Run locally
uvicorn app.main:app --reload
# open http://localhost:8000
```

---

## AWS deploy

```bash
# 1. Provision infra
python scripts/setup_infra.py --region us-east-1 --name ml-serving

# 2. Upload artifacts
./scripts/upload_artifacts.sh <bucket-name>

# 3. Build + push Docker image
aws ecr create-repository --repository-name aws-docs-search --region us-east-1
docker build -t aws-docs-search ./app
docker tag aws-docs-search <account-id>.dkr.ecr.us-east-1.amazonaws.com/aws-docs-search:latest
docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/aws-docs-search:latest

# 4. SSH into EC2
ssh -i ml-serving-key.pem ubuntu@<ec2-ip>

# 5. Install amazon-efs-utils + mount S3 Files
sudo bash scripts/mount_s3.sh <file-system-id> /mnt/artifacts

# 6. Run container
sudo docker run -d \
  --name aws-docs-search \
  -p 8000:8000 \
  -v /mnt/artifacts:/mnt/artifacts:ro \
  -e ARTIFACTS_DIR=/mnt/artifacts/artifacts \
  <ecr-image-uri>
```

---

## IAM permissions required

**S3 Files role** (assumed by elasticfilesystem.amazonaws.com):
```
s3:GetObject, PutObject, DeleteObject, ListBucket
s3:GetBucketNotification, PutBucketNotification, GetBucketVersioning
events:PutRule, DeleteRule, DescribeRule, PutTargets, RemoveTargets
```

**EC2 instance role:**
```
AmazonS3ReadOnlyAccess
AmazonS3FilesClientFullAccess
AmazonEC2ContainerRegistryReadOnly
```

---

## Weekly index update

```bash
# Runs automatically every Sunday via EventBridge
# Or trigger manually:
bash scripts/update_pipeline.sh

# Setup schedule:
python scripts/schedule_update.py \
    --cluster ml-serving-cluster \
    --task-def ml-serving-task \
    --subnet <subnet-id>
```

Update flow:
```
crawl --update (only changed pages)
    → rebuild FAISS + BM25
    → atomic swap on S3 Files mount
    → serving reads new indexes instantly
    → zero downtime, zero container restart
```

---

## Issues faced

| Issue | Fix |
|---|---|
| S3 Files IAM principal wrong | `elasticfilesystem.amazonaws.com` not `s3files.amazonaws.com` |
| Bucket versioning required | S3 Files needs versioning enabled on bucket |
| File system stuck creating | IAM role missing EventBridge permissions |
| API params are camelCase | `bucket` not `Bucket`, `roleArn` not `RoleArn` |
| Plain NFS mount failed | Needs `amazon-efs-utils` + `mount -t s3files` |
| efs-utils build failed | Missing Rust, Go, cmake — installed all |
| EC2 disk full | Default 8GB too small — set 20GB in launch config |
| Jinja2 template error | Updated to `TemplateResponse(request=request, name=...)` |
