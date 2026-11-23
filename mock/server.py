"""Fake third-party notification service

DELAY (seconds) controls how slow it responds. Scenario 3 sets it high
so the API's outbound call times out
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = float(os.environ.get("DELAY", "0.05"))


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply(200, {"status": "ok", "delay_s": DELAY})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        time.sleep(DELAY)
        self._reply(200, {"received": True})

    def log_message(self, fmt, *args):
        print(f"mock-webhook {self.command} {self.path} delay={DELAY}s", flush=True)


if __name__ == "__main__":
    print(f"mock-webhook listening on :9000 with DELAY={DELAY}s", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
