import Foundation

/// Talks to the Python engine (mb serve) on 127.0.0.1:4470, starting it if needed.
final class Engine {
    static let base = URL(string: "http://127.0.0.1:4470")!
    private var proc: Process?
    var repo: String {
        UserDefaults.standard.string(forKey: "repo") ?? NSHomeDirectory() + "/meeting-brain"
    }

    func ping(_ done: @escaping ([String: String]?) -> Void) {
        var r = URLRequest(url: Self.base.appendingPathComponent("api/ping"))
        r.timeoutInterval = 2
        URLSession.shared.dataTask(with: r) { d, _, _ in
            guard let d, let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return done(nil) }
            done(j["jobs"] as? [String: String] ?? [:])
        }.resume()
    }

    /// Low priority and 2 threads, so a long meeting doesn't spin the fans while you work.
    func start() {
        ping { jobs in
            guard jobs == nil, self.proc == nil else { return }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/zsh")
            p.arguments = ["-lc", "cd \"$MB_REPO\" && MB_THREADS=2 exec nice -n 10 uv run mb serve"]
            var env = ProcessInfo.processInfo.environment
            env["MB_REPO"] = self.repo
            p.environment = env
            let log = URL(fileURLWithPath: self.repo + "/data/app-engine.log")
            FileManager.default.createFile(atPath: log.path, contents: nil)
            if let h = try? FileHandle(forWritingTo: log) { p.standardOutput = h; p.standardError = h }
            try? p.run()
            self.proc = p
        }
    }

    func stop() { proc?.interrupt(); proc = nil }

    func sources(_ done: @escaping (Set<String>?) -> Void) {
        URLSession.shared.dataTask(with: Self.base.appendingPathComponent("api/sources")) { d, _, _ in
            guard let d, let a = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return done(nil) }
            done(Set(a.keys))
        }.resume()
    }

    func interrupted(_ done: @escaping (Set<String>) -> Void) {
        URLSession.shared.dataTask(with: Self.base.appendingPathComponent("api/interrupted")) { d, _, _ in
            done(Set((d.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String] }) ?? []))
        }.resume()
    }

    /// Streams the memo to the engine. The engine dedupes on `source`, so a resend is harmless.
    func send(_ m: Memo, done: @escaping (Int?) -> Void) {
        let boundary = "mb-\(UUID().uuidString)"
        let body = FileManager.default.temporaryDirectory.appendingPathComponent(boundary)
        FileManager.default.createFile(atPath: body.path, contents: nil)
        guard let out = try? FileHandle(forWritingTo: body), let input = try? FileHandle(forReadingFrom: m.url)
        else { return done(nil) }
        func field(_ k: String, _ v: String) {
            out.write("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(k)\"\r\n\r\n\(v)\r\n".data(using: .utf8)!)
        }
        field("title", m.title)
        field("source", m.source)
        field("recorded", String(m.date.timeIntervalSince1970))
        out.write(("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(m.url.lastPathComponent)\"\r\n" +
                   "Content-Type: application/octet-stream\r\n\r\n").data(using: .utf8)!)
        while case let chunk = input.readData(ofLength: 4 << 20), !chunk.isEmpty { out.write(chunk) }
        out.write("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        try? out.close(); try? input.close()

        var r = URLRequest(url: Self.base.appendingPathComponent("api/upload"))
        r.httpMethod = "POST"
        r.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        URLSession.shared.uploadTask(with: r, fromFile: body) { d, _, _ in
            try? FileManager.default.removeItem(at: body)
            let id = d.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }?["id"] as? Int
            done(id)
        }.resume()
    }
}
