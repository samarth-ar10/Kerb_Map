"""Login helper for the retrieval API. Never prints credentials or tokens."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _config() -> dict[str, str]:
    configs = sorted(Path("/config").glob("*.config"))
    if len(configs) != 1:
        raise SystemExit(f"expected exactly one /config/*.config, found {len(configs)}")
    env: dict[str, str] = {}
    for raw in configs[0].read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def login() -> tuple[str, str]:
    env = _config()
    backend = env["INGRESS_URL"].rstrip("/")
    body = json.dumps({"username": env["USERNAME"], "password": env["PASSWORD"]}).encode()
    request = urllib.request.Request(
        backend + "/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    token = payload.get("access_token")
    if not token:
        raise SystemExit("login did not return an access token")
    return backend, token


def get_json(backend: str, token: str, path: str, timeout: int = 120) -> dict:
    request = urllib.request.Request(
        backend + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise RuntimeError(f"GET {path.split('?')[0]} failed: {exc.code} {detail}") from exc


def stream_url(backend: str, token: str, source: str) -> str:
    query = urllib.parse.urlencode({"source": source, "token": token})
    return f"{backend}/api/v1/videos/stream?{query}"
