#!/usr/bin/env python3
"""Authenticated loopback HTTP adapter; all Reminders access stays on the Mac."""
from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/apple-reminders/scripts"))
from apple_reminders import RemindersError, validate_request


def error(token: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": token, "detail": detail}


def load_token(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ValueError("Bridge token must be an owner-owned regular file with mode 0600.")
    token = path.read_text().strip()
    if len(token) < 32 or not token.isascii() or not token.isalnum():
        raise ValueError("Invalid bridge token format.")
    return token


def native(binary: Path, request: dict[str, Any], lock_path: Path) -> dict[str, Any]:
    # Serialize the read/lookup/save transaction, including across host CLI and
    # HTTP callers. The UID marker lives in EventKit, surviving response loss.
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            # LaunchServices preserves the dedicated app's TCC identity; invoking
            # a binary from Python can attribute Reminders permission to Python.
            with tempfile.TemporaryDirectory(prefix="request-", dir=lock_path.parent) as temporary:
                request_path = Path(temporary) / "request.json"
                response_path = Path(temporary) / "response.json"
                request_path.write_text(json.dumps(request))
                request_path.chmod(0o600)
                subprocess.run(["open", "-W", "-n", str(binary.parents[2]), "--args",
                                "--request-file", str(request_path), "--response-file", str(response_path),
                                "--lock-file", str(lock_path.with_name("native.lock"))],
                               text=True, capture_output=True, timeout=55, check=True)
                raw = response_path.read_text()
        except subprocess.TimeoutExpired:
            return error("eventkit_timeout", "EventKit did not respond in 55 seconds; retry with the same UID.")
        except FileNotFoundError:
            return error("helper_failed", "Native helper did not produce a response; check its installation and macOS permission.")
        except OSError:
            return error("helper_unavailable", "The native Reminders helper is not installed.")
        except subprocess.CalledProcessError:
            return error("helper_failed", "macOS could not launch the Reminders helper.")
    try:
        response = json.loads(raw)
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ValueError("Invalid envelope")
        if response["ok"] and (response.get("command") != request["command"] or "data" not in response):
            raise ValueError("Mismatched command")
        return response
    except (ValueError, UnicodeError):
        return error("helper_failed", "Native Reminders helper returned an invalid result.")


def handler(token: str, binary: Path, lock_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format: str, *args: Any) -> None:
            # Intentionally omit HTTP access logs: requests contain private reminders.
            return

        def respond(self, status: int, envelope: dict[str, Any]) -> None:
            raw = json.dumps(envelope).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                self.respond(401, error("bridge_auth_failed", "Authentication required."))
                return
            if self.path != "/v1/reminders/command" or self.headers.get("Origin"):
                self.respond(403, error("request_rejected", "Only authenticated local CLI requests are accepted."))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid length")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError("Not an object")
                validate_request(request)
            except RemindersError as exc:
                self.respond(400, error(exc.token, exc.detail))
                return
            except (ValueError, UnicodeError, TimeoutError):
                self.respond(400, error("invalid_request", "Expected a bounded JSON command object."))
                return
            response = native(binary, request, lock_path)
            self.respond(200 if response["ok"] else 422, response)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=10304)
    args = parser.parse_args()
    token = load_token(args.token_file)
    server = HTTPServer(("127.0.0.1", args.port), handler(token, args.binary, args.token_file.with_suffix(".lock")))
    server.serve_forever()


if __name__ == "__main__":
    main()
