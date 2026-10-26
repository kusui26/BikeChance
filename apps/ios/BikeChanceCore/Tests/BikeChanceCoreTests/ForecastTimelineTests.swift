import Foundation
import Testing

@testable import BikeChanceCore

/// ポート詳細の「到着時刻ごとの予測」（`ForecastTimeline.swift`、iOS-3）。
///
/// **主題は「在る点だけを、在る時刻に出すこと」。** 曲線は補間していない生の 10 点で、
/// 境目には段がある。点を作り足したり、過ぎた時刻の点を残したりすると、
/// 予測していない時刻の確率を画面が言ってしまう（W7 プラン §10.3）。
///
/// フィクスチャは本番の実応答：`station_detail.json`（2026-09-13、ドコモ 1）と
/// `station_detail_unbounded.json`（2026-10-22、ドコモ 4161。境目の段がある）。
@Suite("到着時刻ごとの予測")
struct ForecastTimelineTests {
    static let jst = TimeZone(identifier: "Asia/Tokyo")!
    static let ja = Locale(identifier: "ja_JP")

    static func detail(_ name: String = "station_detail") throws -> StationDetailResponse {
        try ContractTests.decode(StationDetailResponse.self, name)
    }

    /// 実応答を書き換えて読み直す。**書き換えたところ以外は本番のまま**にする。
    static func mutated(
        _ name: String = "station_detail",
        _ change: (inout [String: Any]) throws -> Void
    ) throws -> StationDetailResponse {
        let raw = try ContractTests.fixture(name)
        var object = try #require(try JSONSerialization.jsonObject(with: raw) as? [String: Any])
        try change(&object)
        return try V1Client.makeDecoder().decode(
            StationDetailResponse.self, from: try JSONSerialization.data(withJSONObject: object))
    }

    /// 曲線だけを書き換える。
    static func curveChanged(
        _ name: String = "station_detail",
        _ change: (inout [String: Any]) throws -> Void
    ) throws -> StationDetailResponse {
        try mutated(name) { object in
            var curve = try #require(object["forecast_curve"] as? [String: Any])
            try change(&curve)
            object["forecast_curve"] = curve
        }
    }

    static func timeline(
        _ detail: StationDetailResponse,
        intent: RideIntent = .borrow,
        selectedArrival: Date? = nil,
        now: Date? = nil
    ) throws -> ForecastTimeline {
        let state = ForecastTimelineState.make(
            detail: detail, intent: intent, selectedArrival: selectedArrival,
            now: now ?? detail.generatedAt, locale: ja, timeZone: jst)
        return try #require(state.timeline, "予測が出なかった: \(state)")
    }

    /// `station_detail.json` の曲線の起点（12:40 JST）。
    static let curveOrigin = V1Client.parseISO8601("2026-09-13T03:40:00.000Z")!

    /// 曲線の起点から `minutes` 分後。
    static func atCurve(_ minutes: Int) -> Date {
        curveOrigin.addingTimeInterval(TimeInterval(minutes * 60))
    }

    // MARK: - 在る点だけ

    @Test("前提：曲線の起点は `2026-09-13T03:40:00Z`、応答はその 5 分 10 秒後")
    func fixtureTimes() throws {
        let detail = try Self.detail()
        let curve = try #require(detail.forecastCurve)
        #expect(curve.generatedAt == Self.atCurve(0))
        #expect(detail.generatedAt.timeIntervalSince(curve.generatedAt) > 5 * 60)
        #expect(curve.horizonsMinutes == [5, 10, 15, 20, 30, 45, 60, 90, 120, 180])
    }

    @Test("**過ぎた到着の点は出さない**（実応答では 5 分後の点が、応答の時点でもう過ぎている）")
    func dropsPastArrivals() throws {
        let timeline = try Self.timeline(try Self.detail())
        // 12:45 到着の点は、応答（12:45:10）の時点で過ぎている
        #expect(timeline.points.count == 9)
        #expect(timeline.points.first?.display.arrival == "12:50 到着")
        #expect(timeline.points.last?.display.arrival == "15:40 到着")
    }

    @Test("**点は曲線の水平そのもの**（作り足さない・時刻を数え直さない。契約 16）")
    func pointsAreTheCurveItself() throws {
        let detail = try Self.detail("station_detail_unbounded")
        let curve = try #require(detail.forecastCurve)
        let timeline = try Self.timeline(detail)
        // この応答は起点の 4 分 58 秒後に作られていて、5 分後の点がまだ先にある
        #expect(timeline.points.count == curve.horizonsMinutes.count)
        let arrivals = curve.horizonsMinutes.indices.compactMap { curve.arrival(at: $0) }
        #expect(timeline.points.map(\.arrival) == arrivals)
    }

