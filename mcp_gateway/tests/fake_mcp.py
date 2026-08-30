"""A throwaway HTTPS MCP server, so the gate can be exercised for real.

Real TLS, a real CA, a real proxy in front of it. The only thing pretended here
is the MCP server itself — everything between the client and this file is the
code that ships.
"""

import datetime
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def issue_certs(directory: Path, hostname: str = "localhost") -> tuple[Path, Path]:
    """Write a CA and a server certificate. Returns (ca.pem, server.pem)."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp-gateway test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = directory / "ca.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_path = directory / "server.pem"
    server_path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, server_path


class FakeMCP:
    """Records every request that actually reached it."""

    def __init__(self, server_pem: Path) -> None:
        self.seen: list[dict] = []
        self.paths: list[str] = []
        self.authorization: list[str | None] = []
        # Held by the SSE branch below until the test says the first event has
        # arrived. If anything in the chain buffers the response body, that
        # event never arrives and this never releases — which is the point.
        self.release = threading.Event()
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep pytest output readable
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or "{}")
                recorder.seen.append(body)
                recorder.paths.append(self.path)
                recorder.authorization.append(self.headers.get("Authorization"))

                if self.path != "/mcp":
                    # The canary: the suffixed path does not exist upstream, so
                    # anything that skipped the gate is answered with a 404.
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                tool = (body.get("params") or {}).get("name")
                if tool == "stream_me":
                    self.stream_sse()
                    return

                payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "result": {"content": [{"type": "text", "text": "did the thing"}]},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def stream_sse(self):
                """An event stream that pauses mid-flight, like MCP's does."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.chunk(b"data: one\n\n")
                recorder.release.wait(timeout=20)
                self.chunk(b"data: two\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def chunk(self, data: bytes) -> None:
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

        class Quiet(ThreadingHTTPServer):
            # Tests close connections mid-flight on purpose; the resulting
            # tracebacks would bury a real failure.
            def handle_error(self, request, client_address):
                pass

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server_pem)
        self._httpd = Quiet(("127.0.0.1", 0), Handler)
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> FakeMCP:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
