import Foundation

/// `/v1` を叩く。**アプリが触ってよい唯一のネットワーク経路。**
///
/// * DB に直結しない。キーを埋め込まない（CLAUDE.md §5）
/// * **`Authorization` を付けない。** CDN にキャッシュさせるための設計上の約束で、
///   ヘッダを足すとキャッシュが効かなくなる
/// * 失敗は `V1Error` に詰め替える。RFC 9457 の本文があればそのまま持つ
public struct V1Client: Sendable {
    /// 差し替え点。テストはここを置き換えてネットワークを使わない。
    public typealias Transport = @Sendable (URLRequest) async throws -> (Data, URLResponse)

    private let baseURL: URL
    private let transport: Transport

    public init(baseURL: URL, transport: @escaping Transport) {
        self.baseURL = baseURL
        self.transport = transport
    }

    /// 本番で使う組み立て。
    public init(baseURL: URL, session: URLSession = .shared) {
        self.init(baseURL: baseURL, transport: { request in try await session.data(for: request) })
    }

    /// 矩形の中のポートの現在値と、指定した到着時刻の予測。
    ///
    /// **渡した矩形はそのまま送らない。** 上限まで縮めてから格子に丸める。細かい位置が
    /// 要求に載らず（CLAUDE.md §5）、近い要求が同じ URL になって CDN も効く。
    ///
    /// - Parameter at: 到着時刻。**渡さなければ予測は返らない**（「いまの確率」は現在値
    ///   そのもの）。**相対の `in_min` ではなく絶対の時刻を送る**：応答は CDN に最大 3 分
    ///   留まりうるので、相対で頼むと指している到着が読むたびにずれる（W4 プラン §12 の 114）。
    public func stations(in bbox: Bbox, system: String? = nil, at: Date? = nil) async throws
        -> StationsResponse
    {
        let requested = bbox.clampedToRequestable().quantized()
        var items = [URLQueryItem(name: "bbox", value: requested.queryValue)]
        if let system {
            items.append(URLQueryItem(name: "system", value: system))
        }
        if let at {
            items.append(URLQueryItem(name: "at", value: V1Client.timestamp(at)))
        }
        return try await get(path: "/v1/stations", query: items)
    }

    /// `at` に載せる時刻。**UTC の `Z` で書く。**
    ///
    /// サーバーはタイムゾーンを必須にしている（素の日時を UTC と決めつけないため）。
    /// `+09:00` で書くと、クエリの中の `+` を `URLComponents` が符号化せずそのまま通し、
    /// 受け手によっては空白として解釈される。**`Z` なら符号化の問題が起きない。**
    static func timestamp(_ date: Date) -> String {
        date.formatted(.iso8601)
    }

    /// 行程が成立するかを調べる（`/v1/trip-check`。W4 プラン §6.7）。
    ///
    /// **系統は 1 つだけ。** 事業者をまたぐ行程は成立しないので、引数でも 1 つしか
    /// 受けない（W4-21）。
    ///
    /// **出発は絶対の `depart_at` で送る**（`stations(at:)` と同じ理由）。応答は CDN に
    /// 最大 3 分留まりうるので、相対で頼むと指している出発が読むたびにずれる
    /// （W4 プラン §12 の 114）。呼ぶ側が 5 分の格子に丸めてから渡す（`Arrival.gridded`）。
    ///
    /// - Parameter rideMinutes: 乗車時間。**省略するとサーバーが概算する**
    ///   （直線距離 ÷ 14 km/h。`ride_min_estimated` が true で返る）。端末が
    ///   `MKDirections` で出せたときだけ渡す。
    ///
    /// **範囲の検査はここでしない。** 「出発 ＋ 乗車」が水平（180 分）に収まるかは
    /// サーバーが見て 400 を返す。端末でも見ると**同じ範囲の実装が 2 つ**になり、
    /// 片方だけ直したときに食い違う（`packages/shared/src/trip.ts` が同じ理由で
    /// `parseRideMinutes` に上限を書いていない）。
    public func tripCheck(
        system: String,
        from: String,
        to: String,
        departAt: Date,
        rideMinutes: Int? = nil
    ) async throws -> TripCheckResponse {
        var items = [
            URLQueryItem(name: "system", value: system),
            URLQueryItem(name: "from", value: from),
            URLQueryItem(name: "to", value: to),
            URLQueryItem(name: "depart_at", value: V1Client.timestamp(departAt)),
        ]
        if let rideMinutes {
            items.append(URLQueryItem(name: "ride_min", value: String(rideMinutes)))
        }
        return try await get(path: "/v1/trip-check", query: items)
    }

