import Foundation

/// 「いつ着くか」の選択と、それを `/v1` に送る形にする規則。
///
/// **相対の `in_min` ではなく、絶対の `at` を送る**（W4 プラン §12 の 114）。応答は CDN に
/// 最大 3 分留まりうるので、「30 分後」で頼むと、読んだときには 27 分後の確率になっている。
/// 絶対の時刻で頼めば、キャッシュから返っても指している到着は動かない。
///
/// **5 分の格子に丸める。** 同じ時間帯の利用者が同じ URL を叩き、CDN が効くようにするため
/// （サーバーも `in_min` を 5 分に丸める。`packages/shared/src/forecast.ts`）。
public enum Arrival {
    /// 丸めの刻み（分）。サーバーの `ARRIVAL_STEP_MIN` と同じ。
    public static let stepMinutes = 5
    /// 受け付けてもらえる最短（分）。**これより手前は 400 になる。**
    public static let minMinutes = 5
    /// 受け付けてもらえる最長（分）。水平の端（`HORIZONS_MIN` の最後）。
    public static let maxMinutes = 180

    /// 5 分の格子に丸め、サーバーが受ける範囲に収める。
    ///
    /// **収めるときも格子の上に置く。** 端で格子を外すと、利用者ごとに URL がばらけて
    /// CDN が効かなくなる。丸めの誤差は刻みの半分（2.5 分）なので、1 段ずらせば必ず入る。
    public static func gridded(_ target: Date, now: Date) -> Date {
        let step = TimeInterval(stepMinutes * 60)
        let earliest = now.timeIntervalSince1970 + TimeInterval(minMinutes * 60)
        let latest = now.timeIntervalSince1970 + TimeInterval(maxMinutes * 60)
        var seconds = (target.timeIntervalSince1970 / step).rounded() * step
        if seconds > latest { seconds -= step }
        if seconds < earliest { seconds += step }
        return Date(timeIntervalSince1970: seconds)
    }

    /// 到着時刻の表示。**「約 30 分後」ではなく時刻で書く**（W4 プラン §12 の 114）。
    ///
    /// 端末の設定に従う（24 時間表記かどうかは利用者の設定）。検査では固定の暦を渡す。
    public static func clockText(
        _ date: Date, locale: Locale = .current, timeZone: TimeZone = .current
    ) -> String {
        date.formatted(
            Date.FormatStyle(date: .omitted, time: .shortened, locale: locale, timeZone: timeZone)
        )
    }
}

/// 到着時刻の選択肢。
///
/// **既定は「いま」で、そのときは予測を頼まない。** 「いまの確率」は現在値そのものであって
/// 予測ではない（W4-02）。頼まなければ応答の形も大きさも W2 のままになる。
public enum ArrivalChoice: Equatable, Hashable, Sendable, Identifiable {
    case now
    /// いまから N 分後に着く。
    case later(minutes: Int)

    /// ピッカーに並べる順。**「いま」を先頭に置く**（既定であり、いちばん使う）。
    public static let choices: [ArrivalChoice] = [
        .now, .later(minutes: 15), .later(minutes: 30), .later(minutes: 60),
        .later(minutes: 90), .later(minutes: 120), .later(minutes: 180),
    ]

    public var id: Int {
        switch self {
        case .now: 0
        case .later(let minutes): minutes
        }
    }

    /// 予測を頼むか。`.now` は頼まない。
    public var wantsForecast: Bool {
        if case .later = self { return true }
        return false
    }

    /// `/v1/stations?at=` に載せる時刻。**`.now` なら送らない。**
    public func at(now: Date) -> Date? {
        guard case .later(let minutes) = self else { return nil }
        return Arrival.gridded(now.addingTimeInterval(TimeInterval(minutes * 60)), now: now)
    }

    /// ピッカーに出す短い名前。
    public var pickerLabel: String {
        guard case .later(let minutes) = self else { return "いま" }
        let (hours, rest) = (minutes / 60, minutes % 60)
        switch (hours, rest) {
        case (0, let rest): return "\(rest) 分後"
        case (let hours, 0): return "\(hours) 時間後"
        case (let hours, let rest): return "\(hours) 時間 \(rest) 分後"
        }
    }

    /// 選択中に画面へ出す 1 行。**時刻で書く**（丸めた後の到着が分かる）。
    public func label(now: Date, locale: Locale = .current, timeZone: TimeZone = .current)
        -> String
    {
        guard let at = at(now: now) else { return "いまの空き" }
        return "\(Arrival.clockText(at, locale: locale, timeZone: timeZone)) 到着"
    }
}
