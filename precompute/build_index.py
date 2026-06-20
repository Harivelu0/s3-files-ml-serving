#!/usr/bin/env python3
"""
build_index.py  Build FAISS + BM25 indexes from AWS docs corpus.

Usage:
    python precompute/build_index.py --corpus data/corpus.jsonl --out artifacts/
"""

import argparse
import json
import pickle
import re
from pathlib import Path
from typing import List

import numpy as np
from sklearn.preprocessing import normalize as sk_normalize
from tqdm import tqdm


def tokenize(text: str) -> List[str]:
    return re.findall(r"[a-zA-Z0-9]+", text.lower())


def load_corpus(path: str):
    ids, texts, meta = [], [], {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                doc_id = rec["id"]
                ids.append(doc_id)
                texts.append(f"{rec.get('title', '')} {rec.get('text', '')}")
                meta[doc_id] = {
                    "title":   rec.get("title", ""),
                    "snippet": rec.get("snippet", ""),
                    "url":     rec.get("url", ""),
                }
            except (json.JSONDecodeError, KeyError):
                continue
    return ids, texts, meta


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", default="artifacts/")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[1/4] Loading corpus...")
    ids, texts, meta = load_corpus(args.corpus)
    print(f"  → {len(ids)} documents")

    print("[2/4] Loading model (all-MiniLM-L6-v2)...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")

    print("[3/4] Building FAISS index...")
    BATCH = 256
    all_embeddings = []
    for i in tqdm(range(0, len(ids), BATCH), desc="  encoding"):
        vecs = model.encode(texts[i:i + BATCH], show_progress_bar=False)
        all_embeddings.append(vecs)

    embeddings = np.vstack(all_embeddings).astype(np.float32)
    embeddings = sk_normalize(embeddings)

    import faiss
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    faiss.write_index(index, str(out_dir / "faiss.index"))

    with open(out_dir / "faiss_ids.pkl", "wb") as f:
        pickle.dump(ids, f)
    print(f"  → {len(ids)} vectors, dim={embeddings.shape[1]}")

    print("[4/4] Building BM25 index...")
    from rank_bm25 import BM25Okapi
    tokenized = [tokenize(t) for t in tqdm(texts, desc="  tokenizing")]
    bm25 = BM25Okapi(tokenized)

    with open(out_dir / "bm25_index.pkl", "wb") as f:
        pickle.dump({"bm25": bm25, "ids": ids}, f)

    with open(out_dir / "corpus_meta.pkl", "wb") as f:
        pickle.dump(meta, f)

    print(f"\n✓ Artifacts saved to {out_dir}")
    print(f"  faiss.index      {len(ids)} vectors")
    print(f"  bm25_index.pkl   BM25 index")
    print(f"  corpus_meta.pkl  title + snippet + url")


if __name__ == "__main__":
    main()
