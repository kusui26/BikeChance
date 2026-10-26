import Foundation

/// ポート詳細の「直近 24 時間」（iOS-3。W7 プラン §4.9、開発プラン §9.2）。
///
/// 守る規則は 4 つ。**判断はすべてここに置き、View は並べるだけにする**（`StationDetail` と同じ）。
///   * **観測の無い時間は穴のまま**（サーバーが行を作らない。0 を作らない・棒をつながない）
///   * **実測値には観測の時刻を添える**（契約 43。最新の 1 時間を言う）
///   * **意図に合うほうだけ**（借りたいなら台数、返したいなら空き枠。予測と同じ作法。§9.3）
///   * **上限なしのポートは空き枠の数を出さない**（W7-05。上流の番兵 32767 を棒にしない）
public struct RecentHistory: Equatable, Sendable {
    /// 1 時間ぶんの棒。
    ///
    /// **平均 0 台の棒は高さが無く、画面では穴と見分けられない。** だから棒とは別に、
    /// 観測のあった時間の下に線を引く（`RecentHistoryChart`）。0 台は「線だけ」、
    /// 観測の無い時間は「線も無い」になる。
    public struct Bar: Equatable, Sendable, Identifiable {
        /// その時間の始まり。
        public let hourStart: Date
        /// その 1 時間の平均（台）。
        public let value: Double
        /// 「11:00〜12:00」
        public let hourText: String
        /// 「平均 7.7 台」
        public let valueText: String

        public var id: Date { hourStart }

        /// その時間の終わり（次の時間の始まり）。観測のあった時間の線はここまで引く。
        public var hourEnd: Date { hourStart.addingTimeInterval(RecentHistory.secondsPerHour) }
    }

    /// 縦軸の名前（「借りられる台数」）。
    public let title: String
    /// **時刻の順。観測の無い時間は入らない**（穴は穴のまま描く）。
    public let bars: [Bar]
    /// 横軸の始まり。**サーバーが切った 24 時間の始まり**（時間に切り上げたもの）。
    public let start: Date
    /// 横軸の終わり。最新の棒の終わりか、集計を待っている 1 時間の手前のどちらか遅いほう。
    public let end: Date
    /// 縦軸の目盛り（0 から、きりのよい整数の刻み）。**最後が縦軸の上端。**
    public let ticks: [Int]
    /// 「1 時間ごとの平均です。最新は 11:00〜12:00 の観測です。」（契約 43）
    public let footnote: String
}

extension RecentHistory {
    /// 応答が持つ幅（時間）。**サーバーの `RECENT_HOURS` と同じ**（`station-detail.ts`）。
    public static let windowHours = 24
    /// 末尾で、まだ集計されていなくてよい時間。集計は毎時 :12 に前の 1 時間を作るので
    /// （migration 0046）、**それより前に開くと最後の 1 時間は無い**。その 1 時間を
    /// 「観測が無かった」穴として描かない。
    public static let pendingHours = 1
    /// 1 時間も観測が無いとき。
    public static let emptyText = "直近 24 時間の観測がありません"
    /// 穴があるときに添える 1 行。**「棒の無い時間」とは書かない**——平均 0 台の時間も棒は見えない。
    public static let gapNote = "下の線が途切れた時間は、観測がありませんでした。"

    /// 欄の見出し。**意図で変わる**（借りたいなら台数、返したいなら空き枠）。
    public static func header(for intent: RideIntent) -> String {
        "直近 \(windowHours) 時間（\(intent.verb)台数）"
    }

    static let secondsPerHour: TimeInterval = 3600
    /// 縦軸の刻みの数の上限。多いと小さな画面で数字が重なる。
    private static let maxTickIntervals = 4
}

extension RecentHistory {
    /// 応答から作る。**1 時間も観測が無ければ nil。**
    init?(detail: StationDetailResponse, intent: RideIntent, locale: Locale, timeZone: TimeZone) {
        let bars = detail.recent
            .sorted { $0.hourStart < $1.hourStart }
            .map { Bar(hour: $0, intent: intent, locale: locale, timeZone: timeZone) }
        guard let first = bars.first, let latest = bars.last else { return nil }
        // **サーバーが送った棒は必ず横軸に入れる**（幅の計算が食い違っても、値を隠さない）
        let start = min(RecentHistory.windowStart(for: detail.generatedAt), first.hourStart)
        let end = max(latest.hourEnd, RecentHistory.settledEnd(for: detail.generatedAt))
        self.title = "\(intent.verb)台数"
        self.bars = bars
        self.start = start
        self.end = end
        self.ticks = RecentHistory.ticks(peak: bars.map(\.value).max() ?? 0)
        self.footnote = RecentHistory.footnote(
            latest: latest, hasGaps: bars.count < RecentHistory.slots(start, end))
    }

