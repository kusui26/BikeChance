// swift-tools-version: 6.2
import PackageDescription

/// アプリから切り離した**純粋なロジック**。
///
/// UIKit / SwiftUI / MapKit に依存しないので、`swift test` だけで（シミュレータ無しで）
/// 検証できる。開発プラン §9.1 の「純粋ロジックは nonisolated のパッケージへ」に対応する。
let package = Package(
    name: "BikeChanceCore",
    platforms: [.iOS(.v26), .macOS(.v15)],
    products: [
        .library(name: "BikeChanceCore", targets: ["BikeChanceCore"])
    ],
    targets: [
        .target(
            name: "BikeChanceCore",
            swiftSettings: [.swiftLanguageMode(.v6)]
        ),
        .testTarget(
            name: "BikeChanceCoreTests",
            dependencies: ["BikeChanceCore"],
            resources: [.copy("Fixtures")],
            swiftSettings: [.swiftLanguageMode(.v6)]
        ),
    ]
)
