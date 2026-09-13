import BikeChanceCore
import SwiftUI

/// アプリの入口。
///
/// **画面は 4 つ**：地図（`MapScreen`）、ポートの詳細（`StationDetailScreen`）、
/// 行程チェック（`TripCheckScreen`。W5 の PR G）、お気に入り（`FavoritesScreen`。PR H）。
///
/// **実測値には必ず観測時刻を添え、予測とは欄を分ける**（CLAUDE.md §2 の 7・8）。
/// 判断はすべて `BikeChanceCore` に置いてあり、View は並べるだけである。
@main
struct BikeChanceApp: App {
    /// **1 つだけ作る。** 画面には `@Environment(\.v1Client)` で配る。
    private let client: V1Client
    /// **お気に入りも 1 つだけ。** 地図のピンと一覧が同じ登録簿を見る——
    /// 2 つ作ると、☆ を押したのに一覧に出ない、が起きる。
    @State private var favorites: FavoritesModel

    init() {
        let client = AppEnvironment.makeClient()
        self.client = client
        _favorites = State(initialValue: AppEnvironment.makeFavorites(client: client))
    }

    var body: some Scene {
        WindowGroup {
            MapScreen(model: StationsModel(client: client), favorites: favorites)
                .environment(\.v1Client, client)
        }
    }
}