    /// 横軸の始まり。サーバーは `hour_start ≥ 応答の時刻 − 24 時間` を返すので、
    /// **その時刻を時間に切り上げたもの**が最初に来うる棒になる。
    static func windowStart(for generatedAt: Date) -> Date {
        let from = generatedAt.addingTimeInterval(-Double(windowHours) * secondsPerHour)
        let hours = (from.timeIntervalSince1970 / secondsPerHour).rounded(.up)
        return Date(timeIntervalSince1970: hours * secondsPerHour)
    }

    /// 集計が済んでいるはずの終わり。**いまの時間の始まりから `pendingHours` だけ手前。**
    /// 最新の棒がこれより前で終わっていれば、そのあいだは穴として描く（観測が途切れた）。
    static func settledEnd(for generatedAt: Date) -> Date {
        let hours = (generatedAt.timeIntervalSince1970 / secondsPerHour).rounded(.down)
        return Date(timeIntervalSince1970: (hours - Double(pendingHours)) * secondsPerHour)
    }

    /// 横軸に並ぶ時間の数。
    static func slots(_ start: Date, _ end: Date) -> Int {
        Int((end.timeIntervalSince(start) / secondsPerHour).rounded())
    }

    /// 縦軸の目盛り。**刻みは 1・2・5 × 10 の累乗**から、`maxTickIntervals` 以内に収まる最小のもの。
    /// 平均は小数になるが、目盛りは整数にする（「0.25 台」の目盛りは台数として読めない）。
    static func ticks(peak: Double) -> [Int] {
        let top = max(Int(peak.rounded(.up)), 1)
        let step = niceStep(atLeast: Double(top) / Double(maxTickIntervals))
        let intervals = Int((Double(top) / Double(step)).rounded(.up))
        return (0...intervals).map { $0 * step }
    }

    private static func niceStep(atLeast minimum: Double) -> Int {
        let candidates = (0...9).flatMap { power in
            [1, 2, 5].map { $0 * Int(pow(10, Double(power))) }
        }
        return candidates.first { Double($0) >= minimum } ?? candidates.last ?? 1
    }

    private static func footnote(latest: Bar, hasGaps: Bool) -> String {
        let lines = [
            "1 時間ごとの平均です。最新は \(latest.hourText) の観測です。",
            hasGaps ? gapNote : nil,
        ]
        return lines.compactMap { $0 }.joined()
    }
}

extension RecentHistory.Bar {
    /// 1 時間の実績を、意図に合うほうの 1 本の棒にする。
    init(hour: RecentHour, intent: RideIntent, locale: Locale, timeZone: TimeZone) {
        let value = intent == .borrow ? hour.bikesMean : hour.docksMean
        let end = hour.hourStart.addingTimeInterval(RecentHistory.secondsPerHour)
        let from = Arrival.clockText(hour.hourStart, locale: locale, timeZone: timeZone)
        let to = Arrival.clockText(end, locale: locale, timeZone: timeZone)
        let mean = value.formatted(.number.precision(.fractionLength(1)).locale(locale))
        self.init(
            hourStart: hour.hourStart, value: value, hourText: "\(from)〜\(to)",
            valueText: "平均 \(mean) 台")
    }
}

/// 直近 24 時間を出せるかどうか。
public enum RecentHistoryState: Equatable, Sendable {
    /// 1 時間も観測が無い。
    case empty(String)
    /// 上限なしのポートの空き枠。**数を出さず、理由だけを出す**（W7-05）。
    case unbounded(String)
    case available(RecentHistory)

    public var history: RecentHistory? {
        if case .available(let history) = self { return history }
        return nil
    }
}

extension RecentHistoryState {
    /// 詳細の応答から、意図に合うほうの直近 24 時間を組み立てる。
    ///
    /// **鮮度では隠さない。** これは「いまの値」ではなく、時刻の付いた過去の観測である
    /// （「鮮度を超えた値を現在値として出さない」は現在値の規則）。
    ///
    /// **上限なしの判定はサーバーの印だけを読む**（`isCapacityUnbounded`。数から計算し直さない）。
    /// 返したい人に上限なしのポートの空き枠を棒で見せると、32767 の棒の隣で
    /// ほかの時間が見えなくなるうえ、「返せる 32767 台」と読ませてしまう。
    public static func make(
        detail: StationDetailResponse,
        intent: RideIntent,
        locale: Locale = .current,
        timeZone: TimeZone = .current
    ) -> RecentHistoryState {
        if intent == .returnBike && detail.station.isCapacityUnbounded {
            return .unbounded(StationDetail.unboundedCapacityNote)
        }
        guard
            let history = RecentHistory(
                detail: detail, intent: intent, locale: locale, timeZone: timeZone)
        else { return .empty(RecentHistory.emptyText) }
        return .available(history)
    }
}
