#!/usr/bin/env python3
"""Linux-compatible JSON CLI for the authenticated NanoClaw Mac Reminders bridge."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

DEFAULT_URL = "http://host.docker.internal:10304"
MAX_RESPONSE = 8 * 1024 * 1024


class RemindersError(Exception):
    def __init__(self, token: str, detail: str):
        super().__init__(detail)
        self.token = token
        self.detail = detail


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RemindersError("invalid_arguments", message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RemindersError("unexpected_redirect", "The local bridge must not redirect requests.")


def validate_request(request: dict[str, Any]) -> None:
    command = request.get("command")
    commands = {"doctor", "list-lists", "list-todos", "add-todo", "complete-todo"}
    if command not in commands:
        raise RemindersError("invalid_command", "Unsupported Reminders command.")
    allowed = {"command", "list", "title", "notes", "due", "uid", "include_completed"}
    if set(request) - allowed:
        raise RemindersError("invalid_arguments", "Unknown request field.")
    for key in allowed - {"include_completed"}:
        if key in request and (not isinstance(request[key], str) or len(request[key].encode()) > 16000):
            raise RemindersError("invalid_arguments", "Text fields must be strings of at most 16000 bytes.")
    if "include_completed" in request and not isinstance(request["include_completed"], bool):
        raise RemindersError("invalid_arguments", "include_completed must be boolean.")
    if command in {"list-todos", "add-todo", "complete-todo"} and not request.get("list"):
        raise RemindersError("invalid_list", "Select a list ID or @default.")
    if command == "add-todo" and not request.get("title", "").strip():
        raise RemindersError("invalid_title", "A nonempty title is required.")
    if command == "complete-todo" and not request.get("uid"):
        raise RemindersError("invalid_uid", "A reminder UID is required.")
    if "uid" in request:
        uid = request["uid"]
        if not uid or len(uid.encode()) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in uid):
            raise RemindersError("invalid_uid", "UID must be 1–512 bytes without control characters.")
    if "due" in request:
        try:
            due = datetime.fromisoformat(request["due"].replace("Z", "+00:00"))
            valid = "T" in request["due"] and due.tzinfo is not None and due.utcoffset() == timedelta(0)
        except ValueError:
            valid = False
        if not valid:
            raise RemindersError("invalid_due", "Use an ISO8601 UTC timestamp with Z or +00:00.")


def invoke(request: dict[str, Any]) -> dict[str, Any]:
    validate_request(request)
    base = os.environ.get("APPLE_REMINDERS_BRIDGE_URL", DEFAULT_URL).rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"}
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise RemindersError("invalid_bridge_url", "Use the local HTTP bridge origin on localhost or host.docker.internal.")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = os.environ.get("APPLE_REMINDERS_BRIDGE_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    # Agent requests use OneCLI HTTP_PROXY to inject the bearer. Host tests may
    # supply the token in their process environment. Never accept token CLI args.
    http_request = urllib.request.Request(base + "/v1/reminders/command",
                                          data=json.dumps(request).encode(), headers=headers, method="POST")
    # NanoClaw excludes host.docker.internal from NO_PROXY for its Claude
    # credential proxy. This one bridge request needs OneCLI injection instead.
    # Explicitly route only this request; never change global NO_PROXY settings.
    proxy = urllib.request.getproxies().get("http") if not token else None
    if proxy:
        proxy_url = urllib.parse.urlsplit(proxy)
        if proxy_url.scheme != "http" or not proxy_url.hostname:
            raise RemindersError("invalid_proxy", "The bridge requires an HTTP OneCLI proxy origin.")
        authority = proxy_url.hostname + (":" + str(proxy_url.port) if proxy_url.port else "")
        http_request.set_proxy(authority, "http")
        if proxy_url.username is not None:
            credentials = urllib.parse.unquote(proxy_url.username) + ":" + urllib.parse.unquote(proxy_url.password or "")
            value = base64.b64encode(credentials.encode()).decode()
            http_request.add_header("Proxy-Authorization", "Basic " + value)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(http_request, timeout=70) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RemindersError("bridge_auth_failed", "The bridge credential is missing or invalid; check the main-agent OneCLI grant.") from None
        raw = exc.read(MAX_RESPONSE + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RemindersError("bridge_unavailable", "Cannot reach the Mac Reminders bridge; check its LaunchAgent and proxy.") from None
    if len(raw) > MAX_RESPONSE:
        raise RemindersError("response_too_large", "Bridge result exceeds the response limit.")
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RemindersError("invalid_response", "Bridge returned invalid JSON.") from None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
        raise RemindersError("invalid_response", "Bridge returned an invalid envelope.")
    if not envelope["ok"]:
        raise RemindersError(str(envelope.get("error", "bridge_error")), str(envelope.get("detail", "Bridge operation failed.")))
    if envelope.get("command") != request["command"] or "data" not in envelope:
        raise RemindersError("invalid_response", "Bridge returned a mismatched command.")
    return envelope


def parser() -> Parser:
    result = Parser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("list-lists")
    listing = commands.add_parser("list-todos")
    listing.add_argument("--list", required=True)
    listing.add_argument("--include-completed", action="store_true")
    add = commands.add_parser("add-todo")
    add.add_argument("--list", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--notes")
    add.add_argument("--due")
    add.add_argument("--uid")
    complete = commands.add_parser("complete-todo")
    complete.add_argument("--list", required=True)
    complete.add_argument("--uid", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        request = {k: v for k, v in vars(parser().parse_args(argv)).items() if v is not None}
        print(json.dumps(invoke(request), ensure_ascii=False))
        return 0
    except RemindersError as exc:
        print(json.dumps({"ok": False, "error": exc.token, "detail": exc.detail}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
