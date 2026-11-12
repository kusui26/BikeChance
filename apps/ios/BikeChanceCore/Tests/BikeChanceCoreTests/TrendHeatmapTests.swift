import Foundation
import Testing

@testable import BikeChanceCore

/// ポート詳細の「ふだんの傾向」（`TrendHeatmap.swift`、iOS-5。W9-06、開発プランの D-47）。
///
/// **主題は「予測と読ませないこと」と「出せない時間を 0% にしないこと」。** あわせて、帯と丸めが
/// 地図と同じであること、曜日と祝日の印、過ぎた時間、時刻を JST で数えることを固定する。
///
/// フィクスチャは本番の実応答 `station_detail.json`（2026-09-13）に、**`trend` だけを足した**もの。
/// 足す 7 日は 11/3（火・文化の日）から：祝日・平日・土曜・日曜を含む（祝日と境目を作れる）。
/// **本番が返した `trend` そのもの**は `station_detail_trend.json`（2026-11-12）で、`ContractTests` が
/// 確かめる（出せない時間が null のまま届くこと・同じ曜日種別の日が同じ値であることなど）。
@Suite("ふだんの傾向")
struct TrendHeatmapTests {
    /// 11/3（火・祝）08:30 JST。
    static let now = V1Client.parseISO8601("2026-11-02T23:30:00Z")!

    static let days: [(date: String, dowType: String)] = [
        ("2026-11-03", "sun_holiday"),
        ("2026-11-04", "weekday"),
        ("2026-11-05", "weekday"),
        ("2026-11-06", "weekday"),
        ("2026-11-07", "sat"),
        ("2026-11-08", "sun_holiday"),
        ("2026-11-09", "weekday"),
    ]

    /// 借りられる確率：0〜5 時 0.95・6〜11 時 0.70・12〜17 時 0.40・18〜23 時は出せない。
    static func bike(_ hour: Int) -> Any {
        switch hour {
        case 0..<6: 0.95
        case 6..<12: 0.70
        case 12..<18: 0.40
        default: NSNull()
        }
    }

    static func trendObject(
        bike: (Int) -> Any = Self.bike, dock: (Int) -> Any = { _ in 0.9 },
        days: [(date: String, dowType: String)] = Self.days
    ) -> [String: Any] {
        [
            "profile_date": "2026-11-02",
            "days": days.map { day in
                [
                    "date": day.date, "dow_type": day.dowType,
                    "p_bike": (0..<24).map(bike), "p_dock": (0..<24).map(dock),
                ] as [String: Any]
            },
        ]
    }

    static func detail(_ trend: [String: Any] = Self.trendObject()) throws -> StationDetailResponse
    {
        try ForecastTimelineTests.mutated { object in object["trend"] = trend }
    }

    static func heatmap(
        _ detail: StationDetailResponse, intent: RideIntent = .borrow
    ) throws -> TrendHeatmap {
        let state = TrendHeatmapState.make(detail: detail, intent: intent, now: now)
        return try #require(state.heatmap, "傾向が出なかった: \(state)")
    }

    // MARK: - 読む

    @Test("`trend` を読める（日付は暦日の文字列のまま、出せない時間は nil）")
    func decodesTheTrend() throws {
        let trend = try #require(try Self.detail().trend)
        #expect(trend.profileDate == "2026-11-02")
        #expect(trend.days.count == 7)
        #expect(trend.days[0].date == "2026-11-03" && trend.days[0].dowType == "sun_holiday")
        #expect(trend.days[0].bike[0] == 0.95 && trend.days[0].bike[18] == nil)
    }

    @Test("**`trend` の無い応答も読める**（PR B より前のサーバー）。欄は「まだ出せません」")
    func oldResponsesHaveNoTrend() throws {
        let detail = try ForecastTimelineTests.detail()
        #expect(detail.trend == nil)
        let state = TrendHeatmapState.make(detail: detail, intent: .borrow, now: Self.now)
        #expect(state == .unavailable(TrendHeatmap.unavailableText))
    }

    // MARK: - 行（日と曜日）

