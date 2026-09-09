import Foundation

/// 利用者が何をしたいか。**同じポートでも見るべき確率が違う。**
///
/// 借りたい人には自転車の有無が、返したい人には空き枠の有無が要る。1 つの色に混ぜると
/// どちらの話か分からなくなるので、選ばせて片方だけを出す（開発プラン §9.2）。
public enum RideIntent: String, Equatable, Sendable, CaseIterable, Identifiable {
    case borrow
    case returnBike

    public var id: String { rawValue }

    /// 切替に出す短い名前。
    public var label: String {
        switch self {
        case .borrow: "借りる"
        case .returnBike: "返す"
        }
    }

    /// 「〜可能性 80%」の前に置く言葉。**断定しない形にする**（CLAUDE.md §2 の 7）。
    public var verb: String {
        switch self {
        case .borrow: "借りられる"
        case .returnBike: "返せる"
        }
    }
}

/// 確率の帯（開発プラン §9.3 の表）。**色は View が決める**（ここは UI に依存しない）。
public enum ProbabilityBand: Equatable, Sendable {
    /// 85% 以上。
    case high
    /// 60〜84%。
    case medium
    /// 60% 未満。
    case low

    /// 帯の言い換え。**数字だけでは行動に結びつかない。**
    public var label: String {
        switch self {
        case .high: "ほぼ大丈夫"
        case .medium: "余裕を持って"
        case .low: "別の手を"
        }
    }

    /// 表示する百分率から帯を決める。**丸めた後の数で決める**ので、
    /// 「85% と出ているのに『余裕を持って』」という食い違いが起きない。
    public static func of(percent: Int) -> ProbabilityBand {
        if percent >= 85 { return .high }
        if percent >= 60 { return .medium }
        return .low
    }
}

/// 予測 1 つぶんの見せ方（開発プラン §9.3、W4 プラン §6.2）。
///
/// **断定しない。** 「借りられます」ではなく「借りられる可能性 80%」。
/// **過度な精度を見せない。** 5% 刻みに丸め、**100% とは言わない**（上限 99%）。
public struct ForecastDisplay: Equatable, Sendable {
    /// 5% 刻み・上限 99 の百分率。
    public let percent: Int
    public let band: ProbabilityBand
    /// 「借りられる可能性 80%」
    public let headline: String
    /// 「14:35 到着」。**この確率が指している時刻**（端末の「いま」からの分数ではない）。
    public let arrival: String
    /// 「10:33 時点の観測にもとづく予測」（開発プラン §9.2 の表示義務）。
    public let basis: String
    /// **確度が低い。「参考値」として出す**（開発プラン §9.3）。
    public let isReference: Bool

    /// 読み上げ用の 1 文。順番を「時刻 → 可能性」にして、数字だけが読まれないようにする。
    public var accessibilityLabel: String {
        let reference = isReference ? "、参考値" : ""
        return "\(arrival)、\(headline)、\(band.label)\(reference)"
    }
}

/// 予測を出せるかどうか。**出せない理由は 1 つに畳む**（W4-02）。
///
/// サーバーは理由を返さない（貸出・返却が止まっている／観測が古い／成果物に無い）。
/// 利用者に要るのは「いまは出せない」までで、原因は `inference_log` と監視が持つ。
public enum ForecastState: Equatable, Sendable {
    /// 到着時刻を選んでいない。**「いまの確率」は現在値そのもの**なので、予測は出さない。
    case notRequested
    /// 出せない。台数は出す（現在値は別の情報）。
    case unavailable(String)
    case available(ForecastDisplay)

    public var display: ForecastDisplay? {
        if case .available(let display) = self { return display }
        return nil
    }

    /// 地図のピンに使う帯。出せないときは nil（灰色にする）。
    public var band: ProbabilityBand? { display?.band }
}

extension ForecastState {
    /// 予測が無いときの文言。**理由は言わない。**
    public static let unavailableText = "いまは予測できません"
    /// フィードが滞っているときの文言。**こちらは理由が分かっている**ので伝える。
    public static let staleFeedText = "更新が滞っているため、いまは予測を出しません"

    /// 1 ポートぶんの予測の見せ方を決める。
    ///
    /// - Parameter arrival: **応答が指している到着時刻**（`StationsResponse.forecastArrival`）。
    ///   端末の時計から計算し直さない。応答は CDN に留まりうるので、**指している時刻は
    ///   応答が持っている**（W4 プラン §12 の 114）。nil なら予測を頼んでいない。
    public static func make(
        station: StationCurrent,
        feed: FeedStatus?,
        intent: RideIntent,
        arrival: Date?,
        locale: Locale = .current,
        timeZone: TimeZone = .current
    ) -> ForecastState {
        guard let arrival else { return .notRequested }
        // **フィードが滞っていれば予測を隠す**（開発プラン §9.2）。台数が信用できない
        // ときの確率は、数字の形をしているぶんだけ余計に信用されてしまう
        if feed?.isStale == true { return .unavailable(staleFeedText) }
        guard let forecast = station.forecast else { return .unavailable(unavailableText) }
        let percent = ForecastDisplay.percent(of: forecast.probability(for: intent))
        return .available(
            ForecastDisplay(
                percent: percent,
                band: ProbabilityBand.of(percent: percent),
                headline: "\(intent.verb)可能性 \(percent)%",
                arrival: "\(Arrival.clockText(arrival, locale: locale, timeZone: timeZone)) 到着",
                basis: """
                    \(Arrival.clockText(forecast.baseObservedAt, locale: locale, timeZone: timeZone)) \
                    時点の観測にもとづく予測
                    """,
                isReference: forecast.confidence <= ForecastDisplay.referenceConfidence
            )
        )
    }
}

extension ForecastDisplay {
    /// 表示の刻み（5%）。**過度な精度を見せない**（開発プラン §9.3）。
    public static let stepPercent = 5
    /// 表示の上限。**100% とは言い切らない。**
    public static let maxPercent = 99
    /// これ以下の確度は「参考値」。新設ポート・鮮度不良・再配置直後に落ちる。
    public static let referenceConfidence = 1

    /// 0〜1 の確率を、5% 刻み・上限 99% の百分率にする。
    ///
    /// **丸めてから上限を掛ける。** 先に上限を掛けると 99% が 100% に戻る。
    ///
    /// **上限だけが刻みから外れる。** 「5% 刻み」と「100% と言わない」は上の端でぶつかり、
    /// 100 を 99 に落とすので 99 だけが 5 の倍数でなくなる。95 に落とすと 0.96 と 0.999 が
    /// 同じ表示になり、**上のほうの違いが全部つぶれる**（開発プラン §9.3）。
    public static func percent(of probability: Double) -> Int {
        let steps = (probability * 100 / Double(stepPercent)).rounded()
        let rounded = Int(steps) * stepPercent
        return min(max(rounded, 0), maxPercent)
    }
}
