#!/usr/bin/env python3
"""Build the Mac EventKit helper and install its authenticated loopback bridge."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import secrets
import json
import shutil
import subprocess
import tempfile

LABEL = "io.gamov.nanoclaw.reminders"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--runtime-dir", type=Path, help="Private runtime location; defaults to NanoClaw data or ~/.local/share/nanoclaw.")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    candidate = source.parents[2]
    root = args.runtime_dir or (candidate if (candidate / "src/container-runner.ts").exists()
                                else Path.home() / ".local/share/nanoclaw")
    root = root.expanduser().resolve()
    app = root / "data/macos/NanoClaw Reminders.app"
    binary = app / "Contents/MacOS/Reminders"
    binary.parent.mkdir(parents=True, exist_ok=True)
    info = {"CFBundleIdentifier": LABEL, "CFBundleName": "NanoClaw Reminders",
            "CFBundleDisplayName": "NanoClaw Reminders", "CFBundleExecutable": "Reminders",
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "CFBundleShortVersionString": "0.1.0",
            "LSUIElement": True, "LSMinimumSystemVersion": "14.0",
            "NSRemindersFullAccessUsageDescription": "NanoClaw reads your reminder lists and creates or completes reminders you request."}
    info_path = app / "Contents/Info.plist"
    old = plistlib.loads(info_path.read_bytes()) if info_path.exists() else {}
    if not binary.exists() or binary.stat().st_mtime < (source / "Reminders.swift").stat().st_mtime or old != info:
        info_path.write_bytes(plistlib.dumps(info))
        architecture = os.uname().machine
        subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-swift-version", "6",
                        "-target", f"{architecture}-apple-macosx14.0", str(source / "Reminders.swift"),
                        "-o", str(binary), "-framework", "EventKit", "-framework", "AppKit"], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", LABEL, str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", str(app)], check=True)
    private = Path.home() / ".config/nanoclaw/apple-reminders"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.chmod(0o700)
    token = private / "bridge-token"
    if not token.exists():
        fd = os.open(token, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    if args.authorize:
        with tempfile.TemporaryDirectory(prefix="authorize-", dir=private) as temporary:
            output = Path(temporary) / "result.json"
            subprocess.run(["open", "-W", "-n", str(app), "--args", "--authorize", "--response-file", str(output)], check=True)
            result = json.loads(output.read_text())
            if not result.get("ok"):
                raise SystemExit(result.get("detail", "Reminders permission was not granted."))
    if args.start:
        # Confirm access before installing a permanently failing service.
        from bridge import native
        result = native(binary, {"command": "doctor"}, private / "bridge-token.lock")
        if not result.get("ok"):
            raise SystemExit(result.get("detail", "Reminders access check failed."))
        python = shutil.which("python3")
        if python is None:
            raise SystemExit("Python 3.11+ is required.")
        plist = {"Label": LABEL, "ProgramArguments": [python, str(source / "bridge.py"),
                 "--binary", str(binary), "--token-file", str(token)],
                 "AssociatedBundleIdentifiers": [LABEL], "WorkingDirectory": str(root),
                 "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
                 "Umask": 0o077, "StandardOutPath": str(private / "bridge.log"),
                 "StandardErrorPath": str(private / "bridge.error.log")}
        destination = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        destination.parent.mkdir(parents=True, exist_ok=True)
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        if loaded.returncode == 0:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True)
        destination.write_bytes(plistlib.dumps(plist))
        destination.chmod(0o600)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)], check=True)
    print("Native helper installed." + (" Loopback bridge started on port 10304." if args.start else ""))


if __name__ == "__main__":
    main()
