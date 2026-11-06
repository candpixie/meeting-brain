import AppKit
import ServiceManagement
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKDownloadDelegate {
    let engine = Engine()
    var item: NSStatusItem!
    var window: NSWindow?
    var web: WKWebView?
    var jobs: [String: String]? = nil       // nil = engine not reachable
    var done: Set<String> = []              // sources the engine already has
    var blocked = false                     // Voice Memos folder unreadable (no Full Disk Access)
    var sending = false
    let d = UserDefaults.standard

    var auto: Bool { d.object(forKey: "auto") as? Bool ?? true }
    var minMinutes: Double { d.object(forKey: "minMinutes") as? Double ?? 5 }
    var sent: Set<String> { Set(d.stringArray(forKey: "sent") ?? []) }

    func applicationDidFinishLaunching(_ n: Notification) {
        // Only memos recorded after the first launch are auto-processed. Older ones are one click away in the menu.
        if d.object(forKey: "watchSince") == nil { d.set(Date().timeIntervalSince1970, forKey: "watchSince") }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        setIcon(busy: false)
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        engine.start()
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.tick() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { self.tick() }
        // Opened by hand: show the dashboard. Started at login: stay quietly in the menu bar.
        let ev = NSAppleEventManager.shared().currentAppleEvent
        let atLogin = ev?.eventID == kAEOpenApplication &&
            ev?.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
        if !atLogin { openWhenReady() }   // MB_OPEN=#/memos opens a specific page (testing)
    }

    /// Clicking the app again (Finder, Spotlight, Dock) while it's running opens the dashboard.
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        openWhenReady()
        return true
    }

    /// Wait for the engine to answer so the window never shows a "can't connect" page.
    func openWhenReady(tries: Int = 0) {
        engine.ping { jobs in
            DispatchQueue.main.async {
                if jobs != nil || tries > 60 { self.openDashboard(path: ProcessInfo.processInfo.environment["MB_OPEN"] ?? "") }
                else { DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.openWhenReady(tries: tries + 1) } }
            }
        }
    }

    func applicationWillTerminate(_ n: Notification) { engine.stop() }

    func setIcon(busy: Bool) {
        let name = busy ? "waveform.badge.ellipsis" : "waveform"
        item.button?.image = NSImage(systemSymbolName: name, accessibilityDescription: "Meeting Brain")
    }

    // MARK: watching

    func tick() {
        engine.ping { jobs in
            self.engine.sources { src in
                DispatchQueue.main.async {
                    self.jobs = jobs
                    if let src { self.done = src }
                    self.setIcon(busy: !(jobs ?? [:]).filter { $0.value == "running" || $0.value == "queued" }.isEmpty)
                    if jobs == nil { self.engine.start(); return }
                    self.engine.interrupted { lost in
                        DispatchQueue.main.async { self.autoSend(redo: lost) }
                    }
                }
            }
        }
    }

    func autoSend(redo: Set<String> = []) {
        guard let memos = Memos.list() else { blocked = true; return }
        blocked = false
        guard !sending else { return }
        // the engine stopped mid-way on these (quit, crash, reboot): always finish them
        let lost = memos.filter { redo.contains($0.source) }
        if !lost.isEmpty { NSLog("redoing interrupted %@", lost.map(\.source).joined(separator: ",")); return sendAll(lost) }
        guard auto else { return }
        let since = Date(timeIntervalSince1970: d.double(forKey: "watchSince"))
        let todo = memos.filter {
            $0.date > since && $0.duration >= minMinutes * 60 && !done.contains($0.source) && !sent.contains($0.source)
                && Memos.modified($0) < Date().addingTimeInterval(-90)   // still recording if it changed recently
        }
        if !todo.isEmpty { NSLog("auto-send %@ (auto=%d min=%.0f)", todo.map(\.source).joined(separator: ","), auto ? 1 : 0, minMinutes) }
        sendAll(todo.reversed())
    }

    func sendAll(_ memos: [Memo], open: Bool = false) {
        guard let m = memos.first else { sending = false; return }
        sending = true
        engine.send(m) { id in
            DispatchQueue.main.async {
                if id != nil { self.d.set(Array(self.sent.union([m.source])), forKey: "sent"); self.done.insert(m.source) }
                if open, let id { self.openDashboard(path: "#/m/\(id)") }
                self.setIcon(busy: true)
                self.sendAll(Array(memos.dropFirst()))
            }
        }
    }

    // MARK: menu

    func menuWillOpen(_ menu: NSMenu) {
        menu.removeAllItems()
        let running = (jobs ?? [:]).values.filter { $0 == "running" || $0 == "queued" }.count
        let status = jobs == nil ? "Engine starting…" : running > 0 ? "Processing \(running) recording\(running == 1 ? "" : "s")" : "Engine ready"
        menu.addItem(withTitle: status, action: nil, keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(action("Open Dashboard", #selector(openDash), "d"))

        let recent = NSMenuItem(title: "Voice Memos", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        if let memos = Memos.list() {
            blocked = false
            let fmt = DateFormatter()
            fmt.dateFormat = "MMM d, HH:mm"
            for m in memos.prefix(15) {
                let mins = Int((m.duration / 60).rounded())
                let name = m.title.isEmpty ? "Untitled" : m.title
                let it = action("\(name)  ·  \(fmt.string(from: m.date))  ·  \(mins) min", #selector(processMemo(_:)), "")
                it.representedObject = m.url.path
                it.state = done.contains(m.source) ? .on : .off
                sub.addItem(it)
            }
            if memos.isEmpty { sub.addItem(withTitle: "No recordings yet", action: nil, keyEquivalent: "") }
            sub.addItem(.separator())
            sub.addItem(withTitle: "Click one to process it (✓ = already in your brain)", action: nil, keyEquivalent: "")
        } else {
            blocked = true
            sub.addItem(action("Grant Full Disk Access…", #selector(openPrivacy), ""))
        }
        recent.submenu = sub
        menu.addItem(recent)

        let a = action("Auto-process new memos over \(Int(minMinutes)) min", #selector(toggleAuto), "")
        a.state = auto ? .on : .off
        menu.addItem(a)
        let mm = NSMenuItem(title: "Minimum length", action: nil, keyEquivalent: "")
        let ms = NSMenu()
        for v in [1.0, 3, 5, 10, 20] {
            let it = action("\(Int(v)) min", #selector(setMin(_:)), "")
            it.representedObject = v
            it.state = v == minMinutes ? .on : .off
            ms.addItem(it)
        }
        mm.submenu = ms
        menu.addItem(mm)
        if blocked {
            menu.addItem(.separator())
            menu.addItem(action("⚠︎ Needs Full Disk Access to see Voice Memos", #selector(openPrivacy), ""))
        }
        menu.addItem(.separator())
        let login = action("Open at Login", #selector(toggleLogin), "")
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        menu.addItem(action("Quit Meeting Brain", #selector(quit), "q"))
    }

    func action(_ t: String, _ s: Selector, _ k: String) -> NSMenuItem {
        let i = NSMenuItem(title: t, action: s, keyEquivalent: k)
        i.target = self
        return i
    }

    @objc func processMemo(_ sender: NSMenuItem) {
        guard let p = sender.representedObject as? String,
              let m = Memos.list()?.first(where: { $0.url.path == p }) else { return }
        sendAll([m], open: true)
    }

    @objc func toggleLogin() {
        let s = SMAppService.mainApp
        try? (s.status == .enabled ? s.unregister() : s.register())
    }
    @objc func toggleAuto() { d.set(!auto, forKey: "auto") }
    @objc func setMin(_ s: NSMenuItem) { d.set(s.representedObject as? Double ?? 5, forKey: "minMinutes") }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func openPrivacy() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!)
    }

    // MARK: dashboard window

    @objc func openDash() { openDashboard(path: "") }

    func openDashboard(path: String) {
        if window == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 820),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            w.title = "Meeting Brain"
            w.isReleasedWhenClosed = false
            w.setFrameAutosaveName("dashboard")
            let cfg = WKWebViewConfiguration()
            cfg.userContentController.add(self, name: "mb")   // lets the dashboard list and process Voice Memos
            let v = WKWebView(frame: w.contentView!.bounds, configuration: cfg)
            v.autoresizingMask = [.width, .height]
            v.navigationDelegate = self   // exports arrive as downloads
            w.contentView!.addSubview(v)
            if w.frame.origin == .zero { w.center() }
            window = w; web = v
        }
        web?.load(URLRequest(url: URL(string: Engine.base.absoluteString + "/" + path)!))
        NSApp.setActivationPolicy(.regular)   // dock icon + cmd-tab while the window is open
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: window, queue: .main) { _ in
            NSApp.setActivationPolicy(.accessory)
        }
    }

    // MARK: dashboard bridge

    func userContentController(_ c: WKUserContentController, didReceive msg: WKScriptMessage) {
        guard let b = msg.body as? [String: Any], let id = b["id"] as? Int, let type = b["type"] as? String else { return }
        func reply(_ v: Any) {
            let data = (try? JSONSerialization.data(withJSONObject: v)) ?? Data("null".utf8)
            web?.evaluateJavaScript("mbReply(\(id), \(String(decoding: data, as: UTF8.self)))")
        }
        switch type {
        case "list":
            DispatchQueue.global(qos: .userInitiated).async {
                let memos = Memos.list()
                DispatchQueue.main.async {
                    guard let memos else { self.blocked = true; return reply(["blocked": true]) }
                    reply(["memos": memos.map { ["title": $0.title, "date": $0.date.timeIntervalSince1970,
                                                  "duration": $0.duration, "path": $0.url.path, "source": $0.source] },
                           "auto": self.auto, "minMinutes": self.minMinutes])
                }
            }
        case "process":
            NSLog("dashboard asked to process %@", String(describing: b["paths"]))
            let paths = Set(b["paths"] as? [String] ?? [])
            let todo = (Memos.list() ?? []).filter { paths.contains($0.url.path) }.reversed()
            sendAll(Array(todo))
            reply(["queued": todo.count])
        case "copy":
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(b["text"] as? String ?? "", forType: .string)
            reply(["ok": true])
        case "privacy":
            openPrivacy(); reply([:])
        default:
            reply([:])
        }
    }

    // MARK: exports (WKWebView drops downloads unless told what to do with them)

    var saving: [ObjectIdentifier: URL] = [:]

    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(a.shouldPerformDownload ? .download : .allow)
    }

    func webView(_ w: WKWebView, decidePolicyFor r: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let cd = (r.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(cd.contains("attachment") || !r.canShowMIMEType ? .download : .allow)
    }

    func webView(_ w: WKWebView, navigationAction a: WKNavigationAction, didBecome d: WKDownload) { d.delegate = self }
    func webView(_ w: WKWebView, navigationResponse r: WKNavigationResponse, didBecome d: WKDownload) { d.delegate = self }

    func download(_ d: WKDownload, decideDestinationUsing r: URLResponse, suggestedFilename name: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        let base = (name as NSString).deletingPathExtension, ext = (name as NSString).pathExtension
        var url = dir.appendingPathComponent(name)
        var n = 2
        while FileManager.default.fileExists(atPath: url.path) {   // never overwrite an earlier export
            url = dir.appendingPathComponent("\(base) \(n).\(ext)"); n += 1
        }
        saving[ObjectIdentifier(d)] = url
        completionHandler(url)
    }

    func downloadDidFinish(_ d: WKDownload) {
        if let url = saving.removeValue(forKey: ObjectIdentifier(d)) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }

    func download(_ d: WKDownload, didFailWithError e: Error, resumeData: Data?) {
        saving.removeValue(forKey: ObjectIdentifier(d))
        NSLog("export failed: %@", e.localizedDescription)
    }
}
