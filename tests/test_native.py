"""Regression for EventKit calling a callback outside the main actor."""
import platform
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(platform.system() != "Darwin", reason="EventKit requires macOS")
def test_eventkit_callback_on_background_queue(tmp_path):
    source = Path(__file__).resolve().parents[1] / "host/Reminders.swift"
    library = tmp_path / "Reminders.swift"
    # Keep the real implementation; replace only its executable entry marker.
    library.write_text(source.read_text().replace("@main\nstruct RemindersCLI", "struct RemindersCLI", 1))
    driver = tmp_path / "Driver.swift"
    driver.write_text('''
import Foundation
@preconcurrency import EventKit
@main struct Driver {
    static func main() async {
        let result: [String]? = await withCheckedContinuation { continuation in
            let callback = RemindersCLI.identifiersCallback(continuation)
            DispatchQueue.global().async {
                let reminder = EKReminder(eventStore: EKEventStore())
                callback([reminder])
            }
        }
        guard result?.count == 1 else { exit(1) }
        print("background callback passed")
    }
}
''')
    binary = tmp_path / "test-native"
    subprocess.run(["xcrun", "swiftc", "-module-cache-path", str(tmp_path / "cache"),
                    "-parse-as-library", "-swift-version", "6", str(library), str(driver),
                    "-framework", "EventKit", "-framework", "AppKit", "-o", str(binary)],
                   capture_output=True, text=True, check=True, timeout=120)
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "background callback passed" in result.stdout
