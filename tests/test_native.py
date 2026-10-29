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


@pytest.mark.skipif(platform.system() != "Darwin", reason="EventKit requires macOS")
def test_due_alarm_updates_preserve_user_alarms(tmp_path):
    source = Path(__file__).resolve().parents[1] / "host/Reminders.swift"
    library = tmp_path / "Reminders.swift"
    library.write_text(source.read_text().replace("@main\nstruct RemindersCLI", "struct RemindersCLI", 1))
    driver = tmp_path / "Driver.swift"
    driver.write_text('''
import Foundation
@preconcurrency import EventKit
@main struct Driver {
    static func main() throws {
        let store = EKEventStore()
        let item = EKReminder(eventStore: store)
        let first = try RemindersCLI.date("2024-10-17T09:00:00Z")
        let second = first.addingTimeInterval(3600)
        RemindersCLI.applyDue(nil, to: item)
        precondition((item.alarms ?? []).isEmpty)
        RemindersCLI.applyDue(first, to: item)
        precondition(item.alarms?.count == 1)
        precondition(RemindersCLI.dueAlarm(item) == first)
        RemindersCLI.applyDue(first, to: item)
        precondition(item.alarms?.count == 1)
        let other = first.addingTimeInterval(-600)
        item.addAlarm(EKAlarm(absoluteDate: other))
        item.addAlarm(EKAlarm(relativeOffset: -120))
        let location = EKAlarm()
        location.structuredLocation = EKStructuredLocation(title: "fixture")
        location.proximity = .enter
        item.addAlarm(location)
        let before = item.alarms!.count
        RemindersCLI.applyDue(nil, to: item)
        precondition(item.alarms!.count == before && RemindersCLI.dueAlarm(item) == first)
        RemindersCLI.applyDue(second, to: item)
        precondition(item.alarms!.count == before)
        precondition(RemindersCLI.dueAlarm(item) == second)
        precondition(!item.alarms!.contains { $0.absoluteDate == first })
        precondition(item.alarms!.contains { $0.absoluteDate == other })
        precondition(item.alarms!.contains { $0.absoluteDate == nil && $0.relativeOffset == -120 })
        precondition(item.alarms!.contains { $0.structuredLocation != nil && $0.proximity == .enter })
        RemindersCLI.applyDue(second.addingTimeInterval(0.123), to: item)
        RemindersCLI.applyDue(second.addingTimeInterval(0.123), to: item)
        precondition(item.alarms!.count == before)
        precondition(item.alarms!.filter { $0.absoluteDate == second }.count == 1)
        // Re-saving an older bridge reminder backfills its missing alarm.
        item.alarms = []
        RemindersCLI.applyDue(second, to: item)
        precondition(item.alarms!.count == 1 && RemindersCLI.dueAlarm(item) == second)
        print("due alarm cases passed")
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
    assert "due alarm cases passed" in result.stdout
