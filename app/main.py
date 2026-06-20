#!/usr/bin/env python3
"""
main.py  FastAPI semantic search over AWS docs.
Reads FAISS + BM25 + metadata directly from S3 Files mount.
"""

import os
import pickle
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import faiss
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer
from sklearn.preprocessing import normalize as sk_normalize

BASE_DIR      = Path(__file__).resolve().parent
ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", BASE_DIR.parent / "artifacts"))
MODEL_NAME    = "all-MiniLM-L6-v2"
TOP_K         = int(os.getenv("TOP_K", "10"))

state     = {}
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[startup] Loading artifacts from {ARTIFACTS_DIR}...")

    if not (ARTIFACTS_DIR / "faiss.index").exists():
        raise RuntimeError(f"Artifacts not found at {ARTIFACTS_DIR}. Run build_index.py first.")

    state["index"]    = faiss.read_index(str(ARTIFACTS_DIR / "faiss.index"))
    state["model"]    = SentenceTransformer(MODEL_NAME)

    with open(ARTIFACTS_DIR / "faiss_ids.pkl", "rb") as f:
        state["faiss_ids"] = pickle.load(f)

    with open(ARTIFACTS_DIR / "bm25_index.pkl", "rb") as f:
        d = pickle.load(f)
        state["bm25"]     = d["bm25"]
        state["bm25_ids"] = d["ids"]

    with open(ARTIFACTS_DIR / "corpus_meta.pkl", "rb") as f:
        state["meta"] = pickle.load(f)

    print(f"[startup] Ready. {state['index'].ntotal} docs indexed.")
    yield
    state.clear()


app = FastAPI(title="AWS Docs Semantic Search", lifespan=lifespan)


# ── Schemas ───────────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str
    top_k: int = TOP_K
    alpha: float = 0.6


class SearchResult(BaseModel):
    id:      str
    title:   str
    snippet: str
    url:     str
    score:   float


class SearchResponse(BaseModel):
    query:   str
    results: List[SearchResult]
    total:   int


# ── Search logic ──────────────────────────────────────────────────────────────
def faiss_search(query: str, top_k: int) -> dict:
    vec = state["model"].encode([query])
    vec = sk_normalize(vec).astype(np.float32)
    scores, idxs = state["index"].search(vec, top_k)
    return {
        state["faiss_ids"][i]: float(s)
        for i, s in zip(idxs[0], scores[0]) if i != -1
    }


def bm25_search(query: str, top_k: int) -> dict:
    tokens  = query.lower().split()
    scores  = state["bm25"].get_scores(tokens)
    top_idx = np.argsort(scores)[::-1][:top_k]
    max_s   = scores[top_idx[0]] if scores[top_idx[0]] > 0 else 1.0
    return {state["bm25_ids"][i]: float(scores[i]) / max_s for i in top_idx}


def hybrid_search(query: str, top_k: int, alpha: float) -> List[SearchResult]:
    f_scores = faiss_search(query, top_k * 2)
    b_scores = bm25_search(query, top_k * 2)
    all_ids  = set(f_scores) | set(b_scores)

    combined = {
        doc_id: alpha * f_scores.get(doc_id, 0.0) + (1 - alpha) * b_scores.get(doc_id, 0.0)
        for doc_id in all_ids
    }
    ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)[:top_k]

    results = []
    for doc_id, score in ranked:
        m = state["meta"].get(doc_id, {})
        results.append(SearchResult(
            id=doc_id,
            title=m.get("title", doc_id),
            snippet=m.get("snippet", ""),
            url=m.get("url", ""),
            score=round(score, 4),
        ))
    return results


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "docs_indexed": state.get("index", None) and state["index"].ntotal,
        "artifacts_dir": str(ARTIFACTS_DIR),
    }


@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest):
    results = hybrid_search(req.query, req.top_k, req.alpha)
    return SearchResponse(query=req.query, results=results, total=len(results))
