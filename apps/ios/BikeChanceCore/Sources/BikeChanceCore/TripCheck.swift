import Foundation

/// 行程チェックの画面に出す内容を組み立てる（W5 プラン §6.7、開発プラン §9.2）。
///
/// **判断はすべてここに置き、View は並べるだけにする**（`StationDetail` と同じ作法）。
/// 行程の画面は「2 つの確率を掛けた 1 つの数」を出すので、**掛けてよい条件**を
/// 見落とすと、根拠のない数字が画面のいちばん大きい字で出る。
///
/// 守る規則は 5 つ。
///   * **独立仮定の注記を確率と同じ構造体に置く**（W4-24）。数だけ取り出せなくする
///   * **`p_trip` を出すのは、両端の確率を**両方出しているときだけ**（契約 10）
///   * **時刻は絶対で表示する**（W4 プラン §12 の 114）。「約 30 分後」と書かない
///   * **`confidence` は行程のもの**を使う（サーバーが両端の小さいほうを入れてある）
///   * **代替候補を並べ替えない**（順序はサーバーが決める。W4-25）
public struct TripPlan: Equatable, Sendable {
    public let systemName: String
    /// 借りる側。
    public let from: TripEndpoint
    /// 返す側。
    public let to: TripEndpoint
    public let ride: RideSummary
    /// 両方うまくいく見通し。**出せないこともある。**
    public let outcome: TripOutcomeState
    /// **表示は義務**（CC BY 4.0）。この行程の系統のぶんだけ。
    public let credit: String?

    public init(
        response: TripCheckResponse,
        now: Date,
        locale: Locale = .current,
        timeZone: TimeZone = .current
    ) {
        // **`feeds` は 2 系統ぶん来る**（地図と同じ形）。行程の系統のものを引く
        let feed = response.feed
        self.systemName = feed?.displayName ?? response.systemID
        self.from = TripEndpoint(
            station: response.from, feed: feed, intent: .borrow,
            at: response.departure, suffix: TripEndpoint.departureSuffix,
            alternatives: response.alternatives.from, systemID: response.systemID,
            now: now, locale: locale, timeZone: timeZone
        )
        self.to = TripEndpoint(
            station: response.to, feed: feed, intent: .returnBike,
            at: response.arrival, suffix: TripEndpoint.arrivalSuffix,
            alternatives: response.alternatives.to, systemID: response.systemID,
            now: now, locale: locale, timeZone: timeZone
        )
        self.ride = RideSummary(
            minutes: response.rideMinutes, isEstimated: response.rideMinutesEstimated)
        self.outcome = TripOutcomeState.make(
            outcome: response.trip, from: self.from.forecast, to: self.to.forecast,
            departure: response.departure, arrival: response.arrival,
            locale: locale, timeZone: timeZone
        )
        self.credit = response.credit?.credit
    }
}

/// 行程の片側（借りる側・返す側）。
public struct TripEndpoint: Equatable, Sendable {
    /// 「14:35 出発」に付ける語。
    static let departureSuffix = "出発"
    /// 「14:49 到着」に付ける語。
    static let arrivalSuffix = "到着"

    public let stationID: String
    public let name: String
    /// この側で見るべき確率。借りる側は自転車、返す側は空き。
    public let intent: RideIntent
    /// 「14:35 出発」。**絶対時刻**（W4 プラン §12 の 114）。
    public let clock: String
    /// その時刻の確率。**出せないこともある**（理由は返らない）。
    public let forecast: ForecastState
    /// いまの台数。**確率が主、台数は従**（CLAUDE.md §2 の 7）。
    public let count: Availability
    /// 実測値に添える観測時刻。
    public let freshness: Freshness
    /// 代替のポート候補。**並びはサーバーが決めたまま。**
    public let alternatives: [TripAlternativeRow]

    init(
        station: StationCurrent,
        feed: FeedStatus?,
        intent: RideIntent,
        at: Date,
        suffix: String,
        alternatives: [TripAlternative],
        systemID: String,
        now: Date,
        locale: Locale,
        timeZone: TimeZone
    ) {
        let freshness = station.freshness(feed: feed, now: now)
        self.stationID = station.stationID
        self.name = station.name ?? "（名称未取得）"
        self.intent = intent
        self.clock = "\(Arrival.clockText(at, locale: locale, timeZone: timeZone)) \(suffix)"
        self.forecast = ForecastState.make(
            station: station, feed: feed, intent: intent, arrival: at,
            locale: locale, timeZone: timeZone)
        self.count = Availability(
            title: intent.verb,
            value: station.countText(for: intent, freshness: freshness),
            caution: station.stopNote(for: intent)
        )
        self.freshness = freshness
        // **別事業者のポートは候補にしない。ここでも受け取らない。**
        //
        // 借りられるのは同じ事業者の自転車だけなので、これは製品の事実であって
        // サーバーの都合ではない。サーバーも同一系統に絞っているが（W4-25）、
        // **2026-09-13 の本番は絞れていなかった**——`station_id` の衝突で、
        // 虎ノ門の行程に厚木（約 40 km）のポートが「80 m 先」として返っていた
        // （W5 プラン §12 の 158）。原因はサーバー側で直したが、**端末でも見る**：
        // CDN に残った古い応答が最大 3 分返り続けるし、`StationDetail` が
        // 「容量が動的な系統では数を出さない」をサーバーと両方で守っているのと同じ理由である。
        self.alternatives =
            alternatives
            .filter { $0.station.systemID == systemID }
            .map {
                TripAlternativeRow(
                    alternative: $0, feed: feed, intent: intent, at: at,
                    locale: locale, timeZone: timeZone)
            }
    }
}

