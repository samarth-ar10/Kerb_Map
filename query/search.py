"""Semantic ranking for /api/search: filter in the DB first, then rank by cosine similarity.

Embedder (env KERB_EMBEDDER):
- "cosmos": Cosmos Embed1 (256-dim). URL from COSMOS_EMBED1_URL (default http://$GPU_HOST:8003 with
  GPU_HOST=166.19.38.112, as in the starter-kit gpu README); bearer token GPU_BEARER_TOKEN from env or
  the single /config/*.config, read at runtime and never logged. Query text is embedded with
  request_type "query" and compared with the stored kerb_spaces.embedding vectors.
- "hash": TEST-ONLY offline embedder (hashed bag of words over receipt_text/reason/street). It has no
  semantic knowledge and exists so the ranking code can be tested without the GPU endpoint.
Default: cosmos when a bearer token is available, else hash.
"""

import glob
import hashlib
import json
import math
import os
import re
import urllib.request

DIM = 256
DEFAULT_GPU_HOST = "166.19.38.112"


# ---------------------------------------------------------------- embedders

def _gpu_token():
    tok = os.environ.get("GPU_BEARER_TOKEN")
    if tok:
        return tok
    paths = sorted(glob.glob("/config/*.config"))
    if len(paths) != 1:
        return None
    with open(paths[0]) as fh:
        for line in fh:
            line = line.strip()
            if line.removeprefix("export ").startswith("GPU_BEARER_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def embedder_name():
    choice = (os.environ.get("KERB_EMBEDDER") or "").strip().lower()
    if choice in ("cosmos", "hash"):
        return choice
    return "cosmos" if _gpu_token() else "hash"


def hash_embed(text: str) -> list:
    """TEST-ONLY: hashed bag-of-words vector, L2-normalised."""
    vec = [0.0] * DIM
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class CosmosEmbedder:
    def __init__(self):
        host = os.environ.get("GPU_HOST") or DEFAULT_GPU_HOST
        self.url = (os.environ.get("COSMOS_EMBED1_URL") or f"http://{host}:8003").rstrip("/")
        self.token = _gpu_token()
        if not self.token:
            raise RuntimeError("GPU_BEARER_TOKEN not set (env or /config/<team>.config)")
        self._model = os.environ.get("COSMOS_EMBED1_MODEL")

    def _call(self, path, payload=None):
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode() if payload else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def model(self):
        if not self._model:
            self._model = self._call("/v1/models")["data"][0]["id"]
        return self._model

    def embed(self, text: str) -> list:
        out = self._call("/v1/embeddings", {"input": text, "model": self.model(), "request_type": "query",
                                            "encoding_format": "float"})
        vec = out["data"][0]["embedding"]
        if len(vec) != DIM:
            raise RuntimeError(f"Cosmos Embed1 returned {len(vec)} dims, expected {DIM}")
        return vec


def embed_query(text, name=None):
    name = name or embedder_name()
    return CosmosEmbedder().embed(text) if name == "cosmos" else hash_embed(text)


def space_text(space):
    return " ".join(str(space.get(k) or "") for k in ("state", "reason", "street", "receipt_text"))


# ---------------------------------------------------------------- ranking

def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else None


def rank(q, candidates, name=None):
    """Return ([(score, space), ...] best first, embedder name). Spaces without a vector go last (score None)."""
    name = name or embedder_name()
    qv = embed_query(q, name)
    scored = []
    for s in candidates:
        vec = s.get("embedding") if name == "cosmos" else hash_embed(space_text(s))
        score = cosine(qv, vec)
        scored.append((None if score is None else round(score, 4), s))
    scored.sort(key=lambda p: (p[0] is None, -(p[0] or 0)))
    return scored, name


def describe_query(q, lat, lon, radius_m, state, side, source, embedder):
    parts = []
    if state or side or source:
        conds = [f"{k} = {v!r}" for k, v in (("state", state), ("side", side), ("source", source)) if v]
        parts.append("filter kerb_spaces where " + " and ".join(conds))
    else:
        parts.append("all kerb_spaces")
    if lat is not None and lon is not None:
        parts.append(f"within {radius_m:g} m of ({lat:.5f}, {lon:.5f})")
    if embedder == "cosmos":
        parts.append(f"then rank by cosine between the Cosmos Embed1 vector of {q!r} and each stored embedding")
    else:
        parts.append(f"then rank by cosine of hashed word vectors of {q!r} vs receipt text (TEST-ONLY embedder)")
    return ", ".join(parts)


def explain(q, space):
    """Short text for a receipt opened from a search: what was asked and how this space scored."""
    name = embedder_name()
    try:
        ranked, _ = rank(q, [space], name)
        score = ranked[0][0]
    except Exception:
        score = None
    return {"q": q, "embedder": name, "score": score,
            "how": f"state/side/source filter and radius in the DB query, then cosine similarity ({name})"}
