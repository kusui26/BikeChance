import Foundation

/// ポート詳細の「到着時刻ごとの予測」（iOS-3。W7 プラン §4.9・§10.3、開発プラン §9.2）。
///
/// **点を線でつながない。** 曲線は補間していない生の 10 点で（契約 16）、水平の境目では
/// 森と B3 が切り替わって段が立つ（W7 プラン §10.2 の 7。境目で 0.1 以上跳ぶ曲線が 6.4%）。
/// なめらかな線にすると、その段が「この時刻に自転車が減る」という動きに見える——
/// 点を到着時刻ごとに置き、**点のあいだは予測していない**と添える（10/23 に決めた）。
///
/// 守る規則は 5 つ。**判断はすべてここに置き、View は並べるだけにする**（`StationDetail` と同じ）。
///   * **丸め・帯・言い回しは地図と同じ**（`ForecastDisplay` を通す。5% 刻み・上限 99%）
///   * **フィードが滞っていれば出さない**（地図・お気に入りと同じ判断）
///   * **過ぎた到着時刻の点は出さない**（起点は 5 分の格子で、応答は CDN に最大 3 分留まる）
///   * **予測には、基づく観測の時刻を添える**（契約 43）
///   * **長さのそろわない曲線・時刻の順に並ばない曲線は読まない**（欠けた表から数を作らない）
public struct ForecastTimeline: Equatable, Sendable {
    /// 到着時刻 1 つぶんの点。
    public struct Point: Equatable, Sendable, Identifiable {
        /// この点が指す到着の**絶対時刻**（曲線の `generated_at` ＋ 水平）。
        public let arrival: Date
        /// 地図と同じ見せ方。`percent` が縦の位置、`band` が色になる。
        public let display: ForecastDisplay

        public var id: Date { arrival }

        /// 読み上げ用。**「参考値」は点ごとに言わない**（10 回繰り返さない。欄で 1 度言う）。
        public var accessibilityLabel: String {
            "\(display.arrival)、\(display.headline)、\(display.band.label)"
        }
    }

    /// 縦軸の名前（「借りられる可能性」）。
    public let title: String
    /// **時刻の順。過ぎた到着は入らない。**
    public let points: [Point]
    /// 「13:00 時点の観測にもとづく予測」（契約 43）。
    public let basis: String
    /// 確度が低い（`confidence` が 1 以下）。**「参考値」として出す**（開発プラン §9.3）。
    public let isReference: Bool
    /// 地図で選んだ到着時刻。**点の範囲に入るときだけ**持つ（縦の線で印を付ける）。
    public let selectedArrival: Date?
}

extension ForecastTimeline {
    /// 欄の見出し。**出せないときも同じ見出しを出す**（何が出せないのかを言う）。
    public static func header(for intent: RideIntent) -> String {
        "\(intent.verb)可能性（到着時刻ごと）"
    }

    /// 点を線でつながない理由。**グラフの下に必ず添える。**
    public static let pointsNote = "点は到着時刻ごとの予測です。点と点のあいだの時刻は予測していません。"

    /// 確度が低いときの 1 行。
    public static let referenceNote = "確度が低いため、参考値として見てください。"

    /// 縦軸の目盛り（%）。**帯の境に置く**ので、点がどの帯に入るかを目で追える。
    public static let axisPercents = [
        0, ProbabilityBand.mediumLowerPercent, ProbabilityBand.highLowerPercent, 100,
    ]

    /// 曲線から作る。**`cutoff` より後の点が 1 つも無ければ nil**（出せる予測が無い）。
    init?(
        curve: ForecastCurve,
        intent: RideIntent,
        selectedArrival: Date?,
        cutoff: Date,
        locale: Locale,
        timeZone: TimeZone
    ) {
        let points = ForecastTimeline.points(
            of: curve, intent: intent, after: cutoff, locale: locale, timeZone: timeZone)
        guard let first = points.first, let last = points.last else { return nil }
        self.title = "\(intent.verb)可能性"
        self.points = points
        self.basis = ForecastDisplay.basisText(
            curve.baseObservedAt, locale: locale, timeZone: timeZone)
        self.isReference = curve.confidence <= ForecastDisplay.referenceConfidence
        self.selectedArrival = selectedArrival.flatMap {
            (first.arrival...last.arrival).contains($0) ? $0 : nil
        }
    }

    /// 1 つの水平を 1 つの点にする。**`cutoff` 以前の到着は入れない**（過ぎた時刻の予測は出さない）。
    static func points(
        of curve: ForecastCurve,
        intent: RideIntent,
        after cutoff: Date,
        locale: Locale,
        timeZone: TimeZone
    ) -> [Point] {
        curve.horizonsMinutes.indices.compactMap { index in
            guard let arrival = curve.arrival(at: index), arrival > cutoff,
                let probability = curve.probability(for: intent, at: index)
            else { return nil }
            return Point(
                arrival: arrival,
                display: ForecastDisplay(
                    probability: probability, intent: intent, confidence: curve.confidence,
                    arrival: arrival, basis: curve.baseObservedAt,
                    locale: locale, timeZone: timeZone))
        }
    }
}

/// 到着時刻ごとの予測を出せるかどうか。**出せない理由は地図と同じ 2 つの文言に畳む**（W4-02）。
public enum ForecastTimelineState: Equatable, Sendable {
    case unavailable(String)
    case available(ForecastTimeline)

    public var timeline: ForecastTimeline? {
        if case .available(let timeline) = self { return timeline }
        return nil
    }
}

extension ForecastTimelineState {
    /// 詳細の応答から、到着時刻ごとの点を組み立てる。
    ///
    /// - Parameters:
    ///   - selectedArrival: 地図で選んだ到着時刻（`StationsResponse.forecastArrival`）。
    ///     nil なら印を付けない。
    ///   - now: 端末の「いま」。**過ぎた点を落とすためだけに使う**——点の時刻は応答が持つ
    ///     （端末の時計から数え直さない。W4 プラン §12 の 114）。**応答の時刻より前には
    ///     しない**：端末の時計が遅れていても、サーバーが応答を作った時刻はもう過ぎている。
    public static func make(
        detail: StationDetailResponse,
        intent: RideIntent,
        selectedArrival: Date? = nil,
        now: Date,
        locale: Locale = .current,
        timeZone: TimeZone = .current
    ) -> ForecastTimelineState {
        // **フィードが滞っていれば予測を隠す**（`ForecastState.make` と同じ判断）
        if detail.feed?.isStale == true { return .unavailable(ForecastState.staleFeedText) }
        guard let curve = detail.forecastCurve, curve.lengthsMatch, curve.horizonsIncrease,
            let timeline = ForecastTimeline(
                curve: curve, intent: intent, selectedArrival: selectedArrival,
                cutoff: max(now, detail.generatedAt), locale: locale, timeZone: timeZone)
        else { return .unavailable(ForecastState.unavailableText) }
        return .available(timeline)
    }
}