    @Test("**丸め・帯・言い回しは地図と同じ**（5% 刻み・上限 99%。`ForecastDisplay` を通る）")
    func usesTheSameRoundingAsTheMap() throws {
        let detail = try Self.detail()
        let curve = try #require(detail.forecastCurve)
        let timeline = try Self.timeline(detail)
        for point in timeline.points {
            let index = try #require(
                curve.horizonsMinutes.indices.first {
                    curve.arrival(at: $0) == point.arrival
                })
            let expected = ForecastDisplay(
                probability: curve.rentProbabilities[index], intent: .borrow,
                confidence: curve.confidence, arrival: point.arrival,
                basis: curve.baseObservedAt, locale: Self.ja, timeZone: Self.jst)
            #expect(point.display == expected)
        }
        // 1.0 は 100% と言わない。0.963 は 95%
        #expect(timeline.points.first?.display.percent == ForecastDisplay.maxPercent)
        #expect(timeline.points.last?.display.percent == 95)
    }

    @Test("**借りると返すで別の数**（返したいなら `p_dock` を読む）")
    func eachIntentReadsItsOwnValues() throws {
        let detail = try Self.detail()
        let borrow = try Self.timeline(detail, intent: .borrow)
        let give = try Self.timeline(detail, intent: .returnBike)
        #expect(borrow.title == "借りられる可能性")
        #expect(give.title == "返せる可能性")
        #expect(give.points.allSatisfy { $0.display.headline.hasPrefix("返せる可能性") })
        // 返せる確率は 0.015〜0.097。どれも「別の手を」
        #expect(give.points.allSatisfy { $0.display.band == .low })
        #expect(borrow.points.allSatisfy { $0.display.band == .high })
    }

    // MARK: - 時刻を添える

    @Test("**予測には、基づく観測の時刻を添える**（契約 43。`base_observed_at` の 12:39）")
    func carriesTheBasisTime() throws {
        let timeline = try Self.timeline(try Self.detail())
        #expect(timeline.basis == "12:39 時点の観測にもとづく予測")
    }

    @Test("**時計が進めば、過ぎた点から消える**")
    func dropsPointsAsTimePasses() throws {
        let detail = try Self.detail()
        // 13:05:10 JST。13:10 到着より前の点は全部過ぎた
        let later = detail.generatedAt.addingTimeInterval(20 * 60)
        let timeline = try Self.timeline(detail, now: later)
        #expect(timeline.points.map(\.display.arrival).first == "13:10 到着")
        #expect(timeline.points.count == 6)
    }

    @Test("**端末の時計が遅れていても、応答の時刻より前の点は出さない**")
    func neverShowsPointsBeforeTheResponse() throws {
        let detail = try Self.detail()
        let behind = detail.generatedAt.addingTimeInterval(-10 * 60)
        let timeline = try Self.timeline(detail, now: behind)
        #expect(timeline.points.count == 9, "12:45 の点は応答の時点で過ぎている")
    }

    @Test("**ちょうどその時刻の点も出さない**（到着が「いま」なら、それは現在値の話）")
    func dropsAPointExactlyAtNow() throws {
        let detail = try Self.detail()
        let timeline = try Self.timeline(detail, now: Self.atCurve(10))
        #expect(timeline.points.first?.arrival == Self.atCurve(15))
    }

    @Test("**全部の点が過ぎたら、出せないと言う**（古い応答を開いたまま 3 時間経った）")
    func saysUnavailableWhenEveryPointHasPassed() throws {
        let detail = try Self.detail()
        let state = ForecastTimelineState.make(
            detail: detail, intent: .borrow, now: Self.atCurve(180), locale: Self.ja,
            timeZone: Self.jst)
        #expect(state == .unavailable(ForecastState.unavailableText))
    }

    // MARK: - 出せないとき

    @Test("**フィードが滞っていれば出さない**（地図・お気に入りと同じ判断）")
    func hidesWhenTheFeedIsStale() throws {
        let detail = try Self.mutated { object in
            var feeds = try #require(object["feeds"] as? [[String: Any]])
            for index in feeds.indices { feeds[index]["stale"] = true }
            object["feeds"] = feeds
        }
        let state = ForecastTimelineState.make(
            detail: detail, intent: .borrow, now: detail.generatedAt)
        #expect(state == .unavailable(ForecastState.staleFeedText))
    }

    @Test("**別の系統のフィードが滞っていても、このポートの判断には使わない**")
    func readsOnlyThisSystemsFeed() throws {
        let detail = try Self.mutated { object in
            var feeds = try #require(object["feeds"] as? [[String: Any]])
            var other = try #require(feeds.first)
            other["system_id"] = "hellocycling"
            other["stale"] = true
            feeds.insert(other, at: 0)
            object["feeds"] = feeds
        }
        #expect(detail.feed?.systemID == "docomo-cycle")
        #expect(try Self.timeline(detail).points.isEmpty == false)
    }

    @Test("**曲線が無ければ出せない**（実応答：予測の無いポート）")
    func saysUnavailableWithoutACurve() throws {
        let detail = try Self.detail("station_detail_no_forecast")
        #expect(detail.forecastCurve == nil, "前提：サーバーが曲線を返していない")
        let state = ForecastTimelineState.make(
            detail: detail, intent: .borrow, now: detail.generatedAt)
        #expect(state == .unavailable(ForecastState.unavailableText))
    }

    @Test(
        "**長さのそろわない曲線は読まない**（見ていない側の配列が欠けていても）",
        arguments: ["p_bike", "p_dock", "horizons_min"])
    func refusesMismatchedLengths(key: String) throws {
        let detail = try Self.curveChanged { curve in
            var values = try #require(curve[key] as? [Any])
            values.removeLast()
            curve[key] = values
        }
        for intent in RideIntent.allCases {
            let state = ForecastTimelineState.make(
                detail: detail, intent: intent, now: detail.generatedAt)
            #expect(state == .unavailable(ForecastState.unavailableText))
        }
    }

    @Test(
        "**水平が時刻の順に並ばない曲線は読まない**（重複・逆順）",
        arguments: [
            [5, 10, 15, 20, 30, 45, 60, 90, 180, 120],
            [5, 10, 15, 20, 30, 30, 60, 90, 120, 180],
        ])
    func refusesUnorderedHorizons(horizons: [Int]) throws {
        let detail = try Self.curveChanged { $0["horizons_min"] = horizons }
        let state = ForecastTimelineState.make(
            detail: detail, intent: .borrow, now: detail.generatedAt)
        #expect(state == .unavailable(ForecastState.unavailableText))
    }

    // MARK: - 参考値・印・読み上げ

    @Test(
        "**確度が 1 以下なら参考値**（開発プラン §9.3）",
        arguments: [(0, true), (1, true), (2, false), (3, false)])
    func marksLowConfidenceAsReference(confidence: Int, isReference: Bool) throws {
        let detail = try Self.curveChanged { $0["confidence"] = confidence }
        #expect(try Self.timeline(detail).isReference == isReference)
    }

    /// 曲線の起点から `minutes` 分後を「選んだ到着」にしたときの印。nil なら選ばない。
    static func marker(at minutes: Int?) throws -> Date? {
        let selected = minutes.map(atCurve)
        return try timeline(try detail(), selectedArrival: selected).selectedArrival
    }

    @Test("**選んだ到着は、点の範囲に入るときだけ印を付ける**")
    func marksTheSelectedArrivalOnlyInsideThePoints() throws {
        // 点は 12:50（10 分）〜 15:40（180 分）
        #expect(try Self.marker(at: 50) == Self.atCurve(50))
        #expect(try Self.marker(at: 10) == Self.atCurve(10), "端の点ちょうどは範囲に入る")
        #expect(try Self.marker(at: 180) == Self.atCurve(180), "反対の端も入る")
        #expect(try Self.marker(at: 5) == nil, "過ぎた点の時刻には付けない")
        #expect(try Self.marker(at: 185) == nil)
        #expect(try Self.marker(at: nil) == nil)
    }

    @Test("**読み上げは「時刻 → 可能性 → 帯」**。参考値は点ごとに繰り返さない")
    func readsEachPointInOrder() throws {
        let detail = try Self.curveChanged { $0["confidence"] = 1 }
        let timeline = try Self.timeline(detail)
        #expect(timeline.isReference)
        #expect(
            timeline.points.first?.accessibilityLabel
                == "12:50 到着、借りられる可能性 99%、ほぼ大丈夫")
        #expect(timeline.points.allSatisfy { !$0.accessibilityLabel.contains("参考値") })
    }

    // MARK: - 見出しと目盛り

    @Test("見出しは意図で変わる")
    func headerFollowsTheIntent() {
        #expect(ForecastTimeline.header(for: .borrow) == "借りられる可能性（到着時刻ごと）")
        #expect(ForecastTimeline.header(for: .returnBike) == "返せる可能性（到着時刻ごと）")
    }

    @Test("**縦軸の目盛りは帯の境**（帯の判定と同じ数を読む）")
    func axisMarksTheBandEdges() {
        #expect(ForecastTimeline.axisPercents == [0, 60, 85, 100])
        let medium = ForecastTimeline.axisPercents[1]
        let high = ForecastTimeline.axisPercents[2]
        #expect(ProbabilityBand.of(percent: medium) == .medium)
        #expect(ProbabilityBand.of(percent: medium - 1) == .low)
        #expect(ProbabilityBand.of(percent: high) == .high)
        #expect(ProbabilityBand.of(percent: high - 1) == .medium)
    }
}
