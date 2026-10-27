import Foundation
import Testing

@testable import BikeChanceCore

/// 重なるポートの印（W8 プランの W8-11、W7 プランの所見 232）。
///
/// **主題は「押し分けられないものだけをまとめ、まとめ方が揺れないこと」。**
///   * 0 m の 2 つ（25 か所が同じ名前・同じ座標）はまとまる。22 m（001.関内駅）は分かれる
///   * 境目は 1 m（それより遠いものはまとめない）
///   * 組の顔ぶれも並びも、入力の並びだけで決まる（同じ範囲で印が入れ替わらない）
///   * 表示上限は印の数で数える（同じ場所の 2 つで枠を 2 つ使わない）
@Suite("重なるポートの印")
struct MarkerGroupingTests {
    let jst = TimeZone(identifier: "Asia/Tokyo")!
    let ja = Locale(identifier: "ja_JP")
    /// 2026-09-09 14:03:20 JST。
    let now = Date(timeIntervalSince1970: 1_788_930_200)

    /// 横浜の関内あたり。**経度 1 度の縮み**が効く緯度にする。
    let baseLatitude = 35.4437
    let baseLongitude = 139.6380

    func station(
        _ system: String, _ id: String, north: Double = 0, east: Double = 0,
        bikes: Int? = 3, forecast: StationForecast? = nil
    ) -> StationCurrent {
        let metersPerDegree = MarkerGrouping.metersPerDegree
        let latitude = baseLatitude + north / metersPerDegree
        let longitude = baseLongitude + east / (metersPerDegree * cos(baseLatitude * .pi / 180))
        return StationCurrent(
            systemID: system, stationID: id, name: "関内駅", latitude: latitude,
            longitude: longitude, capacity: 10, capacityEstimate: 12, capacityDays: 7,
            capacityUnbounded: false, bikes: bikes, docks: 5, isInstalled: true,
            isRenting: true, isReturning: true, isPresent: true,
            observedAt: now.addingTimeInterval(-120), lastChangedAt: now, forecast: forecast
        )
    }

    func feed(_ system: String, _ name: String) -> FeedStatus {
        FeedStatus(
            systemID: system, displayName: name, dataUpdatedAt: now,
            expectedCadenceSeconds: 300, staleAfterSeconds: 960, isStale: false,
            capacityIsDynamic: system == "docomo-cycle")
    }

    // ── まとめる ───────────────────────────────────────────────
    @Test("同じ座標の 2 系統は 1 つの印になる（入力の順のまま）")
    func samePlaceIsOneMarker() {
        let groups = MarkerGrouping.group([
            station("docomo-cycle", "00001"), station("hellocycling", "1001"),
        ])
        #expect(groups.count == 1)
        #expect(groups[0].isGroup)
        #expect(groups[0].members.map(\.systemID) == ["docomo-cycle", "hellocycling"])
        #expect(groups[0].anchor.systemID == "docomo-cycle")
    }

    @Test("22 m 離れたものはまとめない（いちばん拡大すれば押し分けられる）")
    func twentyTwoMetersStaySeparate() {
        let groups = MarkerGrouping.group([
            station("docomo-cycle", "00001"), station("hellocycling", "1001", east: 22),
        ])
        #expect(groups.map(\.members.count) == [1, 1])
    }

    @Test(
        "境目は 1 m（0.9 m はまとめ、1.1 m は分ける。南北でも東西でも）",
        arguments: [
            (0.9, 0.0, 1), (1.1, 0.0, 2), (0.0, 0.9, 1), (0.0, 1.1, 2), (0.6, 0.6, 1),
            (0.8, 0.8, 2),
        ])
    func oneMeterIsTheBoundary(north: Double, east: Double, expected: Int) {
        let groups = MarkerGrouping.group([
            station("a", "1"), station("b", "2", north: north, east: east),
        ])
        #expect(groups.count == expected)
    }

    @Test("まとまっていない印の id はポートの id のまま（同一性が変わらない）")
    func singleKeepsItsIdentity() {
        let alone = station("hellocycling", "1001", east: 50)
        let groups = MarkerGrouping.group([station("docomo-cycle", "00001"), alone])
        #expect(groups.last?.id == alone.id)
        #expect(groups.last?.isGroup == false)
    }

    @Test("鎖のようにつながる 3 つは 1 組（0.8 m ずつ）")
    func chainedStationsFormOneGroup() {
        let groups = MarkerGrouping.group([
            station("a", "1"), station("b", "2", east: 0.8), station("c", "3", east: 1.6),
        ])
        #expect(groups.count == 1)
        #expect(groups[0].members.map(\.stationID) == ["1", "2", "3"])
    }

