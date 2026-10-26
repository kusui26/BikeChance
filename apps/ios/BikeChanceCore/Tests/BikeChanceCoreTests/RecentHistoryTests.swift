import Foundation
import Testing

@testable import BikeChanceCore

/// ポート詳細の「直近 24 時間」（`RecentHistory.swift`、iOS-3）。
///
/// **主題は「観測の無い時間を 0 にしないこと」と「番兵の数を棒にしないこと」。**
/// サーバーは観測の無い時間の行を作らない（0046）。端末が穴を 0 で埋めたり、
/// 前後の棒で埋めたりすると、「0 台だった」「ずっと 7 台だった」と読ませてしまう。
///
/// フィクスチャは本番の実応答：`station_detail.json`（2026-09-13 03:45Z。23 時間ぶん）と
/// `station_detail_unbounded.json`（2026-10-22 13:04Z。22 時間ぶん、空き枠は 32767）。
@Suite("直近 24 時間")
struct RecentHistoryTests {
    static let jst = TimeZone(identifier: "Asia/Tokyo")!
    static let ja = Locale(identifier: "ja_JP")

    static func date(_ text: String) -> Date { V1Client.parseISO8601(text)! }

    static func history(
        _ detail: StationDetailResponse, intent: RideIntent = .borrow
    ) throws -> RecentHistory {
        let state = RecentHistoryState.make(
            detail: detail, intent: intent, locale: ja, timeZone: jst)
        return try #require(state.history, "直近 24 時間が出なかった: \(state)")
    }

    /// `recent` だけを書き換えて読み直す（ほかは本番のまま）。
    static func recentChanged(
        _ name: String = "station_detail",
        _ change: (inout [[String: Any]]) -> Void
    ) throws -> StationDetailResponse {
        try ForecastTimelineTests.mutated(name) { object in
            var recent = try #require(object["recent"] as? [[String: Any]])
            change(&recent)
            object["recent"] = recent
        }
    }

    // MARK: - 観測の時刻

    @Test("**実測値には観測の時刻を添える**（契約 43。最新の 1 時間を言う）")
    func saysWhenTheLatestHourWasObserved() throws {
        let history = try Self.history(try ForecastTimelineTests.detail())
        // 最新の行は 02:00Z（＝ 11:00 JST）の 1 時間
        #expect(history.footnote == "1 時間ごとの平均です。最新は 11:00〜12:00 の観測です。")
    }

    @Test("**意図に合うほうだけ**（借りたいなら台数、返したいなら空き枠）")
    func followsTheIntent() throws {
        let detail = try ForecastTimelineTests.detail()
        let borrow = try Self.history(detail, intent: .borrow)
        let give = try Self.history(detail, intent: .returnBike)
        #expect(borrow.bars.map(\.value) == detail.recent.map(\.bikesMean))
        #expect(give.bars.map(\.value) == detail.recent.map(\.docksMean))
        #expect(borrow.title == "借りられる台数")
        #expect(give.title == "返せる台数")
    }

    @Test("見出しは意図で変わる")
    func headerFollowsTheIntent() {
        #expect(RecentHistory.header(for: .borrow) == "直近 24 時間（借りられる台数）")
        #expect(RecentHistory.header(for: .returnBike) == "直近 24 時間（返せる台数）")
    }

    @Test("**平均 0 台の時間も棒として持つ**（高さの無い棒。穴とは別。下の線で見分ける）")
    func keepsZeroHoursAsBars() throws {
        let detail = try ForecastTimelineTests.detail()
        // 返せる枠はこのポートでは 1 日じゅう 0（ドコモ 1。前提）
        #expect(detail.recent.allSatisfy { $0.docksMean == 0 })
        let history = try Self.history(detail, intent: .returnBike)
        #expect(history.bars.count == detail.recent.count, "0 台の時間を落とさない")
        #expect(history.bars.allSatisfy { $0.value == 0 })
        #expect(!history.footnote.contains(RecentHistory.gapNote), "0 台は穴ではない")
        let first = try #require(history.bars.first)
        #expect(first.hourEnd.timeIntervalSince(first.hourStart) == 3600)
    }

