"""Get the boxed frames for queued Cosmos checks: send the request list to the team VM, run vm_frames.py there,
copy the JPEGs back to <bystander-data>/dashcam-frames/. Frames stay out of the repo (kept only for the model check).
Usage: python dashcam/frames.py set04 [set05 ...]
"""
import pathlib, subprocess, sys

REPO = pathlib.Path(__file__).resolve().parents[1]
WORK = REPO / "dashcam" / "out" / "work"
LOCAL = pathlib.Path(r"C:\Users\samar\bystander-data\dashcam-frames")
SSH = ["C:/Windows/System32/OpenSSH/ssh.exe", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
SCP = ["C:/Windows/System32/OpenSSH/scp.exe", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
HOST = "bc-samarthar10"


def main():
    LOCAL.mkdir(parents=True, exist_ok=True)
    for s in sys.argv[1:]:
        req = WORK / f"{s}_frame_requests.json"
        if req.read_text().strip() in ("", "[]"):
            print(s, "no frames needed"); continue
        subprocess.run(SCP + [str(req), f"{HOST}:kerb-data/{req.name}"], check=True)
        subprocess.run(SSH + [HOST, f"cd ~/kerb-data && python3 vm_frames.py {req.name} && "
                                    f"tar cf dashcam-frames-{s}.tar -C dashcam-frames $(python3 -c \"import json;"
                                    f"print(' '.join(q['key']+'.jpg' for q in json.load(open('{req.name}'))))\") 2>/dev/null; true"],
                       check=True)
        tar = LOCAL / f"frames-{s}.tar"
        subprocess.run(SCP + [f"{HOST}:kerb-data/dashcam-frames-{s}.tar", str(tar)], check=True)
        subprocess.run(["tar", "xf", tar.name], cwd=LOCAL, check=True)
        tar.unlink()
        print(s, "frames copied")


if __name__ == "__main__":
    main()