    @Test("**今日（JST）から 7 日**。今日と明日は言葉で、曜日と祝日の印")
    func rowsAreTheSevenDays() throws {
        let rows = try Self.heatmap(try Self.detail()).rows
        #expect(rows.map(\.dateText) == ["今日", "明日", "11/5", "11/6", "11/7", "11/8", "11/9"])
        #expect(rows.map(\.weekdayText) == ["火・祝", "水", "木", "金", "土", "日", "月"])
        #expect(rows[0].kind == .sundayOrHoliday)
        #expect(rows[4].kind == .saturday)
        #expect(rows[5].kind == .sundayOrHoliday, "日曜は日曜祝日の種別（祝の印は付けない）")
        #expect(rows.allSatisfy { $0.cells.count == 24 })
    }

    @Test("**祝日の印は「日曜祝日の種別で、日曜でない日」**（土曜の祝日は「土・祝」）")
    func holidayMarks() {
        #expect(TrendHeatmap.weekdayText(weekday: 3, kind: .sundayOrHoliday) == "火・祝")
        #expect(TrendHeatmap.weekdayText(weekday: 7, kind: .sundayOrHoliday) == "土・祝")
        #expect(TrendHeatmap.weekdayText(weekday: 1, kind: .sundayOrHoliday) == "日")
        #expect(TrendHeatmap.weekdayText(weekday: 7, kind: .saturday) == "土")
    }

    @Test("**知らない曜日種別は平日として読む**（応答ごと読めなくしない）")
    func unknownDowTypeReadsAsWeekday() throws {
        var days = Self.days
        days[0] = ("2026-11-03", "festival")
        let rows = try Self.heatmap(try Self.detail(Self.trendObject(days: days))).rows
        #expect(rows[0].kind == .weekday && rows[0].weekdayText == "火")
    }

    @Test("**日付が読めない日は行ごと落とす**（別の日として描かない）")
    func brokenDateDropsTheRow() throws {
        var days = Self.days
        days[2] = ("2026-02-30", "weekday")
        let rows = try Self.heatmap(try Self.detail(Self.trendObject(days: days))).rows
        #expect(rows.count == 6)
        #expect(!rows.contains { $0.date == "2026-02-30" })
    }

    // MARK: - セル（帯・丸め・出せない時間）

    @Test("**色は地図と同じ 3 段の帯**、丸めも同じ（5% 刻み・上限 99）")
    func bandsAndRoundingMatchTheMap() throws {
        let cells = try Self.heatmap(try Self.detail()).rows[1].cells
        #expect(cells[0].percent == 95 && cells[0].band == .high)
        #expect(cells[6].percent == 70 && cells[6].band == .medium)
        #expect(cells[12].percent == 40 && cells[12].band == .low)
        let edges = try Self.heatmap(
            try Self.detail(Self.trendObject(bike: { $0 == 0 ? 0.84 : 0.999 }))
        ).rows[1].cells
        #expect(edges[0].percent == 85 && edges[0].band == .high, "丸めた後の数で帯を決める")
        #expect(edges[1].percent == 99, "100% とは言わない")
    }

    @Test("**出せない時間は帯にしない**（0% と読ませない）")
    func missingHoursHaveNoBand() throws {
        let cell = try Self.heatmap(try Self.detail()).rows[1].cells[18]
        #expect(cell.percent == nil && cell.band == nil)
        #expect(cell.text == "11月4日（水）18時台　データを集めています")
    }

    @Test("**意図に合うほうだけ**（返したいなら返せる確率）")
    func intentPicksTheProbability() throws {
        let heatmap = try Self.heatmap(try Self.detail(), intent: .returnBike)
        #expect(heatmap.title == "ふだんの傾向（返せる確率）")
        #expect(heatmap.rows[1].cells.allSatisfy { $0.percent == 90 })
        #expect(heatmap.rows[1].cells[0].text == "11月4日（水）0時台　返せる 90%（ほぼ大丈夫）")
    }