    @Test("組の並びと顔ぶれは入力の並びで決まる（緯度の順に左右されない）")
    func orderFollowsTheInput() {
        let input = [
            station("a", "north", north: 300), station("b", "pair1"),
            station("c", "south", north: -300), station("d", "pair2", east: 0.5),
        ]
        let groups = MarkerGrouping.group(input)
        #expect(groups.map(\.anchor.stationID) == ["north", "pair1", "south"])
        #expect(groups[1].members.map(\.stationID) == ["pair1", "pair2"])
        #expect(MarkerGrouping.group(input).map(\.id) == groups.map(\.id), "同じ入力から同じ結果")
    }

    @Test("空の入力は空")
    func emptyIsEmpty() {
        #expect(MarkerGrouping.group([]).isEmpty)
    }

    // ── 表示上限 ─────────────────────────────────────────────────
    @Test("表示上限は印の数で数える（同じ場所の 2 つで枠を 2 つ使わない）")
    func limitCountsMarkers() {
        // **入力の順と近さの順を変えておく**（先頭から取るだけの誤りを見逃さない）
        let pairs = [200.0, 0.0, 100.0].enumerated().flatMap { index, north in
            [
                station("docomo-cycle", "d\(index)", north: north),
                station("hellocycling", "h\(index)", north: north),
            ]
        }
        let groups = MarkerGrouping.group(pairs)
        let kept = MarkerSelection.nearestGroups(
            groups, toLatitude: baseLatitude, longitude: baseLongitude, limit: 2)
        #expect(kept.count == 2)
        #expect(kept.flatMap(\.members).count == 4, "印 2 つでポート 4 つ")
        #expect(kept.map(\.anchor.stationID) == ["d1", "d2"], "中心に近い 2 組（入力の順のまま）")
    }

    @Test("上限以下なら組をそのまま返す")
    func underTheLimitKeepsAll() {
        let groups = MarkerGrouping.group([station("a", "1"), station("b", "2", north: 50)])
        let kept = MarkerSelection.nearestGroups(
            groups, toLatitude: baseLatitude, longitude: baseLongitude)
        #expect(kept.map(\.id) == groups.map(\.id))
    }

    // ── 一覧の行 ─────────────────────────────────────────────────
    @Test("到着を選んでいなければ、いまの台数と観測時刻（系統の名前つき）")
    func currentChoiceShowsCountAndAge() {
        let group = MarkerGrouping.group([
            station("docomo-cycle", "00001", bikes: 2), station("hellocycling", "1001", bikes: 0),
        ])[0]
        let feeds = [
            "docomo-cycle": feed("docomo-cycle", "ドコモ・バイクシェア"),
            "hellocycling": feed("hellocycling", "HELLO CYCLING"),
        ]
        let choices = MarkerChoice.list(
            group, feeds: feeds, intent: .borrow, arrival: nil, now: now)
        #expect(choices.map(\.systemName) == ["ドコモ・バイクシェア", "HELLO CYCLING"])
        #expect(choices[0].headline == "いま 借りられる 2・2 分前の観測")
        #expect(choices[1].headline == "いま 借りられる 0・2 分前の観測", "0 台は 0（「—」ではない）")
        #expect(choices.allSatisfy { $0.band == nil && $0.detail == nil })
    }

    @Test("到着を選んでいれば、その時刻の確率と帯（地図の色と同じ話）")
    func forecastChoiceShowsProbability() {
        let forecast = StationForecast(
            rentProbability: 0.83, returnProbability: 0.4, confidence: 3,
            baseObservedAt: now.addingTimeInterval(-180), modelVersion: "composite-v1-20261025")
        let one = station("hellocycling", "1001", forecast: forecast)
        let choice = MarkerChoice(
            station: one, feed: feed("hellocycling", "HELLO CYCLING"), intent: .borrow,
            arrival: now.addingTimeInterval(15 * 60), now: now, locale: ja, timeZone: jst)
        #expect(choice.headline == "借りられる可能性 85%")
        #expect(choice.detail == "14:18 到着")
        #expect(choice.band == .high)
    }

    @Test("予測が無いポートは「いまは予測できません」（灰色の扱い）")
    func missingForecastIsUnavailable() {
        let choice = MarkerChoice(
            station: station("docomo-cycle", "00001"), feed: feed("docomo-cycle", "ドコモ"),
            intent: .returnBike, arrival: now.addingTimeInterval(600), now: now)
        #expect(choice.headline == ForecastState.unavailableText)
        #expect(choice.band == nil)
    }

    @Test("フィードが無ければ系統は system_id で出す")
    func missingFeedFallsBackToSystemID() {
        let choice = MarkerChoice(
            station: station("docomo-cycle", "00001"), feed: nil, intent: .borrow,
            arrival: nil, now: now)
        #expect(choice.systemName == "docomo-cycle")
    }
}
