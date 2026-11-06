// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "MeetingBrain",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "MeetingBrain", path: "Sources/MeetingBrain",
                          linkerSettings: [.linkedLibrary("sqlite3")]),
    ]
)
