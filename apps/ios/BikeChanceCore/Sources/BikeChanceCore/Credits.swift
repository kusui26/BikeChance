import Foundation
import Observation

/// 最後に取れた `/v1/meta` の置き場所（W6-31、契約 42）。
public protocol MetaCaching: Sendable {
    func read() -> Data?
    func write(_ body: Data)
}

/// 最後に取れた `/v1/meta` を 1 つだけ持つ。
///
/// **中身を解釈せずにバイトで持つ**（`FileFavoritesCache` と同じ理由：型に写してから書き戻すと、
/// サーバーが足した欄が端末のキャッシュで消える）。
///
/// **「いつの内容か」はファイルの日時から作らない。** 応答の `generated_at` が持っている
/// （ファイルの日時を読むと、プライバシーマニフェストに理由の申告が要る API になる）。
public struct FileMetaCache: MetaCaching {
    private let url: URL

    public init(directory: URL) {
        url = directory.appendingPathComponent("meta.json")
    }

    public func read() -> Data? {
        try? Data(contentsOf: url)
    }

    /// **書ききるか、前のまま。** 途中で落ちて壊れた JSON を残さない。
    public func write(_ body: Data) {
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? body.write(to: url, options: .atomic)
    }
}

/// 「データについて」の中身を取る（W6-31・32、契約 42）。
///
/// **文言はサーバーが持つ。** 端末はクレジット・出典・通知文・免責を並べるだけにする。
/// 事業者やライセンスの表記が変わっても、アプリを出し直さずに追随できる。
///
/// **取れなくても表示義務を落とさない。** 最後に取れた内容を、**それをサーバーが作った
/// 時刻つきで**出す。1 度も取れていない端末では、取れなかったことを言う。
@MainActor
@Observable
public final class CreditsModel {
    public enum State: Equatable, Sendable {
        case loading
        /// いま取れた。
        case live(MetaResponse)
        /// 取れなかったので、最後に取れた内容を出している。
        case cached(MetaResponse)
        /// 取れず、取っておいた内容も無い（または読めない）。
        case failed(String)
    }

    public private(set) var state: State = .loading
    private let client: V1Client
    private let cache: MetaCaching

    public init(client: V1Client, cache: MetaCaching) {
        self.client = client
        self.cache = cache
    }

    /// 取りに行く。**読めた応答だけを取っておく**（壊れた本文で前の内容を上書きしない）。
    public func load() async {
        do {
            let body = try await client.metaBody()
            let meta = try V1Client.decoded(MetaResponse.self, from: body)
            cache.write(body)
            state = .live(meta)
        } catch {
            state = fallback(after: error)
        }
    }

    private func fallback(after error: Error) -> State {
        if let body = cache.read(),
            let meta = try? V1Client.decoded(MetaResponse.self, from: body)
        {
            return .cached(meta)
        }
        return .failed((error as? V1Error)?.message ?? CreditsDisplay.failedText)
    }
}

/// 「データについて」に出す、内容の外側の言葉（W6-31・32）。
public struct CreditsDisplay: Equatable, Sendable {
    /// 取れず、取っておいた内容も無いとき。
    public static let failedText = "通信に失敗しました。"

    public let meta: MetaResponse
    /// 取れずに、取っておいた内容を出している。
    public let isOffline: Bool
    /// いつの内容か。「10/11 8:30 時点の内容です。」
    public let asOf: String
    /// 配っている予測の版（W6-32）。「予測モデル：composite-v1-20261012」
    public let modelVersion: String

    /// 表示する内容が無い状態（取りに行っている途中・取れなかった）なら nil。
    public init?(
        state: CreditsModel.State, locale: Locale = .current, timeZone: TimeZone = .current
    ) {
        switch state {
        case .live(let meta):
            self.init(meta: meta, isOffline: false, locale: locale, timeZone: timeZone)
        case .cached(let meta):
            self.init(meta: meta, isOffline: true, locale: locale, timeZone: timeZone)
        case .loading, .failed:
            return nil
        }
    }

    init(meta: MetaResponse, isOffline: Bool, locale: Locale, timeZone: TimeZone) {
        let when = CreditsDisplay.dateTimeText(meta.generatedAt, locale: locale, timeZone: timeZone)
        self.meta = meta
        self.isOffline = isOffline
        self.asOf =
            isOffline
            ? "通信できないため、\(when) に取得した内容を表示しています。"
            : "\(when) 時点の内容です。"
        self.modelVersion = meta.modelVersion.map { "予測モデル：\($0)" } ?? "予測はまだ配信していません"
    }

    /// 日付つきの時刻。**取っておいた内容は何日も前のことがある**ので、時刻だけにしない。
    static func dateTimeText(_ date: Date, locale: Locale, timeZone: TimeZone) -> String {
        date.formatted(
            Date.FormatStyle(locale: locale, timeZone: timeZone)
                .month(.defaultDigits).day().hour().minute())
    }
}