    @Test("**読み上げ**：時間は「11:00〜12:00」、値は小数 1 桁の平均")
    func describesEachBar() throws {
        let history = try Self.history(try ForecastTimelineTests.detail())
        let latest = try #require(history.bars.last)
        #expect(latest.hourText == "11:00〜12:00")
        // 7.72093 → 7.7（**平均なので小数のまま**。四捨五入して 8 台とは言わない）
        #expect(latest.valueText == "平均 7.7 台")
    }

    // MARK: - 横軸と穴

    @Test("**横軸はサーバーが切った 24 時間**（応答の 24 時間前を時間に切り上げた所から）")
    func axisSpansTheServerWindow() throws {
        let history = try Self.history(try ForecastTimelineTests.detail())
        // 応答は 09-13 03:45:10Z。24 時間前は 09-12 03:45:10Z → 04:00Z から
        #expect(history.start == Self.date("2026-09-12T04:00:00Z"))
        #expect(history.end == Self.date("2026-09-13T03:00:00Z"))
        #expect(RecentHistory.slots(history.start, history.end) == 23)
        #expect(history.bars.count == 23, "前提：この応答には穴が無い")
        #expect(!history.footnote.contains(RecentHistory.gapNote))
    }

    @Test("**観測の無い時間は穴のまま**（0 を作らない・横軸を詰めない）")
    func keepsGapsAsGaps() throws {
        let full = try Self.history(try ForecastTimelineTests.detail())
        let detail = try Self.recentChanged { $0.removeSubrange(8..<11) }
        let history = try Self.history(detail)
        #expect(history.bars.count == 20)
        #expect(history.start == full.start && history.end == full.end, "横軸は同じ幅のまま")
        let removed = Set(full.bars[8..<11].map(\.hourStart))
        #expect(history.bars.allSatisfy { !removed.contains($0.hourStart) }, "穴に棒を作らない")
        #expect(history.footnote.hasSuffix(RecentHistory.gapNote))
    }

    @Test("**初めの時間が欠けても、横軸は 24 時間の始まりから**（観測が無かったことを見せる）")
    func keepsLeadingGapsVisible() throws {
        let detail = try Self.recentChanged { $0.removeFirst(5) }
        let history = try Self.history(detail)
        #expect(history.start == Self.date("2026-09-12T04:00:00Z"))
        #expect(history.bars.first?.hourStart == Self.date("2026-09-12T09:00:00Z"))
        #expect(history.footnote.hasSuffix(RecentHistory.gapNote))
    }

    @Test("**集計を待っている最後の 1 時間は、穴として描かない**（実応答：13:04 に 12 時台はまだ無い）")
    func doesNotFlagThePendingHour() throws {
        let detail = try ForecastTimelineTests.detail("station_detail_unbounded")
        let history = try Self.history(detail, intent: .borrow)
        // 応答は 10-22 13:04:58Z。最新は 11:00Z の 1 時間で、12:00Z は毎時 :12 の集計の前
        #expect(history.bars.last?.hourStart == Self.date("2026-10-22T11:00:00Z"))
        #expect(history.end == Self.date("2026-10-22T12:00:00Z"))
        #expect(history.bars.count == RecentHistory.slots(history.start, history.end))
        #expect(!history.footnote.contains(RecentHistory.gapNote))
    }

