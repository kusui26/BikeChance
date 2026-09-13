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
}

/// `/v1` の口を画面に配る。
///
/// **1 つを使い回す。** `V1Client` は `Sendable` な値型で、中身は接続先と `URLSession`
/// だけなので複製しても害は無いが、**接続先を差し替えたときに全部が付いてくる**形に
/// しておくほうが、開発中の取り違えが起きない（`BIKECHANCE_API_BASE_URL`）。
extension EnvironmentValues {
    @Entry var v1Client: V1Client = AppEnvironment.makeClient()
}
