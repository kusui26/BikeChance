import Foundation

/// お気に入り 1 行ぶんの見せ方（W5 プラン §6.8、開発プラン §9.2）。
///
/// **判断はここに置き、View は並べるだけにする**（`StationDetail`・`TripPlan` と同じ作法）。
///
/// 守る規則は 4 つ。
///   * **未観測は「—」。0 台と区別する**（`countText` を通す）
///   * **鮮度を超えた値を「現在値」として出さない**（`Freshness`）
///   * **オフラインでも最後の値を出す。ただし古さを必ず添える**（開発プラン §9.4）
///   * **「いつもの時刻」の確率は、曲線に在る点をそのまま読む**（補間しない。契約 16）
public struct FavoriteRow: Equatable, Sendable, Identifiable {
    /// どこから来た値か。**「取れた」と「取れなかったので前の値」を混ぜない。**
    public enum Source: Equatable, Sendable {
        /// いま取れた。
        case live
        /// 取れなかったので、最後に取れた値を出している。
        case cached
        /// 取れておらず、前の値も無い。
        case missing(String)
    }

    public let favorite: Favorite
    public let name: String
    public let systemName: String
    public let source: Source
    /// 実測値に添える観測時刻。**`missing` のときは `.unknown`。**
    public let freshness: Freshness
    /// 借りられる／返せる（現在値）。
    public let availability: [Availability]
    /// **いつもの時刻**の確率。`usualMinutes` が nil なら `.notRequested`。
    public let forecast: ForecastState

    public var id: String { favorite.id }

    /// 取れていないときに画面へ出す 1 行。取れていれば nil。
    public var note: String? {
        switch source {
        case .live: nil
        case .cached: FavoriteRow.cachedNote
        case .missing(let message): message
        }
    }

    /// **最後に取れた値を出していること**を必ず言う（開発プラン §9.4）。
    /// 古さそのものは `freshness` が「○分前の観測」として持っている。
    public static let cachedNote = "いま取得できませんでした。最後に取れた値です"
    /// 一度も取れていないとき。
    public static let missingText = "まだ取得できていません"
}

extension FavoriteRow {
    /// 詳細の応答 1 つから 1 行を作る。
    ///
    /// - Parameter source: `live` か `cached` か。**同じ写し方で、言う言葉だけが違う。**
    public init(
        favorite: Favorite,
        detail: StationDetailResponse,
        source: Source,
        intent: RideIntent,
        now: Date,
        locale: Locale = .current,
        timeZone: TimeZone = .current
    ) {
        // **この行の系統のフィード。** 詳細はそのポートの系統ぶんしか返さないが、
        // `first` ではなく名指しで引く（`TripPlan` と同じ理由）
        let feed = detail.feeds.first { $0.systemID == detail.station.systemID }
        let freshness = detail.station.freshness(feed: feed, now: now)
        self.favorite = favorite
        // **取れた名前があれば、そちらを出す。** 登録時の名前は控えにすぎない
        self.name = detail.station.name ?? favorite.name
        self.systemName = feed?.displayName ?? detail.station.systemID
        self.source = source
        self.freshness = freshness
        self.availability = RideIntent.allCases.map { intent in
            Availability(
                title: intent.verb,
                value: detail.station.countText(for: intent, freshness: freshness),
                caution: detail.station.stopNote(for: intent)
            )
        }
        self.forecast = FavoriteRow.forecast(
            favorite: favorite, detail: detail, feed: feed, intent: intent,
            locale: locale, timeZone: timeZone)
    }

    /// 一度も取れていない行。**名前だけは出す**（登録したときの名前）。
    public init(favorite: Favorite, message: String = FavoriteRow.missingText) {
        self.favorite = favorite
        self.name = favorite.name
        self.systemName = favorite.systemID
        self.source = .missing(message)
        self.freshness = .unknown
        self.availability = RideIntent.allCases.map { intent in
            Availability(title: intent.verb, value: StationDetail.unknownValue, caution: nil)
        }
        self.forecast = .notRequested
    }

    /// 「いつもの時刻」の確率。
    ///
    /// **曲線に在る点をそのまま読む。** 要求した分数に近い点を選び、**その点の絶対時刻**を
    /// 画面に出すので、ずれは隠れない（「15 分後」ではなく「10:35 到着」と書く）。
    ///
    /// null になる道は 4 つ。**どれも「出せない」として同じ形で返す**（理由は言わない）。
    ///   1. いつもの時刻を決めていない（`usualMinutes` が nil）
    ///   2. **フィードが滞っている**（`ForecastState.make` と同じ判断）
    ///   3. 予測が無い・古い（サーバーが `forecast_curve` を null にしている）
    ///   4. 曲線の長さがそろっていない
    private static func forecast(
        favorite: Favorite,
        detail: StationDetailResponse,
        feed: FeedStatus?,
        intent: RideIntent,
        locale: Locale,
        timeZone: TimeZone
    ) -> ForecastState {
        guard let minutes = favorite.usualMinutes else { return .notRequested }
        if feed?.isStale == true { return .unavailable(ForecastState.staleFeedText) }
        guard let curve = detail.forecastCurve,
            let index = curve.nearestIndex(toMinutes: minutes),
            let arrival = curve.arrival(at: index),
            let probability = curve.probability(for: intent, at: index)
        else { return .unavailable(ForecastState.unavailableText) }
        return .available(
            display(
                probability: probability, intent: intent, confidence: curve.confidence,
                arrival: arrival, basis: curve.baseObservedAt,
                locale: locale, timeZone: timeZone))
    }

    /// **地図と同じ丸めと言い回し**を通す（`ForecastDisplay` の規則は 1 か所）。
    ///
    /// **片方だけを大きく出す。** 20 件を 2 段で並べると読めないので、借りる／返すは
    /// 画面の切替（`FavoritesModel.intent`）で選ぶ——地図と同じ作法で、
    /// **切り替えても取りに行かない**（曲線に両方が入っている）。
    private static func display(
        probability: Double,
        intent: RideIntent,
        confidence: Int,
        arrival: Date,
        basis: Date,
        locale: Locale,
        timeZone: TimeZone
    ) -> ForecastDisplay {
        let percent = ForecastDisplay.percent(of: probability)
        return ForecastDisplay(
            percent: percent,
            band: ProbabilityBand.of(percent: percent),
            headline: "\(intent.verb)可能性 \(percent)%",
            arrival: "\(Arrival.clockText(arrival, locale: locale, timeZone: timeZone)) 到着",
            basis: """
                \(Arrival.clockText(basis, locale: locale, timeZone: timeZone)) \
                時点の観測にもとづく予測
                """,
            isReference: confidence <= ForecastDisplay.referenceConfidence
        )
    }
}