    @Test("**観測が途切れたら、末尾は穴として描く**（最新の時刻も言う）")
    func showsATrailingGapWhenObservationsStopped() throws {
        // 最後の 4 時間（23:00Z〜02:00Z）が無い。最新は 22:00Z（＝ 7:00 JST）
        let detail = try Self.recentChanged { $0.removeLast(4) }
        let history = try Self.history(detail)
        #expect(history.bars.last?.hourStart == Self.date("2026-09-12T22:00:00Z"))
        // 終わりは「集計が済んでいるはず」の 02:00Z まで。そのあいだの 3 時間は穴
        #expect(history.end == Self.date("2026-09-13T02:00:00Z"))
        #expect(
            history.footnote
                == "1 時間ごとの平均です。最新は 7:00〜8:00 の観測です。\(RecentHistory.gapNote)")
    }

    @Test("**時刻の順に並べる**（サーバーの並びに頼らない）")
    func sortsBarsByTime() throws {
        let detail = try Self.recentChanged { $0.reverse() }
        let history = try Self.history(detail)
        let starts = history.bars.map(\.hourStart)
        #expect(starts == starts.sorted())
        #expect(history.footnote.contains("最新は 11:00〜12:00"))
    }

    @Test("**1 時間も観測が無ければ、そう言う**（空のグラフを出さない）")
    func saysSoWhenThereIsNothing() throws {
        let detail = try Self.recentChanged { $0.removeAll() }
        let state = RecentHistoryState.make(detail: detail, intent: .borrow)
        #expect(state == .empty(RecentHistory.emptyText))
    }

    // MARK: - 上限なし

    @Test("**上限なしのポートの空き枠は、数を出さない**（実応答 4161。空き枠は 32767）")
    func withholdsTheDocksOfAnUnboundedStation() throws {
        let detail = try ForecastTimelineTests.detail("station_detail_unbounded")
        #expect(detail.station.isCapacityUnbounded, "前提：サーバーが印を付けている")
        #expect(detail.recent.allSatisfy { $0.docksMean == 32767 }, "前提：番兵の数")
        let state = RecentHistoryState.make(detail: detail, intent: .returnBike)
        #expect(state == .unbounded(StationDetail.unboundedCapacityNote))
    }

    @Test("**上限なしのポートでも、借りられる台数は出す**（台数は本物の数）")
    func showsTheBikesOfAnUnboundedStation() throws {
        let detail = try ForecastTimelineTests.detail("station_detail_unbounded")
        let history = try Self.history(detail, intent: .borrow)
        #expect(history.bars.map(\.value) == detail.recent.map(\.bikesMean))
    }

    @Test("**判定はサーバーの印だけを読む**（印の無い古い応答では、数から計算し直さない）")
    func readsOnlyTheServersFlag() throws {
        let detail = try ForecastTimelineTests.mutated("station_detail_unbounded") { object in
            var station = try #require(object["station"] as? [String: Any])
            station["capacity_unbounded"] = nil
            object["station"] = station
        }
        #expect(!detail.station.isCapacityUnbounded)
        let history = try Self.history(detail, intent: .returnBike)
        #expect(history.bars.allSatisfy { $0.value == 32767 })
    }

    // MARK: - 縦軸

    @Test(
        "**縦軸の目盛りはきりのよい整数**（1・2・5 × 10 の累乗、4 刻み以内）",
        arguments: [
            (0.0, [0, 1]),
            (0.4, [0, 1]),
            (1.0, [0, 1]),
            (2.0, [0, 1, 2]),
            (3.2, [0, 1, 2, 3, 4]),
            (7.7, [0, 2, 4, 6, 8]),
            (9.0, [0, 5, 10]),
            (13.0, [0, 5, 10, 15]),
            (21.0, [0, 10, 20, 30]),
            (32767.0, [0, 10000, 20000, 30000, 40000]),
        ])
    func ticksAreRoundNumbers(peak: Double, ticks: [Int]) {
        #expect(RecentHistory.ticks(peak: peak) == ticks)
    }

    @Test("**縦軸の上端は、いちばん高い棒より低くならない**")
    func topCoversTheTallestBar() throws {
        let history = try Self.history(try ForecastTimelineTests.detail())
        let tallest = try #require(history.bars.map(\.value).max())
        let top = try #require(history.ticks.last)
        #expect(Double(top) >= tallest)
    }
}
