# s3-files-ml-serving

Semantic search over AWS documentation using FAISS + BM25 hybrid retrieval, served via Amazon S3 Files NFS mount on ECS EC2.

---

## Stack

| | |
|---|---|
| Embeddings | all-MiniLM-L6-v2 |
| Semantic search | FAISS (IndexFlatIP) |
| Keyword search | BM25Okapi |
| Serving | FastAPI + Jinja2 |
| Artifact storage | Amazon S3 Files (NFS) |
| Compute | ECS on EC2 (t3.medium) |
| Infra | boto3 |
| Updates | EventBridge → ECS run-task |

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
│   ├── crawl_aws_docs.py    sitemap-based crawler with lastmod tracking
│   ├── setup_infra.py       provisions S3, S3 Files, EC2, ECS
│   ├── upload_artifacts.sh  uploads indexes to S3
│   ├── mount_s3.sh          mounts S3 File System on EC2 via NFSv4.1
│   ├── update_pipeline.sh   crawl + rebuild + atomic artifact swap
│   └── schedule_update.py   sets up EventBridge weekly cron
```

---

## Local setup

```bash
pip install -r precompute/requirements.txt
pip install -r app/requirements.txt

python scripts/crawl_aws_docs.py
python precompute/build_index.py --corpus data/corpus.jsonl --out artifacts/

uvicorn app.main:app --reload
# http://localhost:8000
```

## Deploy

```bash
python scripts/setup_infra.py --region us-east-1 --name ml-serving
./scripts/upload_artifacts.sh <bucket-name>
sudo ./scripts/mount_s3.sh <mount-target-dns> /mnt/artifacts

docker build -t aws-docs-search ./app
docker tag aws-docs-search <ecr-uri>
docker push <ecr-uri>

python scripts/schedule_update.py \
    --cluster ml-serving-cluster \
    --task-def ml-serving-task \
    --subnet <subnet-id>
```
