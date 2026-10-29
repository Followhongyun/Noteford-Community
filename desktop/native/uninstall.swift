import AppKit
import Foundation

let application = NSApplication.shared
application.setActivationPolicy(.regular)
application.finishLaunching()
func alert(_ message: String) {
    let box = NSAlert(); box.messageText = "卸载 Noteford"; box.informativeText = message
    box.addButton(withTitle: "好"); application.activate(ignoringOtherApps: true); box.runModal()
}
let companion = Bundle.main.bundleURL
let home = FileManager.default.homeDirectoryForCurrentUser
let systemFolder = URL(fileURLWithPath: "/Applications", isDirectory: true)
let userFolder = home.appendingPathComponent("Applications", isDirectory: true)
let folder = companion.deletingLastPathComponent()
guard [systemFolder.path, userFolder.path].contains(folder.path) else {
    alert("请先把“卸载 Noteford”复制到“应用程序”文件夹，再打开。")
    exit(1)
}
let target = folder.appendingPathComponent("Noteford.app")
let confirmation = NSAlert()
confirmation.alertStyle = .warning
confirmation.messageText = "完整卸载 Noteford？"
confirmation.informativeText = "将关闭 Noteford，并永久删除这台电脑上所有 Noteford 账号的数据库、消息副本、媒体缓存、配置和登录凭据，同时移除应用及本卸载工具。此操作无法撤销。\n\n微信原始数据、云端 Accounts 账号、手动导出文件和系统备份不会删除。"
confirmation.addButton(withTitle: "取消")
confirmation.addButton(withTitle: "永久删除并卸载")
let checkbox = NSButton(checkboxWithTitle: "我确认删除所有 Noteford 账号的本地资料", target: nil, action: nil)
checkbox.frame = NSRect(x: 0, y: 0, width: 380, height: 28)
confirmation.accessoryView = checkbox
application.activate(ignoringOtherApps: true)
guard confirmation.runModal() == .alertSecondButtonReturn && checkbox.state == .on else { exit(0) }
let worker = Process()
worker.executableURL = companion.appendingPathComponent("Contents/Resources/service/noteford-service")
worker.arguments = ["--uninstall-mac", "--app", target.path, "--parent", String(ProcessInfo.processInfo.processIdentifier)]
let pipe = Pipe(); worker.standardOutput = pipe; worker.standardError = FileHandle.nullDevice
worker.standardInput = FileHandle.nullDevice
// The cleaner acknowledges validation before this independent UI exits. It never opens Noteford.
do {
    try worker.run()
    DispatchQueue.global().asyncAfter(deadline: .now() + 30) {
        if worker.isRunning { worker.terminate() }
    }
    var output = Data()
    while let line = try pipe.fileHandleForReading.read(upToCount: 1), !line.isEmpty {
        output.append(line)
        if line == Data([10]) { break }
        if output.count > 1024 { break }
    }
    guard String(data: output, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) == "READY" else {
        alert("卸载检查未通过，未开始删除资料。请重新打开本卸载工具重试。")
        exit(1)
    }
    exit(0)
} catch {
    alert("无法启动卸载工具，资料未删除。请重新安装正式版本后重试。")
    exit(1)
}
