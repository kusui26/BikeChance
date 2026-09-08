import BikeChanceCore
import SwiftUI

/// アプリの入口。
///
/// **W2 の雛形の範囲**：`/v1/stations` を叩いて地図にポートを出すところまで。
/// 予測はまだ無いので、出すのは実測値だけで、必ず観測時刻を添える（CLAUDE.md §2 の 7）。
@main
struct BikeChanceApp: App {
    var body: some Scene {
        WindowGroup {
            MapScreen(model: StationsModel(client: AppEnvironment.makeClient()))
        }
    }
}
