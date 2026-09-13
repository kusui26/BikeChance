import BikeChanceCore
import Foundation
import SwiftUI

/// 接続先。**アプリが触るのは `/v1` だけ**（CLAUDE.md §5）。
///
/// キーもトークンも持たない。ここに秘密が現れたら設計が壊れている。
enum AppEnvironment {
    /// 既定は本番の `/v1`。
    ///
    /// 開発中はスキームの環境変数 `BIKECHANCE_API_BASE_URL` で差し替えられる。
    /// **Info.plist に置かない**のは、配布物に開発用の設定を混ぜないため。
    static var baseURL: URL {
        if let text = ProcessInfo.processInfo.environment["BIKECHANCE_API_BASE_URL"],
            let url = URL(string: text), !text.isEmpty
        {
            return url
        }
        return URL(string: "https://bike-chance.vercel.app")!
    }

    static func makeClient() -> V1Client {
        V1Client(baseURL: baseURL)
    }

    /// お気に入りの置き場所。
    ///
    /// **App Group のコンテナを先に試す**（Widget から同じファイルが読めるように。
    /// 開発プラン §9.1）。**まだ権限を宣言していない**ので、いまは必ず 2 つ目に落ちる
    /// ——Widget は W9 で、そのとき `.entitlements` を足せば**ここも保存の側も
    /// 1 行も変えずに**コンテナへ移る。
    ///
    /// **黙って消えない置き場所を選ぶ。** `Documents` は端末のバックアップに入り、
    /// `Caches` と違って OS に消されない——お気に入りは利用者が作ったもので、
    /// 作り直せるキャッシュではない。
    static var favoritesDirectory: URL {
        if let shared = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier: appGroupID)
        {
            return shared
        }
        return FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    /// Widget（W9）と共有する入れ物の名前。**宣言するのは W9。**
    static let appGroupID = "group.app.bikechance"

    /// 最後に取れた応答の置き場所。**こちらは作り直せる**ので `Caches` でよい。
    static var favoritesCacheDirectory: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("favorites", isDirectory: true)
    }

    @MainActor
    static func makeFavorites(client: V1Client) -> FavoritesModel {
        FavoritesModel(
            client: client,
            store: FileFavoritesStore(directory: favoritesDirectory),
            cache: FileFavoritesCache(directory: favoritesCacheDirectory)
        )
    }
}

/// `/v1` の口を画面に配る。
///
/// **1 つを使い回す。** `V1Client` は `Sendable` な値型で、中身は接続先と `URLSession`
/// だけなので複製しても害は無いが、**接続先を差し替えたときに全部が付いてくる**形に
/// しておくほうが、開発中の取り違えが起きない（`BIKECHANCE_API_BASE_URL`）。
extension EnvironmentValues {
    @Entry var v1Client: V1Client = AppEnvironment.makeClient()
}