    /// データの鮮度・クレジット・通知文。
    public func meta() async throws -> MetaResponse {
        try await get(path: "/v1/meta", query: [])
    }

    // MARK: - 実装

    private func get<T: Decodable>(path: String, query: [URLQueryItem]) async throws -> T {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw V1Error.invalidBaseURL
        }
        components.path = path
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw V1Error.invalidBaseURL }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        // Authorization は付けない（CDN キャッシュのため。開発プラン §8.3）

        let (data, response) = try await transport(request)
        guard let http = response as? HTTPURLResponse else { throw V1Error.notHTTP }
        guard (200..<300).contains(http.statusCode) else {
            throw V1Error.from(status: http.statusCode, body: data)
        }
        do {
            return try V1Client.makeDecoder().decode(T.self, from: data)
        } catch {
            throw V1Error.malformedBody(String(describing: error))
        }
    }

    /// `/v1` の時刻を読むデコーダ。
    ///
    /// **毎回作る。** `JSONDecoder` も `ISO8601DateFormatter` も `Sendable` ではないので、
    /// 静的に持つと Swift 6 の並行性検査で弾かれる。生成は軽いので共有しない。
    /// 解析には値型の `Date.ISO8601FormatStyle` を使い、可変の状態を持たないようにする。
    static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let text = try decoder.singleValueContainer().decode(String.self)
            guard let date = parseISO8601(text) else {
                throw DecodingError.dataCorrupted(
                    .init(codingPath: decoder.codingPath, debugDescription: "時刻として読めません: \(text)")
                )
            }
            return date
        }
        return decoder
    }

    /// ミリ秒つきと無しの両方を受ける。`/v1` はミリ秒つきを返すが、
    /// **相手の書式に 1 つだけ賭けない**（`observed_at` はフィード由来で秒精度のこともある）。
    static func parseISO8601(_ text: String) -> Date? {
        let withFraction = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
        let withoutFraction = Date.ISO8601FormatStyle(includingFractionalSeconds: false)
        return (try? withFraction.parse(text)) ?? (try? withoutFraction.parse(text))
    }
}

/// `/v1` を叩いたときの失敗。**利用者に見せる文言は `message` に集める。**
public enum V1Error: Error, Equatable, Sendable {
    /// サーバーが RFC 9457 で理由を返した。
    case problem(Problem)
    /// HTTP としては失敗したが、本文が Problem ではなかった。
    case http(status: Int)
    case notHTTP
    case invalidBaseURL
    case malformedBody(String)

    static func from(status: Int, body: Data) -> V1Error {
        if let problem = try? V1Client.makeDecoder().decode(Problem.self, from: body) {
            return .problem(problem)
        }
        return .http(status: status)
    }

    /// 地図を拡大してもらうべき状態か。
    public var needsNarrowerBbox: Bool {
        if case .problem(let problem) = self { return problem.needsNarrowerBbox }
        return false
    }

    /// 画面に出す 1 行。
    public var message: String {
        switch self {
        case .problem(let problem): problem.detail
        case .http(let status): "通信に失敗しました（\(status)）。"
        case .notHTTP: "通信に失敗しました。"
        case .invalidBaseURL: "接続先の設定が正しくありません。"
        case .malformedBody: "応答を読み取れませんでした。"
        }
    }
}
