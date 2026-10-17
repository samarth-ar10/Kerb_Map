"""Fetch evidence (frames, clips) server-side so the browser only talks to this app.

Order for an s3:// URI:
  1. VSS range-capable stream (GET {VSS_URL}/api/v1/videos/stream?source=...&token=JWT), logging in
     with VSS_URL / VSS_USERNAME / VSS_PASSWORD (env first, else INGRESS_URL / USERNAME / PASSWORD
     from the single /config/*.config). No video decoding happens here; bytes are passed through.
  2. Fallback: a SigV4-presigned GET on the team S3 endpoint (S3_ENDPOINT, ACCESS_KEY, SECRET_KEY).
Credentials and tokens are never logged or sent to the browser.
"""

import datetime as dt
import glob
import hashlib
import hmac
import json
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

_CTX = ssl._create_unverified_context()  # internal endpoints use self-signed certs (as ssl_verify=False)
_lock = threading.Lock()
_token = None


def _config_value(key):
    paths = sorted(glob.glob("/config/*.config"))
    if len(paths) != 1:
        return None
    with open(paths[0]) as fh:
        for line in fh:
            line = line.strip().removeprefix("export ")
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def vss_settings():
    url = os.environ.get("VSS_URL") or _config_value("INGRESS_URL")
    user = os.environ.get("VSS_USERNAME") or _config_value("USERNAME")
    password = os.environ.get("VSS_PASSWORD") or _config_value("PASSWORD")
    return (url.rstrip("/") if url else None), user, password


def _vss_token(force=False):
    global _token
    with _lock:
        if _token and not force:
            return _token
        url, user, password = vss_settings()
        if not (url and user and password):
            raise RuntimeError("VSS settings missing")
        req = urllib.request.Request(url + "/api/v1/auth/login",
                                     data=json.dumps({"username": user, "password": password}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as resp:
            _token = json.load(resp)["access_token"]
        return _token


def _open(url, range_header=None):
    headers = {"Range": range_header} if range_header else {}
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60, context=_CTX)


def open_vss_stream(uri, range_header=None):
    url = vss_settings()[0]
    for attempt in (0, 1):
        token = _vss_token(force=attempt == 1)
        q = urllib.parse.urlencode({"source": uri, "token": token})
        try:
            return _open(f"{url}/api/v1/videos/stream?{q}", range_header)
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or attempt == 1:
                raise
    raise RuntimeError("unreachable")


def _s3_settings():
    keys = ("S3_ENDPOINT", "ACCESS_KEY", "SECRET_KEY")
    vals = {k: os.environ.get(k) or _config_value(k) for k in keys}
    if not all(vals.values()):
        raise RuntimeError("S3 settings missing")
    return vals


def presign_s3(uri, expires=600):
    """SigV4 query-string presign for GET s3://bucket/key on S3_ENDPOINT (path-style)."""
    cfg = _s3_settings()
    endpoint = cfg["S3_ENDPOINT"].rstrip("/")
    host = urllib.parse.urlparse(endpoint).netloc
    bucket, _, key = uri[len("s3://"):].partition("/")
    canonical_uri = "/" + bucket + "/" + urllib.parse.quote(key, safe="/~")
    now = dt.datetime.now(dt.timezone.utc)
    amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    region = os.environ.get("S3_REGION", "us-east-1")
    scope = f"{date}/{region}/s3/aws4_request"
    params = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{cfg['ACCESS_KEY']}/{scope}",
              "X-Amz-Date": amz_date, "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host"}
    cqs = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                   for k, v in sorted(params.items()))
    creq = f"GET\n{canonical_uri}\n{cqs}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD"
    sts = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(creq.encode()).hexdigest()}"

    def sign(k, msg):
        return hmac.new(k, msg.encode(), hashlib.sha256).digest()

    k = sign(sign(sign(sign(("AWS4" + cfg["SECRET_KEY"]).encode(), date), region), "s3"), "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return f"{endpoint}{canonical_uri}?{cqs}&X-Amz-Signature={sig}"


def open_s3(uri, range_header=None):
    """Open an s3:// object: VSS stream first, then the presigned S3 GET. Returns a response object."""
    errors = []
    try:
        return open_vss_stream(uri, range_header)
    except Exception as exc:  # message only, never credentials
        errors.append(f"vss: {type(exc).__name__}")
    try:
        return _open(presign_s3(uri), range_header)
    except Exception as exc:
        errors.append(f"s3: {type(exc).__name__}")
    raise RuntimeError("; ".join(errors))
