"""Assemble the flat deploy folder deploy/app/ (a ConfigMap takes only top-level files).

    python query/build_deploy.py                 # KERB_DB=vastdb deployment (default)
    python query/build_deploy.py --backend local # no VastDB SDK in requirements

Copies the modules flat, rewrites package imports (from common.x / from query.x) to flat imports,
writes a small requirements.txt, then checks: no sub-folders, total size under 900 KB, and no URL in
the HTML/JS that starts with "/" (the app is served under /app, so every URL must be relative).
Exits non-zero if a check fails.
"""

import argparse
import os
import re
import shutil
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(REPO, "deploy", "app")
MAX_BYTES = 900 * 1024

FILES = {
    "main.py": "query/api.py",
    "store.py": "query/store.py",
    "labels.py": "query/labels.py",
    "sources.py": "query/sources.py",
    "search.py": "query/search.py",
    "evidence.py": "query/evidence.py",
    "replay.py": "query/replay.py",
    "drivelog.py": "query/drivelog.py",
    "timeline.py": "query/timeline.py",
    "upload.py": "query/upload.py",
    "kerbpipe.py": "query/kerbpipe.py",
    "samples_gps.json": "query/samples_gps.json",
    "kerbdb.py": "common/kerbdb.py",
    "kerb_schema.py": "common/kerb_schema.py",
    "index.html": "query/static/index.html",
    "setup.html": "query/static/setup.html",
    "drive.html": "query/static/drive.html",
    "find.html": "query/static/find.html",
    "find.js": "query/static/find.js",
    "explore.html": "query/static/explore.html",
    "label.html": "query/static/label.html",
    "board.html": "query/static/board.html",
    "measured.html": "query/static/measured.html",
    "app.js": "query/static/app.js",
    "api.js": "query/static/api.js",
    "label.js": "query/static/label.js",
    "style.css": "query/static/style.css",
    "favicon.svg": "query/static/favicon.svg",
    "favicon.png": "query/static/favicon.png",
}

BINARY_EXT = {".png", ".jpg", ".jpeg", ".webp", ".ico", ".gif"}

REQUIREMENTS = {
    "vastdb": ["fastapi>=0.110", "uvicorn>=0.29", "pyarrow>=15", "vastdb>=1.3"],
    "local": ["fastapi>=0.110", "uvicorn>=0.29", "pyarrow>=15"],
}

IMPORT_RULES = [
    (re.compile(r"^(\s*)from (?:common|query) import (\w+) as (\w+)$", re.M), r"\1import \2 as \3"),
    (re.compile(r"^(\s*)from (?:common|query)\.(\w+) import ", re.M), r"\1from \2 import "),
    (re.compile(r"^(\s*)from (?:common|query) import ([\w, ]+)$", re.M), r"\1import \2"),
]

ABSOLUTE_URL = [
    re.compile(r"""(?:src|href|action)\s*=\s*["']/(?!/)"""),
    re.compile(r"""fetch\(\s*["'`]/(?!/)"""),
    re.compile(r"""["'`]/(?:api|label|health|static)\b"""),
]


def flatten_imports(text):
    for pattern, repl in IMPORT_RULES:
        text = pattern.sub(repl, text)
    return text


def write_replay_chunks(dest):
    """Compact replay timeline (one row per chunk) so the app does not need the full data/ folder."""
    sys.path.insert(0, REPO)
    from query.timeline import load_chunks
    for name in ("video_geo.csv", "pie_map.csv"):
        src = os.path.join(REPO, "data", name)
        if os.path.isfile(src):
            break
    else:
        print("note: no data/video_geo.csv or data/pie_map.csv, replay timeline left out")
        return
    import csv
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["drive", "camera_id", "chunk_filename", "original_video"])
        for drive, chunk, video in load_chunks(src):
            w.writerow([drive, drive.split(":")[0], chunk, video])
    print(f"replay timeline from data/{name}")


def check(out):
    problems = []
    entries = os.listdir(out)
    dirs = [e for e in entries if os.path.isdir(os.path.join(out, e))]
    if dirs:
        problems.append(f"sub-folders not allowed in a ConfigMap: {dirs}")
    total = sum(os.path.getsize(os.path.join(out, e)) for e in entries if os.path.isfile(os.path.join(out, e)))
    if total > MAX_BYTES:
        problems.append(f"total {total} bytes > {MAX_BYTES}")
    for e in entries:
        path = os.path.join(out, e)
        if e.endswith((".html", ".js")):
            text = open(path, encoding="utf-8").read()
            for pat in ABSOLUTE_URL:
                for m in pat.finditer(text):
                    line = text[:m.start()].count("\n") + 1
                    problems.append(f"{e}:{line}: absolute URL {m.group(0)!r}")
        if e.endswith(".py"):
            text = open(path, encoding="utf-8").read()
            if re.search(r"^\s*(from|import) (common|query)\b", text, re.M):
                problems.append(f"{e}: package import left after flattening")
    return total, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=sorted(REQUIREMENTS), default="vastdb")
    args = ap.parse_args()

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    for dest, src in FILES.items():
        src_path = os.path.join(REPO, src)
        if os.path.splitext(dest)[1].lower() in BINARY_EXT:
            shutil.copyfile(src_path, os.path.join(OUT, dest))
            continue
        text = open(src_path, encoding="utf-8").read()
        if dest.endswith(".py"):
            text = flatten_imports(text)
        with open(os.path.join(OUT, dest), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    write_replay_chunks(os.path.join(OUT, "replay_chunks.csv"))
    with open(os.path.join(OUT, "requirements.txt"), "w", newline="\n") as fh:
        fh.write("\n".join(REQUIREMENTS[args.backend]) + "\n")

    if os.path.exists(os.path.join(OUT, "preview-data.json")):
        os.remove(os.path.join(OUT, "preview-data.json"))
    total, problems = check(OUT)
    for p in problems:
        print("FAIL", p)
    print(f"{'FAIL' if problems else 'OK'}: deploy/app has {len(os.listdir(OUT))} files, {total} bytes "
          f"(limit {MAX_BYTES}), backend {args.backend}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
