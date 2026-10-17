"""Tiny local stand-in for the cluster ingress: serves the app under /app with the prefix stripped.

    python query/prefix_proxy.py --listen 8090 --target 8080
Requests to http://127.0.0.1:8090/app/<path> are forwarded to http://127.0.0.1:8080/<path>.
Anything outside /app gets 404, like the VSS UI owning the rest of the host. Rehearsal use only.
"""

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/app"


def make_handler(target_port):
    class Handler(BaseHTTPRequestHandler):
        def _forward(self):
            if not (self.path == PREFIX or self.path.startswith(PREFIX + "/") or self.path.startswith(PREFIX + "?")):
                self.send_error(404, "outside /app")
                return
            path = self.path[len(PREFIX):] or "/"
            if path.startswith("?"):
                path = "/" + path
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
            conn = http.client.HTTPConnection("127.0.0.1", target_port, timeout=60)
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()

        do_GET = do_POST = do_HEAD = _forward

        def log_message(self, *args):
            pass

    return Handler


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=8090)
    ap.add_argument("--target", type=int, default=8080)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.listen), make_handler(a.target)).serve_forever()
