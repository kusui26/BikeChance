import Foundation

/// まとめた印を押したときの一覧の 1 行（W8 プランの W8-11）。**判断はここに置き、View は並べる
/// だけにする**（`FavoriteRow`・`StationDetail` と同じ作法）。
///
/// 出すのは**名前・系統・地図の色と同じ話**の 3 つ。到着を選んでいれば**その時刻の確率**、
/// 選んでいなければ**いまの台数と観測時刻**（CLAUDE.md §2 の 7：実測値には観測時刻、予測値には
/// 到着時刻を添える）。台数の規則（未観測は「—」・鮮度切れを現在値として出さない）は
/// `countText` の 1 か所を通す。
public struct MarkerChoice: Identifiable, Equatable, Sendable {
    public let station: StationCurrent
    public let name: String
    /// 系統の表示名（応答の `feeds` の `display_name`。無ければ `system_id`）。
    public let systemName: String
    /// 「借りられる可能性 80%」か「いま 借りられる 3・2 分前の観測」。
    public let headline: String
    /// 予測なら「14:35 到着」。いまの台数なら nil（観測時刻は `headline` に入っている）。
    public let detail: String?
    /// 地図の色と同じ帯。**到着を選んでいて予測が出せるときだけ**（ほかは灰色の扱い）。
    public let band: ProbabilityBand?

    public var id: String { station.id }
}

extension MarkerChoice {
    /// 1 行を作る。
    ///
    /// - Parameter arrival: **応答が指している到着時刻**（`StationsResponse.forecastArrival`）。
    ///   nil なら予測を頼んでいない。
    public init(
        station: StationCurrent,
        feed: FeedStatus?,
        intent: RideIntent,
        arrival: Date?,
        now: Date,
        locale: Locale = .current,
        timeZone: TimeZone = .current
    ) {
        let forecast = ForecastState.make(
            station: station, feed: feed, intent: intent, arrival: arrival,
            locale: locale, timeZone: timeZone)
        let lines = MarkerChoice.lines(
            forecast, station: station, feed: feed, intent: intent, now: now)
        self.station = station
        self.name = station.name ?? station.stationID
        self.systemName = feed?.displayName ?? station.systemID
        self.headline = lines.headline
        self.detail = lines.detail
        self.band = forecast.band
    }

    /// 出す 2 行。**予測を頼んでいなければいまの台数**（観測時刻つき）、頼んでいれば予測の話だけ。
    static func lines(
        _ forecast: ForecastState,
        station: StationCurrent,
        feed: FeedStatus?,
        intent: RideIntent,
        now: Date
    ) -> (headline: String, detail: String?) {
        switch forecast {
        case .notRequested: (station.currentLine(for: intent, feed: feed, now: now), nil)
        case .unavailable(let message): (message, nil)
        case .available(let display): (display.headline, display.arrival)
        }
    }

    /// まとめた印の一覧（入力の順＝サーバーの `system_id, station_id`）。
    public static func list(
        _ group: MarkerGroup,
        feeds: [String: FeedStatus],
        intent: RideIntent,
        arrival: Date?,
        now: Date
    ) -> [MarkerChoice] {
        group.members.map { station in
            MarkerChoice(
                station: station, feed: feeds[station.systemID], intent: intent,
                arrival: arrival, now: now)
        }
    }
}
