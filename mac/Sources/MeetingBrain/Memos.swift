import AVFoundation
import Foundation
import SQLite3

/// One Voice Memos recording on disk.
struct Memo {
    let url: URL
    let title: String          // Voice Memos' own title, "" if it's a default "New Recording"
    let date: Date
    let duration: Double
    var source: String { "voicememos:" + url.lastPathComponent }
}

enum Memos {
    static var dir: URL {
        if let o = ProcessInfo.processInfo.environment["MB_MEMOS_DIR"] { return URL(fileURLWithPath: o) }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings")
    }

    /// nil means macOS blocked us (Full Disk Access not granted yet).
    static func list() -> [Memo]? {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys:
                [.contentModificationDateKey, .creationDateKey], options: [.skipsHiddenFiles]) else { return nil }
        let meta = titles()
        return files.filter { ["m4a", "qta"].contains($0.pathExtension.lowercased()) }.map { u in
            let v = try? u.resourceValues(forKeys: [.creationDateKey, .contentModificationDateKey])
            let m = meta[u.lastPathComponent]
            let dur = m?.duration ?? CMTimeGetSeconds(AVURLAsset(url: u).duration)
            return Memo(url: u, title: cleanTitle(m?.title ?? ""), date: m?.date ?? v?.creationDate ?? .distantPast,
                        duration: dur.isFinite ? dur : 0)
        }.sorted { $0.date > $1.date }
    }

    static func modified(_ m: Memo) -> Date {
        (try? m.url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast
    }

    /// Default names carry no information, so let the engine title those from the content.
    static func cleanTitle(_ t: String) -> String {
        let s = t.trimmingCharacters(in: .whitespaces)
        if s.isEmpty || s.hasPrefix("New Recording") || s.hasPrefix("新錄音") || s.hasPrefix("新录音") { return "" }
        // untitled memos can carry a timestamp like 2026-11-05T19:17:28Z as their label
        if s.range(of: #"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"#, options: .regularExpression) != nil { return "" }
        return s
    }

    /// Titles and dates from Voice Memos' own database, read-only.
    static func titles() -> [String: (title: String, date: Date, duration: Double)] {
        var out: [String: (String, Date, Double)] = [:]
        let path = dir.appendingPathComponent("CloudRecordings.db").path
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { return [:] }
        defer { sqlite3_close(db) }
        var cols = Set<String>()
        var st: OpaquePointer?
        if sqlite3_prepare_v2(db, "PRAGMA table_info(ZCLOUDRECORDING)", -1, &st, nil) == SQLITE_OK {
            while sqlite3_step(st) == SQLITE_ROW { cols.insert(String(cString: sqlite3_column_text(st, 1))) }
        }
        sqlite3_finalize(st)
        guard cols.contains("ZPATH") else { return [:] }
        let title = cols.contains("ZCUSTOMLABEL") ? "COALESCE(NULLIF(ZENCRYPTEDTITLE, ''), ZCUSTOMLABEL, '')"
                  : cols.contains("ZENCRYPTEDTITLE") ? "COALESCE(ZENCRYPTEDTITLE, '')" : "''"
        let sql = "SELECT ZPATH, \(title), COALESCE(ZDATE, 0), COALESCE(ZDURATION, 0) FROM ZCLOUDRECORDING"
        if sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK {
            while sqlite3_step(st) == SQLITE_ROW {
                guard let p = sqlite3_column_text(st, 0) else { continue }
                let name = (String(cString: p) as NSString).lastPathComponent
                let t = sqlite3_column_text(st, 1).map { String(cString: $0) } ?? ""
                out[name] = (t, Date(timeIntervalSinceReferenceDate: sqlite3_column_double(st, 2)),
                             sqlite3_column_double(st, 3))
            }
        }
        sqlite3_finalize(st)
        return out
    }
}
