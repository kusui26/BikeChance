import Foundation

/// ポート詳細の「ふだんの傾向」（iOS-5。W9 プランの W9-06、開発プランの D-47）。
///
/// **予測ではない。** いまの台数を見ない過去 4 週の統計で、来週の水曜も再来週の水曜も同じ数に
/// なる。だから見出しは「ふだんの傾向」で、注記に**版の日付**と**予測ではないこと**を必ず添える
/// （CLAUDE.md §2 の 7）。
///
/// 守る規則は 5 つ。**判断はすべてここに置き、View は並べるだけにする**（`RecentHistory` と同じ）。
///   * **色は地図と同じ 3 段の帯**（`ProbabilityBand`）、丸めも同じ（`ForecastDisplay.percent`）。
///     読み方を 1 つにする
///   * **出せない時間は帯にしない**（灰色。0% と読ませない）
///   * **意図に合うほうだけ**（借りたいなら借りられる確率、返したいなら返せる確率）
///   * **過ぎた時間は薄く**（今日の、いまより前に終わった時間）
///   * **曜日と祝日の印**（祝日は「日曜祝日の種別で、日曜でない日」）
///
/// **時刻はすべて JST で数える。** 傾向は JST の 0〜23 時の表で、端末の時間帯で読み替えると
/// 別の時間の色になる。
public struct TrendHeatmap: Equatable, Sendable {
    /// 行の曜日の種類（色の付け分けに使う）。
    public enum DayKind: Equatable, Sendable {
        case weekday
        case saturday
        /// 日曜と祝日（サーバーの `sun_holiday`）。
        case sundayOrHoliday
    }

    /// 1 時間ぶん。
    public struct Cell: Equatable, Sendable, Identifiable {
        /// JST の時（0〜23）。
        public let hour: Int
        /// 5% 刻み・上限 99 の百分率。**nil は出せない時間**（灰色）。
        public let percent: Int?
        public let band: ProbabilityBand?
        /// その時間が終わっている（今日の、いまより前の時間）。**薄く描く。**
        public let isPast: Bool
        /// 押したときと読み上げの 1 文。「11月3日（火・祝）8時台　借りられる 90%（ほぼ大丈夫）」
        public let text: String

        public var id: Int { hour }
    }

    /// 1 日ぶん（7 行のうちの 1 つ）。
    public struct Row: Equatable, Sendable, Identifiable {
        /// JST の暦日（`YYYY-MM-DD`）。
        public let date: String
        /// 「今日」「明日」「11/5」
        public let dateText: String
        /// 「火・祝」「土」
        public let weekdayText: String
        public let kind: DayKind
        public let cells: [Cell]
        /// 読み上げ用の要約（帯ごとの時間の範囲）。**168 個のセルを 1 つずつ読ませない。**
        public let accessibilitySummary: String

        public var id: String { date }
    }

    /// 押されたセルを指す。
    public struct CellID: Hashable, Sendable {
        public let date: String
        public let hour: Int

        public init(date: String, hour: Int) {
            self.date = date
            self.hour = hour
        }
    }

    /// 欄の見出し（「ふだんの傾向（借りられる確率）」）。
    public let title: String
    public let rows: [Row]
    /// 版の日付と「予測ではない」を言う注記。**欄の下に必ず添える。**
    public let footnote: String
    /// 開いたときに選んでおくセル（**今日のいまの時間**）。今日の行が無ければ nil。
    public let current: CellID?

    /// 指したセル。
    public func cell(_ id: CellID) -> Cell? {
        rows.first { $0.date == id.date }?.cells.first { $0.hour == id.hour }
    }
}

extension TrendHeatmap {
    /// 傾向の時刻の時間帯。
    public static let timeZone = TimeZone(identifier: "Asia/Tokyo")!
    /// 横軸に目盛りを打つ時。
    public static let hourMarks = [0, 6, 12, 18]
    /// 出せない時間の言い方（灰色のセル）。
    public static let collectingText = "データを集めています"
    /// 欄ごと出せないとき（サーバーが返さない・その意図の時間が 1 つも無い）。**理由は言わない。**
    public static let unavailableText = "ふだんの傾向は、まだ出せません。"

    /// 欄の見出し。**出せないときも同じ見出しを出す**（何が出せないのかを言う）。
    public static func header(for intent: RideIntent) -> String {
        "ふだんの傾向（\(intent.verb)確率）"
    }

