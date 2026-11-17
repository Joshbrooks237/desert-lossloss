import Cocoa
import Darwin
import WebKit

final class DragBridge: NSObject, WKScriptMessageHandler {
    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard let event = NSApp.currentEvent, event.type == .leftMouseDown,
              let window = message.webView?.window else { return }
        window.performDrag(with: event)
    }
}

final class DropWebView: WKWebView {
    override init(frame frameRect: NSRect, configuration: WKWebViewConfiguration) {
        super.init(frame: frameRect, configuration: configuration)
        registerForDraggedTypes([.fileURL])
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not used")
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        .copy
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        .copy
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let urls = sender.draggingPasteboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) as? [URL]
        guard let paths = urls?.map({ $0.path }), !paths.isEmpty else { return false }
        guard
            let data = try? JSONSerialization.data(withJSONObject: paths),
            let json = String(data: data, encoding: .utf8)
        else { return false }
        evaluateJavaScript("window.losslossDrop(\(json))", completionHandler: nil)
        return true
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    let page: URL?
    let bridge = DragBridge()
    var window: NSWindow?
    var web: DropWebView?
    var server: Process?
    var serverOut = ""
    var serverErr = ""
    var pageLoaded = false

    init(page: URL?) {
        self.page = page
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(bridge, name: "drag")
        let web = DropWebView(frame: .zero, configuration: configuration)
        web.navigationDelegate = self
        web.allowsBackForwardNavigationGestures = false
        web.underPageBackgroundColor = NSColor(srgbRed: 0.067, green: 0.055, blue: 0.047, alpha: 1)

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "LOSSLOSS"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.isMovableByWindowBackground = false
        window.backgroundColor = NSColor(srgbRed: 0.067, green: 0.055, blue: 0.047, alpha: 1)
        window.appearance = NSAppearance(named: .darkAqua)
        window.minSize = NSSize(width: 980, height: 680)
        window.contentView = web
        window.center()
        window.makeKeyAndOrderFront(nil)
        self.window = window
        self.web = web
        NSApp.activate(ignoringOtherApps: true)
        if let page {
            pageLoaded = true
            web.load(URLRequest(url: page))
        } else {
            startServer()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        guard let server, server.isRunning else { return }
        server.terminate()
        let limit = Date().addingTimeInterval(1.2)
        while server.isRunning && Date() < limit {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        if server.isRunning {
            kill(server.processIdentifier, SIGKILL)
        }
    }

    func startServer() {
        guard let resources = Bundle.main.resourceURL else {
            fail("The app bundle is missing its resources.")
            return
        }
        let script = resources.appendingPathComponent("player/server.py")
        guard FileManager.default.fileExists(atPath: script.path) else {
            fail("The player is missing from the app.")
            return
        }
        guard let python = Self.interpreter() else {
            fail("Python is missing from the app, and python3 was not found on this Mac.")
            return
        }
        let proc = Process()
        proc.executableURL = python
        proc.arguments = ["-u", script.path, "--no-open"]
        proc.currentDirectoryURL = resources
        var env = ProcessInfo.processInfo.environment
        let prefix = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        var path = prefix + ":" + (env["PATH"] ?? "")
        if let resources = Bundle.main.resourceURL {
            path = resources.appendingPathComponent("bin/\(Self.cpu)").path + ":" + path
        }
        env["PATH"] = path
        proc.environment = env
        let stdout = Pipe()
        let stderr = Pipe()
        proc.standardOutput = stdout
        proc.standardError = stderr
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            self?.takeServerOutput(data)
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async { self?.serverErr += text }
        }
        do {
            try proc.run()
        } catch {
            fail(error.localizedDescription)
            return
        }
        server = proc
        proc.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self, self.server === process, !self.pageLoaded else { return }
                let detail = self.serverErr.trimmingCharacters(in: .whitespacesAndNewlines)
                self.fail(detail.isEmpty ? "The player stopped before the window could open." : detail)
            }
        }
    }

    func takeServerOutput(_ data: Data) {
        let chunk = String(decoding: data, as: UTF8.self)
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.serverOut += chunk
            while let range = self.serverOut.range(of: "\n") {
                let line = String(self.serverOut[..<range.lowerBound])
                self.serverOut = String(self.serverOut[range.upperBound...])
                self.note(line)
            }
        }
    }

    func note(_ line: String) {
        guard line.contains("http://"),
              let raw = line.split(whereSeparator: \.isWhitespace).last else { return }
        var text = String(raw)
        if !text.contains("?") {
            if text.hasSuffix("/") {
                text += "?shell=1"
            } else {
                text += "/?shell=1"
            }
        }
        guard let page = URL(string: text) else { return }
        pageLoaded = true
        web?.load(URLRequest(url: page))
    }

    func fail(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "LOSSLOSS could not start"
        alert.informativeText = message
        alert.runModal()
        NSApp.terminate(nil)
    }

    static var cpu: String {
        #if arch(arm64)
        return "arm64"
        #else
        return "x86_64"
        #endif
    }

    static func interpreter() -> URL? {
        if let resources = Bundle.main.resourceURL {
            let bundled = resources.appendingPathComponent("python/\(cpu)/bin/python3")
            if FileManager.default.isExecutableFile(atPath: bundled.path) {
                return bundled
            }
        }
        return python()
    }

    static func python() -> URL? {
        let candidates = [
            "/usr/local/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/bin/python3",
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            return URL(fileURLWithPath: path)
        }
        return nil
    }
}

let page: URL? = {
    guard CommandLine.arguments.count > 1 else { return nil }
    return URL(string: CommandLine.arguments[1])
}()

let application = NSApplication.shared
application.setActivationPolicy(.regular)

let menu = NSMenu()
let appItem = NSMenuItem()
menu.addItem(appItem)
let appMenu = NSMenu()
appMenu.addItem(
    withTitle: "Quit LOSSLOSS",
    action: #selector(NSApplication.terminate(_:)),
    keyEquivalent: "q"
)
appItem.submenu = appMenu
application.mainMenu = menu

let delegate = AppDelegate(page: page)
application.delegate = delegate
application.run()
