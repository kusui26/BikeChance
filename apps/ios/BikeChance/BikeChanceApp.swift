import BikeChanceCore
import SwiftUI

/// アプリの入口。
///
/// **画面は 3 つ**：地図（`MapScreen`）、ポートの詳細（`StationDetailScreen`）、
/// 行程チェック（`TripCheckScreen`。W5 の PR G）。
///
/// **実測値には必ず観測時刻を添え、予測とは欄を分ける**（CLAUDE.md §2 の 7・8）。
/// 判断はすべて `BikeChanceCore` に置いてあり、View は並べるだけである。
@main
struct BikeChanceApp: App {
    /// **1 つだけ作る。** 画面には `@Environment(\.v1Client)` で配る。
    private let client = AppEnvironment.makeClient()

    var body: some Scene {
        WindowGroup {
            MapScreen(model: StationsModel(client: client))
                .environment(\.v1Client, client)
        }
    }
}