    static let hoursPerDay = 24
    static let secondsPerHour: TimeInterval = 3600
    static let weekdayNames = ["日", "月", "火", "水", "木", "金", "土"]
    /// `Calendar.weekday` の日曜。
    static let sunday = 1

    static var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        return calendar
    }
}

// MARK: - 組み立て

extension TrendHeatmap {
    /// 傾向から作る。**その意図で出せる時間が 1 つも無ければ nil**（欄ごと「まだ出せません」）。
    init?(trend: StationTrend, intent: RideIntent, now: Date) {
        let rows = trend.days.compactMap { Row(day: $0, intent: intent, now: now) }
        guard rows.contains(where: { row in row.cells.contains { $0.percent != nil } }) else {
            return nil
        }
        self.title = TrendHeatmap.header(for: intent)
        self.rows = rows
        self.footnote = TrendHeatmap.footnote(profileDate: trend.profileDate)
        self.current = TrendHeatmap.currentCell(in: rows, now: now)
    }

    /// 「11月2日までの 4 週の観測から作った、…」。**版の日付と、予測ではないことを言う。**
    static func footnote(profileDate: String) -> String {
        let since = day(of: profileDate).map(fullDateText) ?? profileDate
        return [
            "\(since)までの 4 週の観測から作った、曜日と時間ごとの「ふだん」です。",
            "いまの台数は見ていないので、予測ではありません。灰色の時間はデータを集めています。",
        ].joined()
    }

    /// 今日（JST）のいまの時間。**応答が日をまたいで古くても、無い行は指さない。**
    static func currentCell(in rows: [Row], now: Date) -> CellID? {
        let today = dateKey(now)
        let hour = calendar.component(.hour, from: now)
        guard rows.contains(where: { $0.date == today && $0.cells.contains { $0.hour == hour } })
        else { return nil }
        return CellID(date: today, hour: hour)
    }
}

extension TrendHeatmap.Row {
    /// 1 日ぶん。**日付が読めなければ行ごと落とす**（形の崩れた日を別の日として描かない）。
    init?(day: TrendDay, intent: RideIntent, now: Date) {
        guard let start = TrendHeatmap.day(of: day.date) else { return nil }
        let weekday = TrendHeatmap.calendar.component(.weekday, from: start)
        let kind = TrendHeatmap.kind(dowType: day.dowType)
        let weekdayText = TrendHeatmap.weekdayText(weekday: weekday, kind: kind)
        let label = "\(TrendHeatmap.fullDateText(start))（\(weekdayText)）"
        let values = intent == .borrow ? day.bike : day.dock
        let cells = values.prefix(TrendHeatmap.hoursPerDay).enumerated().map { hour, value in
            TrendHeatmap.Cell(
                hour: hour, probability: value, start: start, now: now, label: label,
                intent: intent)
        }
        self.init(
            date: day.date,
            dateText: TrendHeatmap.dateText(start, now: now),
            weekdayText: weekdayText,
            kind: kind,
            cells: cells,
            accessibilitySummary: TrendHeatmap.summary(label: label, cells: cells)
        )
    }
}

extension TrendHeatmap.Cell {
    init(hour: Int, probability: Double?, start: Date, now: Date, label: String, intent: RideIntent)
    {
        let percent = probability.map(ForecastDisplay.percent(of:))
        let band = percent.map(ProbabilityBand.of(percent:))
        let end = start.addingTimeInterval(Double(hour + 1) * TrendHeatmap.secondsPerHour)
        let value =
            percent.map { "\(intent.verb) \($0)%（\(band?.label ?? "")）" }
            ?? TrendHeatmap.collectingText
        self.init(
            hour: hour, percent: percent, band: band, isPast: end <= now,
            text: "\(label)\(hour)時台　\(value)")
    }
}

// MARK: - 暦（JST）

extension TrendHeatmap {
    /// `YYYY-MM-DD` を、その日の 0 時（JST）にする。**読めなければ nil。**
    static func day(of text: String) -> Date? {
        let parts = text.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        let components = DateComponents(year: parts[0], month: parts[1], day: parts[2])
        guard let date = calendar.date(from: components), dateKey(date) == text else { return nil }
        return date
    }

