import Foundation
import AppKit
import Darwin
@preconcurrency import EventKit

struct BridgeError: Error {
    let token: String
    let detail: String
}

struct Request: Decodable {
    let command: String
    var list: String?
    var title: String?
    var notes: String?
    var due: String?
    var uid: String?
    var include_completed: Bool?
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        Task {
            await RemindersCLI.run()
            NSApplication.shared.terminate(nil)
        }
    }
}

@main
struct RemindersCLI {
    static func argument(_ name: String) -> String? {
        guard let index = CommandLine.arguments.firstIndex(of: name), index + 1 < CommandLine.arguments.count else { return nil }
        return CommandLine.arguments[index + 1]
    }
    static func emit(_ value: [String: Any], failure: Bool = false) {
        do {
            var data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
            data.append(10)
            if let destination = argument("--response-file") {
                try data.write(to: URL(fileURLWithPath: destination), options: .atomic)
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination)
            } else {
                (failure ? FileHandle.standardError : FileHandle.standardOutput).write(data)
            }
        } catch {
            FileHandle.standardError.write(Data("{\"ok\":false,\"error\":\"serialization_error\",\"detail\":\"Could not encode result.\"}\n".utf8))
        }
    }

    static func failure(_ token: String, _ detail: String) -> BridgeError {
        BridgeError(token: token, detail: detail)
    }

    static func date(_ value: String) throws -> Date {
        guard value.hasSuffix("Z") || value.hasSuffix("+00:00") else {
            throw failure("invalid_due", "Use an ISO8601 UTC timestamp ending in Z or +00:00.")
        }
        let formatter = ISO8601DateFormatter()
        if let result = formatter.date(from: value) { return result }
        formatter.formatOptions.insert(.withFractionalSeconds)
        guard let result = formatter.date(from: value) else {
            throw failure("invalid_due", "Invalid ISO8601 UTC timestamp.")
        }
        return result
    }

    static func marker(_ uid: String) throws -> URL {
        guard !uid.isEmpty, uid.utf8.count <= 512,
              !uid.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              let encoded = uid.addingPercentEncoding(withAllowedCharacters: .alphanumerics),
              let url = URL(string: "nanoclaw-reminder://uid/" + encoded) else {
            throw failure("invalid_uid", "UID must be 1–512 bytes without control characters.")
        }
        return url
    }

    static func externalUID(_ item: EKReminder) -> String? {
        guard let url = item.url, url.scheme == "nanoclaw-reminder", url.host == "uid" else { return nil }
        return String(url.path.dropFirst())
    }

    // EventKit invokes this on its own queue. Construct it outside MainActor
    // isolation so Swift does not insert an inherited main-queue assertion.
    nonisolated static func identifiersCallback(
        _ continuation: CheckedContinuation<[String]?, Never>
    ) -> @Sendable ([EKReminder]?) -> Void {
        { items in
            continuation.resume(returning: items?.map { $0.calendarItemIdentifier })
        }
    }

    // The absolute, non-location alarm at the prior due time is bridge-owned.
    // All alarms at other times, relative alarms and location alarms survive.
    static func applyDue(_ due: Date?, to item: EKReminder) {
        guard let due else { return }
        let previousDue = item.dueDateComponents?.date
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = TimeZone(secondsFromGMT: 0)!
        let components = utc.dateComponents([.calendar, .timeZone, .year, .month, .day, .hour, .minute, .second], from: due)
        // Match the due date's existing second precision so fractional input
        // cannot leave an unmatchable old alarm on the next update.
        let alarmDate = components.date!
        if let previousDue {
            for alarm in item.alarms ?? [] where alarm.absoluteDate == previousDue && alarm.structuredLocation == nil {
                item.removeAlarm(alarm)
            }
        }
        item.dueDateComponents = components
        if !(item.alarms ?? []).contains(where: { $0.absoluteDate == alarmDate && $0.structuredLocation == nil }) {
            item.addAlarm(EKAlarm(absoluteDate: alarmDate))
        }
    }

    static func dueAlarm(_ item: EKReminder) -> Date? {
        guard let due = item.dueDateComponents?.date else { return nil }
        return item.alarms?.first(where: { $0.absoluteDate == due && $0.structuredLocation == nil })?.absoluteDate
    }

    static func record(_ item: EKReminder) -> [String: Any] {
        let formatter = ISO8601DateFormatter()
        return ["id": item.calendarItemIdentifier,
                "uid": externalUID(item) ?? item.calendarItemIdentifier,
                "managed": externalUID(item) != nil,
                "list_id": item.calendar.calendarIdentifier,
                "title": item.title ?? "", "notes": item.notes as Any? ?? NSNull(),
                "completed": item.isCompleted,
                "completed_at": item.completionDate.map(formatter.string) as Any? ?? NSNull(),
                "due": item.dueDateComponents?.date.map(formatter.string) as Any? ?? NSNull(),
                "alarm": dueAlarm(item).map(formatter.string) as Any? ?? NSNull()]
    }

    @MainActor
    static func perform(_ request: Request, store: EKEventStore) async throws -> [String: Any] {
        let calendars = store.calendars(for: .reminder)
        let defaultID = store.defaultCalendarForNewReminders()?.calendarIdentifier
        let lists: [[String: Any]] = calendars.map {
            ["id": $0.calendarIdentifier, "name": $0.title, "default": $0.calendarIdentifier == defaultID,
             "writable": $0.allowsContentModifications, "source": $0.source.title]
        }
        if request.command == "doctor" {
            guard !lists.isEmpty else { throw failure("no_reminders_lists", "No Reminders lists are available on this Mac.") }
            return ["backend": "mac-eventkit", "authorization": "full_access", "lists": lists,
                    "list_count": lists.count, "default_list_id": defaultID as Any? ?? NSNull(),
                    "sync_visibility": "Local EventKit store; remote-device sync must be checked on that device."]
        }
        if request.command == "list-lists" { return ["lists": lists] }
        guard ["list-todos", "add-todo", "complete-todo"].contains(request.command) else {
            throw failure("invalid_command", "Unsupported Reminders command.")
        }
        guard let requested = request.list, !requested.isEmpty,
              let calendar = calendars.first(where: {
                  $0.calendarIdentifier == (requested == "@default" ? defaultID : requested)
              }) else { throw failure("list_not_found", "Select a list ID from list-lists, or @default.") }
        if request.command != "list-todos", !calendar.allowsContentModifications {
            throw failure("list_read_only", "The selected list cannot be modified.")
        }
        let predicate = store.predicateForReminders(in: [calendar])
        // Transfer immutable identifiers out of EventKit's callback, then resolve
        // objects on our actor. EKReminder itself is not Sendable.
        let fetched: [String]? = await withCheckedContinuation { continuation in
            store.fetchReminders(matching: predicate, completion: identifiersCallback(continuation))
        }
        guard let identifiers = fetched else {
            throw failure("fetch_failed", "EventKit could not read reminders.")
        }
        let reminders = try identifiers.map { identifier -> EKReminder in
            guard let item = store.calendarItem(withIdentifier: identifier) as? EKReminder else {
                throw failure("store_changed", "A reminder changed during the read; retry the command.")
            }
            return item
        }
        if request.command == "list-todos" {
            return ["list_id": calendar.calendarIdentifier,
                    "todos": reminders.filter { request.include_completed == true || !$0.isCompleted }
                        .sorted { $0.calendarItemIdentifier < $1.calendarItemIdentifier }.map(record)]
        }
        let uid = request.uid ?? UUID().uuidString
        let target = try marker(uid)
        let matches = reminders.filter {
            $0.url == target || (request.command == "complete-todo" && $0.calendarItemIdentifier == uid)
        }
        guard matches.count <= 1 else {
            throw failure("duplicate_uid", "Multiple reminders have this UID; resolve the duplicate before updating.")
        }
        if request.command == "complete-todo" {
            guard request.uid != nil, let item = matches.first else {
                throw failure("todo_not_found", "No reminder with this UID exists in the selected list.")
            }
            if !item.isCompleted {
                item.isCompleted = true
                item.completionDate = Date()
                try store.save(item, commit: true)
            }
            return record(item)
        }
        guard let title = request.title, !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw failure("invalid_title", "A nonempty reminder title is required.")
        }
        // Parse before mutating: invalid input must not partially update a reminder.
        let due = try request.due.map(date)
        let item = matches.first ?? EKReminder(eventStore: store)
        item.calendar = calendar
        item.title = title
        if let notes = request.notes { item.notes = notes }
        item.url = target
        applyDue(due, to: item)
        try store.save(item, commit: true)
        var result = record(item)
        result["created"] = matches.isEmpty
        return result
    }

    @MainActor
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = AppDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }

    @MainActor
    static func run() async {
        do {
            if !CommandLine.arguments.contains("--authorize") { alarm(50) }
            let lockFD = argument("--lock-file").map { Darwin.open($0, O_CREAT | O_RDWR, 0o600) }
            if let lockFD {
                guard lockFD >= 0, flock(lockFD, LOCK_EX) == 0 else {
                    throw failure("lock_failed", "Could not serialize access to the Reminders store.")
                }
            }
            defer { if let lockFD { flock(lockFD, LOCK_UN); Darwin.close(lockFD) } }
            let app = NSApplication.shared
            app.setActivationPolicy(.accessory)
            let store = EKEventStore()
            if CommandLine.arguments.contains("--authorize") {
                app.activate(ignoringOtherApps: true)
                guard try await store.requestFullAccessToReminders() else {
                    throw failure("permission_denied", "Allow NanoClaw Reminders in System Settings > Privacy & Security > Reminders.")
                }
                emit(["ok": true, "command": "authorize", "data": ["authorized": true]])
                return
            }
            guard EKEventStore.authorizationStatus(for: .reminder) == .fullAccess else {
                throw failure("permission_denied", "Run the host installer with --authorize and grant Reminders access.")
            }
            let data = try argument("--request-file").map { try Data(contentsOf: URL(fileURLWithPath: $0)) }
                ?? FileHandle.standardInput.readDataToEndOfFile()
            guard data.count <= 65536 else { throw failure("invalid_request", "Request is too large.") }
            let request = try JSONDecoder().decode(Request.self, from: data)
            let result = try await perform(request, store: store)
            emit(["ok": true, "command": request.command, "data": result])
        } catch let error as BridgeError {
            emit(["ok": false, "error": error.token, "detail": error.detail], failure: true)
            exit(1)
        } catch is DecodingError {
            emit(["ok": false, "error": "invalid_request", "detail": "Invalid command JSON."], failure: true)
            exit(1)
        } catch {
            // Do not relay raw framework errors, which may contain reminder content.
            emit(["ok": false, "error": "eventkit_error", "detail": "EventKit operation failed; check Reminders access and account sync on the Mac."], failure: true)
            exit(1)
        }
    }
}
