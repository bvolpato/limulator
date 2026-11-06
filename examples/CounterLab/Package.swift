// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "CounterLab",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(
            name: "CounterLab",
            targets: ["CounterLab"]
        ),
    ],
    targets: [
        .target(name: "CounterLab"),
    ]
)