    /// JST の暦日（`YYYY-MM-DD`）。
    static func dateKey(_ date: Date) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    /// 「11月3日」
    static func fullDateText(_ date: Date) -> String {
        let parts = calendar.dateComponents([.month, .day], from: date)
        return "\(parts.month ?? 0)月\(parts.day ?? 0)日"
    }

    /// 行の左の日付。**今日と明日は言葉で**（JST で数える）。
    static func dateText(_ start: Date, now: Date) -> String {
        let today = calendar.startOfDay(for: now)
        let days = calendar.dateComponents([.day], from: today, to: start).day
        if days == 0 { return "今日" }
        if days == 1 { return "明日" }
        let parts = calendar.dateComponents([.month, .day], from: start)
        return "\(parts.month ?? 0)/\(parts.day ?? 0)"
    }

    /// サーバーの曜日種別。**知らない値は平日として読む**（色を付けないほうへ倒す）。
    static func kind(dowType: String) -> DayKind {
        switch dowType {
        case "sat": .saturday
        case "sun_holiday": .sundayOrHoliday
        default: .weekday
        }
    }

    /// 「火・祝」。**祝日は「日曜祝日の種別で、日曜でない日」**（土曜の祝日も「土・祝」）。
    static func weekdayText(weekday: Int, kind: DayKind) -> String {
        let name = weekdayNames[(weekday - 1) % weekdayNames.count]
        let isHoliday = kind == .sundayOrHoliday && weekday != sunday
        return isHoliday ? "\(name)・祝" : name
    }

    /// 読み上げの要約：「11月4日（水）、ほぼ大丈夫 10〜17時台、余裕を持って 9時台・18〜19時台、…」。
    /// **帯ごとに、その帯の時間を範囲で言う**（数だけでは、いつ借りやすいのかが分からない）。
    static func summary(label: String, cells: [Cell]) -> String {
        let runs = runs(of: cells)
        let bands: [ProbabilityBand?] = [.high, .medium, .low, nil]
        let parts = bands.compactMap { band -> String? in
            let hours = runs.filter { $0.band == band }.map(\.text)
            guard !hours.isEmpty else { return nil }
            return "\(band?.label ?? collectingText) \(hours.joined(separator: "・"))"
        }
        return ([label] + parts).joined(separator: "、")
    }

    /// 同じ帯が続く時間のまとまり（読み上げの範囲）。
    struct Run: Equatable {
        let band: ProbabilityBand?
        let first: Int
        let last: Int

        /// 「9時台」「10〜17時台」
        var text: String { first == last ? "\(first)時台" : "\(first)〜\(last)時台" }
    }

    /// 隣り合う時間で帯が同じものを 1 つにまとめる。
    static func runs(of cells: [Cell]) -> [Run] {
        cells.reduce(into: [Run]()) { runs, cell in
            if let previous = runs.last, previous.band == cell.band, previous.last == cell.hour - 1
            {
                runs[runs.count - 1] = Run(
                    band: previous.band, first: previous.first, last: cell.hour)
            } else {
                runs.append(Run(band: cell.band, first: cell.hour, last: cell.hour))
            }
        }
    }
}

/// ふだんの傾向を出せるかどうか。
public enum TrendHeatmapState: Equatable, Sendable {
    /// 出せない（サーバーが返さない・その意図の時間が 1 つも無い）。**欄は出して、そう言う。**
    case unavailable(String)
    case available(TrendHeatmap)

    public var heatmap: TrendHeatmap? {
        if case .available(let heatmap) = self { return heatmap }
        return nil
    }
}

extension TrendHeatmapState {
    /// 詳細の応答から、意図に合うほうの傾向を組み立てる。
    ///
    /// **フィードの鮮度では隠さない。** これは「いまの値」ではなく、版の日付の付いた過去 4 週の
    /// 統計である（「鮮度を超えた値を現在値として出さない」は現在値の規則）。古い版はサーバーが
    /// 返さない（`TREND_MAX_AGE_DAYS`）。
    public static func make(
        detail: StationDetailResponse, intent: RideIntent, now: Date
    ) -> TrendHeatmapState {
        guard let trend = detail.trend,
            let heatmap = TrendHeatmap(trend: trend, intent: intent, now: now)
        else { return .unavailable(TrendHeatmap.unavailableText) }
        return .available(heatmap)
    }
}
