"""Fetch stored YOLO detections for every Toronto dashcam segment (runs on the team VM).

Reads the backend URL and token from the VM's own files inside this process only; never prints or writes them.
GET requests only. Saves one JSON per segment to ~/kerb-data/dashcam-det/<segment stem>.json and skips files
already saved. Input: a text file of segment_source URIs (one per line), in the order to fetch.
Usage: python3 fetch_detections_vm.py segments.txt [workers]
"""
import json, pathlib, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests

HOME = pathlib.Path.home()
OUT = HOME / "kerb-data" / "dashcam-det"
OUT.mkdir(parents=True, exist_ok=True)
BACKEND = (HOME / ".vss_backend").read_text().strip().rstrip("/")
H = {"Authorization": "Bearer " + (HOME / ".vss_token").read_text().strip()}


def fetch(src):
    stem = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    path = OUT / f"{stem}.json"
    if path.exists() and path.stat().st_size > 100:
        return stem, "skip"
    for attempt in range(3):
        try:
            r = requests.get(BACKEND + "/api/v1/videos/detections", headers=H, params={"source": src}, timeout=90)
            if r.status_code == 200:
                path.write_text(json.dumps(r.json()))
                return stem, "ok"
            status = f"http {r.status_code}"
        except requests.RequestException as exc:
            status = type(exc).__name__
        time.sleep(2 * (attempt + 1))
    return stem, status


def main():
    srcs = [l.strip() for l in open(sys.argv[1]) if l.strip()]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for stem, status in ex.map(fetch, srcs):
            done += 1
            if status not in ("ok", "skip"):
                print("FAIL", stem, status, flush=True)
            if done % 50 == 0:
                print(f"{done}/{len(srcs)} {time.time() - t0:.0f}s", flush=True)
    print(f"done {done} in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
