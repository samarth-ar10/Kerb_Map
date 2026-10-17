"""Extract one frame per request, with the candidate gap boxed, for the Cosmos check (runs on the team VM).

Input JSON: [{"key", "source", "t", "box": [x1, y1, x2, y2]}] (box in original pixels). For each source the segment
video is fetched once through the playback URL (token read inside this process only, never printed), then ffmpeg
writes <key>.jpg (960 px wide, gap outlined in yellow) to ~/kerb-data/dashcam-frames/. Skips frames already written.
Usage: python3 vm_frames.py requests.json
"""
import json, pathlib, subprocess, sys, tempfile, collections
import requests

HOME = pathlib.Path.home()
OUT = HOME / "kerb-data" / "dashcam-frames"
OUT.mkdir(parents=True, exist_ok=True)
BACKEND = (HOME / ".vss_backend").read_text().strip().rstrip("/")
TOKEN = (HOME / ".vss_token").read_text().strip()
H = {"Authorization": "Bearer " + TOKEN}


def video(src, tmp):
    r = requests.get(BACKEND + "/api/v1/videos/playback-url", headers=H, params={"source": src, "token": TOKEN}, timeout=60)
    r.raise_for_status()
    url = r.json()["url"]
    if url.startswith("/"):
        url = BACKEND + url
    path = pathlib.Path(tmp) / (src.rsplit("/", 1)[-1])
    with requests.get(url, stream=True, timeout=120) as v:
        v.raise_for_status()
        with open(path, "wb") as fh:
            for chunk in v.iter_content(1 << 20):
                fh.write(chunk)
    return path


def main():
    reqs = json.load(open(sys.argv[1]))
    by = collections.defaultdict(list)
    for q in reqs:
        if not (OUT / f"{q['key']}.jpg").exists():
            by[q["source"]].append(q)
    ok = fail = 0
    with tempfile.TemporaryDirectory() as tmp:
        for src, qs in by.items():
            try:
                path = video(src, tmp)
            except Exception as exc:
                print("FAIL video", src.rsplit("/", 1)[-1], type(exc).__name__, flush=True); fail += len(qs); continue
            for q in qs:
                x1, y1, x2, y2 = [int(v) for v in q["box"]]
                vf = f"drawbox=x={x1}:y={y1}:w={max(4, x2 - x1)}:h={max(4, y2 - y1)}:color=yellow:t=6,scale=960:-2"
                cmd = ["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{q['t']:.3f}", "-i", str(path),
                       "-frames:v", "1", "-vf", vf, "-q:v", "3", str(OUT / f"{q['key']}.jpg")]
                if subprocess.run(cmd).returncode == 0:
                    ok += 1
                else:
                    fail += 1
            path.unlink(missing_ok=True)
    print(f"frames ok {ok} fail {fail}", flush=True)


if __name__ == "__main__":
    main()