/// 代替候補の 1 行。
public struct TripAlternativeRow: Equatable, Sendable, Identifiable {
    public let stationID: String
    public let name: String
    /// 「203 m」。**徒歩で歩く距離。**
    public let distance: String
    /// その端点と同じ時刻の確率。
    public let forecast: ForecastState

    public var id: String { stationID }

    init(
        alternative: TripAlternative,
        feed: FeedStatus?,
        intent: RideIntent,
        at: Date,
        locale: Locale,
        timeZone: TimeZone
    ) {
        self.stationID = alternative.station.stationID
        self.name = alternative.station.name ?? "（名称未取得）"
        self.distance = "\(alternative.distanceMeters) m"
        self.forecast = ForecastState.make(
            station: alternative.station, feed: feed, intent: intent, arrival: at,
            locale: locale, timeZone: timeZone)
    }
}

/// 乗車時間の見せ方。
public struct RideSummary: Equatable, Sendable {
    public let minutes: Int
    /// 「自転車 14 分」
    public let label: String
    /// **サーバーが概算したか。**
    public let isEstimated: Bool
    /// 概算の但し書き。**概算でなければ nil。**
    public let note: String?

    /// **概算は楽観側に外れる。** 直線距離 ÷ 14 km/h なので、市街地の経路
    /// （直線の 1.2〜1.4 倍）より短く出る。そのことを画面に書く。
    public static let estimatedNote = "直線距離からの概算です。実際の経路はこれより長く、到着は遅くなることがあります。"

    init(minutes: Int, isEstimated: Bool) {
        self.minutes = minutes
        self.label = "自転車 \(minutes) 分"
        self.isEstimated = isEstimated
        self.note = isEstimated ? RideSummary.estimatedNote : nil
    }
}

/// 「両方うまくいく」見通し。**出せないときは欄ごと消す**（0% と出さない）。
public enum TripOutcomeState: Equatable, Sendable {
    /// 出せない。**理由は 1 つに畳む**（`ForecastState` と同じ方針）。
    case unavailable(String)
    case available(TripDisplay)

    public var display: TripDisplay? {
        if case .available(let display) = self { return display }
        return nil
    }

    /// 片側でも確率を出していないときの文言。
    public static let unavailableText = "両端の確率がそろわないため、行程の見通しは出せません"

    /// 掛け算を出してよいかを決める。
    ///
    /// **サーバーが `trip` を返していても、端末が片側を隠すなら掛け算も隠す。**
    /// 契約 10（`p_trip` が非 null ⟺ 両端の予測が非 null）はサーバー側の約束だが、
    /// 端末には**もう 1 つ隠す道**がある——フィードが滞っていれば `ForecastState` が
    /// 確率を伏せる（`staleFeedText`）。そのとき掛け算だけが残ると、**画面のいちばん
    /// 大きい字が、隠したはずの数から作られている**ことになる。
    ///
    /// **注記が無ければ出さない。** 独立仮定を伏せた `p_trip` は根拠のない断定になる
    /// （W4-24）。サーバーのスキーマは空文字を許さないが、**ここでも受け取らない**
    /// （CDN に残った古い応答や、将来の別の経路に備える。`StationDetail` の容量と同じ作法）。
    static func make(
        outcome: TripOutcome?,
        from: ForecastState,
        to: ForecastState,
        departure: Date,
        arrival: Date,
        locale: Locale,
        timeZone: TimeZone
    ) -> TripOutcomeState {
        guard case .available = from, case .available = to else {
            return .unavailable(unavailableText)
        }
        guard let outcome, !outcome.notice.isEmpty else { return .unavailable(unavailableText) }
        let percent = ForecastDisplay.percent(of: outcome.probability)
        return .available(
            TripDisplay(
                percent: percent,
                band: ProbabilityBand.of(percent: percent),
                headline: "両方うまくいく可能性 \(percent)%",
                notice: outcome.notice,
                window: """
                    \(Arrival.clockText(departure, locale: locale, timeZone: timeZone)) 出発 → \
                    \(Arrival.clockText(arrival, locale: locale, timeZone: timeZone)) 到着
                    """,
                isReference: outcome.confidence <= ForecastDisplay.referenceConfidence
            )
        )
    }
}

/// 行程の見通しの見せ方。
///
/// **`notice` を持たない `TripDisplay` は作れない。** 生成の口は
/// `TripOutcomeState.make` だけで（メンバーワイズ初期化子は公開していない）、
/// そこが注記の無い応答を弾く。**数と注記を切り離せなくする**のが W4-24 の要求で、
/// 「同じ欄に置く」を型で表すとこうなる。
public struct TripDisplay: Equatable, Sendable {
    /// 5% 刻み・上限 99% の百分率（`ForecastDisplay` と同じ丸め）。
    public let percent: Int
    public let band: ProbabilityBand
    /// 「両方うまくいく可能性 45%」
    public let headline: String
    /// **独立仮定の注記。** 必ず添える。
    public let notice: String
    /// 「14:35 出発 → 14:49 到着」
    public let window: String
    /// **確度が低い。「参考値」として出す。** 行程の `confidence`（両端の小さいほう）で決まる。
    public let isReference: Bool

    /// 読み上げ用の 1 文。**注記まで読む**——見える文字と読まれる文字を食い違わせない。
    public var accessibilityLabel: String {
        let reference = isReference ? "、参考値" : ""
        return "\(window)、\(headline)、\(band.label)\(reference)。\(notice)"
    }
}