    @Test("**その意図で出せる時間が 1 つも無ければ、欄ごと「まだ出せません」**")
    func allMissingForTheIntentIsUnavailable() throws {
        let detail = try Self.detail(Self.trendObject(bike: { _ in NSNull() }))
        let borrow = TrendHeatmapState.make(detail: detail, intent: .borrow, now: Self.now)
        #expect(borrow == .unavailable(TrendHeatmap.unavailableText))
        #expect(
            TrendHeatmapState.make(detail: detail, intent: .returnBike, now: Self.now).heatmap
                != nil)
    }

    // MARK: - 時刻（JST）

    @Test("**過ぎた時間は薄く**：今日の、いまより前に終わった時間だけ")
    func pastHoursAreFaded() throws {
        let rows = try Self.heatmap(try Self.detail()).rows
        // いまは 08:30 JST。0〜7 時は終わっていて、8 時台は途中
        #expect(rows[0].cells.filter(\.isPast).map(\.hour) == Array(0..<8))
        #expect(rows[1].cells.filter(\.isPast).isEmpty)
    }

    @Test("開いたときは**今日のいまの時間**を選んでおく")
    func currentCellIsTodayNow() throws {
        let heatmap = try Self.heatmap(try Self.detail())
        let current = try #require(heatmap.current)
        #expect(current == TrendHeatmap.CellID(date: "2026-11-03", hour: 8))
        #expect(heatmap.cell(current)?.text == "11月3日（火・祝）8時台　借りられる 70%（余裕を持って）")
    }

    @Test("**JST の日付で数える**（端末の時間帯に依らない。UTC ではまだ 11/2 の時刻でも今日は 11/3）")
    func countsInJST() throws {
        // 11/3 00:30 JST ＝ 11/2 15:30 UTC
        let midnight = V1Client.parseISO8601("2026-11-02T15:30:00Z")!
        let state = TrendHeatmapState.make(
            detail: try Self.detail(), intent: .borrow, now: midnight)
        let heatmap = try #require(state.heatmap)
        #expect(heatmap.rows[0].dateText == "今日")
        #expect(heatmap.current == TrendHeatmap.CellID(date: "2026-11-03", hour: 0))
    }

    @Test("**応答が古くて今日の行が無ければ、どこも指さない**（11/3 からの 7 日を 11/10 に見る）")
    func aStaleResponseDoesNotPointAtMissingDays() throws {
        let nextMorning = V1Client.parseISO8601("2026-11-10T01:00:00Z")!
        let state = TrendHeatmapState.make(
            detail: try Self.detail(), intent: .borrow, now: nextMorning)
        let heatmap = try #require(state.heatmap)
        #expect(heatmap.current == nil)
        #expect(heatmap.rows.flatMap(\.cells).filter { !$0.isPast }.isEmpty)
    }

    // MARK: - 言葉

    @Test("**注記は版の日付と「予測ではない」**（W9-06、CLAUDE.md §2 の 7）")
    func footnoteSaysVersionAndNotAForecast() throws {
        let heatmap = try Self.heatmap(try Self.detail())
        #expect(heatmap.title == "ふだんの傾向（借りられる確率）")
        #expect(heatmap.footnote.contains("11月2日までの 4 週の観測"))
        #expect(heatmap.footnote.contains("予測ではありません"))
        #expect(!heatmap.title.contains("予測"), "見出しに「予測」と書かない")
    }

    @Test("読み上げは行ごとの要約（**帯ごとの時間の範囲**）。168 個を 1 つずつ読ませない")
    func accessibilitySummaryListsHourRanges() throws {
        let row = try Self.heatmap(try Self.detail()).rows[0]
        #expect(
            row.accessibilitySummary
                == "11月3日（火・祝）、ほぼ大丈夫 0〜5時台、余裕を持って 6〜11時台、別の手を 12〜17時台、データを集めています 18〜23時台"
        )
    }

    @Test("**途切れた帯は範囲を分けて言う**（1 時間だけの帯は「○時台」）")
    func accessibilitySummarySplitsBrokenRuns() throws {
        let bike: (Int) -> Any = { hour in hour == 1 ? 0.70 : (hour < 3 ? 0.95 : 0.40) }
        let row = try Self.heatmap(try Self.detail(Self.trendObject(bike: bike))).rows[1]
        #expect(
            row.accessibilitySummary == "11月4日（水）、ほぼ大丈夫 0時台・2時台、余裕を持って 1時台、別の手を 3〜23時台"
        )
    }
}
