#!/usr/bin/env python3
"""Vault the local bridge token for explicitly selected OneCLI agents only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import urllib.request

from bridge import load_token


def cli(*args: str):
    result = subprocess.run(["onecli", *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("OneCLI grant operation failed; credential remains inactive.")
    data = json.loads(result.stdout)
    return data.get("data", data) if isinstance(data, dict) else data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("OneCLI API unexpectedly redirected the request.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-env", type=Path, required=True)
    parser.add_argument("--agent-id", action="append", required=True)
    args = parser.parse_args()
    private = Path.home() / ".config/nanoclaw/apple-reminders"
    token = load_token(private / "bridge-token")
    api_key = None
    for line in args.project_env.read_text().splitlines():
        if line.startswith("ONECLI_API_KEY="):
            values = shlex.split(line.partition("=")[2])
            api_key = values[0] if values else None
    if not api_key:
        raise RuntimeError("The project environment has no OneCLI API key.")

    def api(method: str, path: str, body: dict):
        request = urllib.request.Request("http://127.0.0.1:10256/v1" + path,
                                         data=json.dumps(body).encode(), method=method,
                                         headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            return json.load(response)

    allowed = set(args.agent_id)
    agents = {agent["id"] for agent in cli("agents", "list", "--fields", "id")}
    if not allowed or not allowed <= agents:
        raise RuntimeError("Selected OneCLI agents are missing.")
    state_path = private / "onecli.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    secret_id = state.get("secret_id")
    inert = "inactive-" + secrets.token_hex(32)
    scope = {"hostPattern": "apple-reminders-onboarding.invalid", "pathPattern": "/v1/reminders/command",
             "injectionConfig": {"headerName": "Authorization", "valueFormat": "Bearer {value}"}}
    if secret_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", secret_id):
            raise RuntimeError("Invalid saved OneCLI credential identifier.")
        api("PATCH", "/secrets/" + secret_id, {**scope, "value": inert})
    else:
        response = api("POST", "/secrets", {**scope, "name": "NanoClaw Apple Reminders bridge", "type": "generic", "value": inert})
        secret_id = response.get("id") or response.get("secret", {}).get("id")
        if not isinstance(secret_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", secret_id):
            raise RuntimeError("OneCLI returned no credential identifier.")
        fd = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"secret_id": secret_id}, output)
    # OneCLI initially attaches new generic secrets broadly. The staged value is
    # inert and its hostname unreachable until every grant is constrained.
    for agent in agents:
        grants = cli("agents", "grants", "list", "--id", agent, "--fields", "secrets")
        attached = any(s.get("secretId") == secret_id for s in grants.get("secrets", []))
        if agent not in allowed and attached:
            cli("agents", "grants", "detach-secret", "--id", agent, "--secret-id", secret_id)
        elif agent in allowed and not attached:
            cli("agents", "grants", "attach-secret", "--id", agent, "--secret-id", secret_id)
    if {a["id"] for a in cli("agents", "list", "--fields", "id")} != agents:
        raise RuntimeError("Agent inventory changed during setup; credential remains inactive.")
    for agent in agents:
        grants = cli("agents", "grants", "list", "--id", agent, "--fields", "secrets")
        attached = any(s.get("secretId") == secret_id for s in grants.get("secrets", []))
        if attached != (agent in allowed):
            raise RuntimeError("Grant verification failed; credential remains inactive.")
    api("PATCH", "/secrets/" + secret_id, {**scope, "hostPattern": "host.docker.internal", "value": token})
    print(json.dumps({"ok": True, "authorized_agents": len(allowed), "credential_stored": True}))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        print('{"ok":false,"error":"onecli_setup_failed","detail":"Setup failed; check local API access and agent selection. Credential values were not printed."}', file=sys.stderr)
        sys.exit(1)
