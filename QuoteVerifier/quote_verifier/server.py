"""Small local HTTP adapter. POST /verify returns a JSON boolean."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import Verifier
from .sources import SourceError

MAX_REQUEST_BYTES = 100_000


def make_handler(verifier):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QuoteVerifier/0.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def respond(self, status, value):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def error(self, status, code, message):
            self.respond(status, {"error": {"code": code, "message": message}})

        def do_GET(self):
            if self.path == "/health":
                self.respond(200, {"status": "ok"})
            else:
                self.error(404, "not_found", "Unknown endpoint")

        def do_POST(self):
            if self.path not in ("/verify", "/verify/details"):
                self.error(404, "not_found", "Unknown endpoint")
                return
            if self.headers.get_content_type() != "application/json":
                self.error(415, "unsupported_media_type", "Use application/json")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                self.error(400, "invalid_request", "One Content-Length header is required")
                return
            try:
                length = int(lengths[0])
            except ValueError:
                self.error(400, "invalid_request", "Invalid Content-Length")
                return
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self.error(413, "invalid_size", "Request body must be 1–100000 bytes")
                return
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Incomplete request body")
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Request must be a JSON object")
                if set(payload) - {"arxiv_id", "quote", "mode"}:
                    raise ValueError("Unknown request fields")
                if not {"arxiv_id", "quote"}.issubset(payload):
                    raise ValueError("arxiv_id and quote are required")
                result = verifier.verify(
                    payload["arxiv_id"], payload["quote"], payload.get("mode", "normalized"),
                )
            except SourceError as exc:
                self.error(exc.status, exc.code, str(exc))
            except (ValueError, TypeError) as exc:
                self.error(400, "invalid_request", str(exc))
            except TimeoutError:
                self.error(408, "request_timeout", "Request timed out")
            except Exception:
                self.error(500, "internal_error", "Unexpected verification failure")
            else:
                self.respond(200, result.to_dict() if self.path.endswith("/details") else result.found)

    return Handler


def serve(host="127.0.0.1", port=8765, verifier=None):
    server = ThreadingHTTPServer((host, port), make_handler(verifier or Verifier()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
