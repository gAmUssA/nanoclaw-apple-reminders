import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/apple-reminders/scripts"))
import apple_reminders as client

spec = importlib.util.spec_from_file_location("bridge", ROOT / "host/bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture
def server(monkeypatch, tmp_path):
    records = {}
    calls = []

    def native(binary, request, lock):
        calls.append(request)
        command = request["command"]
        if command in {"doctor", "list-lists"}:
            data = {"lists": [{"id": "list1", "name": "Reminders", "default": True}]}
        elif command == "add-todo":
            uid = request["uid"]
            created = uid not in records
            records[uid] = {"uid": uid, "title": request["title"], "completed": False}
            data = {**records[uid], "created": created}
        elif command == "complete-todo":
            records[request["uid"]]["completed"] = True
            data = records[request["uid"]]
        else:
            data = {"todos": [v for v in records.values() if request.get("include_completed") or not v["completed"]]}
        return {"ok": True, "command": command, "data": data}

    monkeypatch.setattr(bridge, "native", native)
    token = "a" * 64
    http = bridge.HTTPServer(("127.0.0.1", 0), bridge.handler(token, Path("/fake/App/Contents/MacOS/Reminders"), tmp_path / "lock"))
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("APPLE_REMINDERS_BRIDGE_URL", f"http://127.0.0.1:{http.server_port}")
    monkeypatch.setenv("APPLE_REMINDERS_BRIDGE_TOKEN", token)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    yield calls
    http.shutdown()
    http.server_close()
    thread.join()


def test_list_and_doctor_envelopes(server):
    for command in ("doctor", "list-lists"):
        result = client.invoke({"command": command})
        assert result["ok"] is True
        assert result["command"] == command
        assert result["data"]["lists"][0]["default"] is True


def test_stable_uid_passes_through_retry_and_completion(server):
    request = {"command": "add-todo", "list": "list1", "title": "Test", "uid": "scheduler-123"}
    assert client.invoke(request)["data"]["created"] is True
    assert client.invoke({**request, "title": "Updated"})["data"]["created"] is False
    todos = client.invoke({"command": "list-todos", "list": "list1"})["data"]["todos"]
    assert len(todos) == 1 and todos[0]["title"] == "Updated"
    assert client.invoke({"command": "complete-todo", "list": "list1", "uid": "scheduler-123"})["data"]["completed"]
    assert not client.invoke({"command": "list-todos", "list": "list1"})["data"]["todos"]
    assert len(client.invoke({"command": "list-todos", "list": "list1", "include_completed": True})["data"]["todos"]) == 1


def test_auth_failure_never_calls_native(server, monkeypatch):
    monkeypatch.setenv("APPLE_REMINDERS_BRIDGE_TOKEN", "wrong")
    with pytest.raises(client.RemindersError) as error:
        client.invoke({"command": "doctor"})
    assert error.value.token == "bridge_auth_failed"
    assert not server


def test_permission_error_surfaces(server, monkeypatch):
    monkeypatch.setattr(bridge, "native", lambda *a: bridge.error("permission_denied", "Grant Reminders permission."))
    with pytest.raises(client.RemindersError) as error:
        client.invoke({"command": "doctor"})
    assert error.value.token == "permission_denied"


@pytest.mark.parametrize("due", ["tomorrow", "2026-10-17", "2026-10-17T09:00:00", "2026-10-17T09:00:00-04:00"])
def test_rejects_non_utc_due_before_network(due):
    with pytest.raises(client.RemindersError, match="ISO8601 UTC"):
        client.validate_request({"command": "add-todo", "list": "l", "title": "t", "due": due})


@pytest.mark.parametrize("due", ["2026-10-17T09:00:00Z", "2026-10-17T09:00:00.123+00:00"])
def test_accepts_utc_due(due):
    client.validate_request({"command": "add-todo", "list": "l", "title": "t", "due": due})


def test_json_error_contract(capsys):
    assert client.main(["complete-todo", "--list", "l"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "invalid_arguments"


def test_json_success_contract(server, capsys):
    assert client.main(["list-lists"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["ok"] is True
    assert len(captured.out.splitlines()) == 1


@pytest.mark.parametrize("url", ["http://evil.example", "http://user:password@localhost:10304", "http://localhost:10304/private"])
def test_rejects_remote_or_credential_url(monkeypatch, url):
    monkeypatch.setenv("APPLE_REMINDERS_BRIDGE_URL", url)
    with pytest.raises(client.RemindersError) as error:
        client.invoke({"command": "doctor"})
    assert error.value.token == "invalid_bridge_url"


def test_redirect_does_not_forward_token():
    with pytest.raises(client.RemindersError) as error:
        client.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example")
    assert error.value.token == "unexpected_redirect"


def test_token_permissions(tmp_path):
    path = tmp_path / "token"
    path.write_text("a" * 64)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        bridge.load_token(path)
    path.chmod(0o600)
    assert len(bridge.load_token(path)) == 64


def test_native_timeout_has_no_private_output(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("private command", 55, output="private reminder")
    monkeypatch.setattr(bridge.subprocess, "run", fail)
    result = bridge.native(Path("/fake/App/Contents/MacOS/Reminders"), {"command": "doctor"}, tmp_path / "lock")
    assert result["error"] == "eventkit_timeout"
    assert "private" not in json.dumps(result)


def test_origin_rejected(server, monkeypatch):
    import os
    request = urllib.request.Request(os.environ["APPLE_REMINDERS_BRIDGE_URL"] + "/v1/reminders/command",
                                     data=b'{"command":"doctor"}',
                                     headers={"Authorization": "Bearer " + "a" * 64, "Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 403
    assert not server


def test_gateway_route_overrides_only_this_hosts_no_proxy(monkeypatch):
    monkeypatch.delenv("APPLE_REMINDERS_BRIDGE_TOKEN", raising=False)
    monkeypatch.setenv("APPLE_REMINDERS_BRIDGE_URL", client.DEFAULT_URL)
    monkeypatch.setenv("NO_PROXY", "host.docker.internal,localhost")
    monkeypatch.setattr(client.urllib.request, "getproxies", lambda: {"http": "http://agent:fake-key@proxy.test:10255"})
    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            return io.BytesIO(b'{"ok":true,"command":"doctor","data":{}}')

    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *handlers: Opener())
    assert client.invoke({"command": "doctor"})["ok"]
    assert seen[0].host == "proxy.test:10255"
    assert seen[0].selector == client.DEFAULT_URL + "/v1/reminders/command"
    assert seen[0].get_header("Proxy-authorization").startswith("Basic ")
    import os
    assert os.environ["NO_PROXY"] == "host.docker.internal,localhost"
