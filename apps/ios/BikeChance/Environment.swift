import BikeChanceCore
import Foundation

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
