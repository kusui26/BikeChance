import Foundation

/// 実測値に添える鮮度の表現。
///
/// **ODPT ガイドライン 2.1 と CLAUDE.md §2 の 7・8 の要求**：実測値には観測時刻を必ず添え、
/// ttl を超えた値を「現在値」として出さない。ここはその判断と文言を 1 か所にまとめる。
public enum Freshness: Equatable, Sendable {
    /// 観測時刻が分かっていて、鮮度も十分。
    case fresh(age: TimeInterval)
    /// 観測時刻は分かるが、期待周期に対して古い。
    case stale(age: TimeInterval)
    /// **最新のフィードにこのポートが現れなかった。** 値がいつのものか分からない。
    case unknown

    /// 画面に出す 1 行。**「いま」とは言わない。**
    public var label: String {
        switch self {
        case .fresh(let age): "\(Freshness.minutes(age)) 分前の観測"
        case .stale(let age): "\(Freshness.minutes(age)) 分前の観測（更新が滞っています）"
        case .unknown: "観測時刻が不明"
        }
    }

    /// 現在値として提示してよいか。false のときは値を薄く出すか隠す。
    public var isPresentable: Bool {
        if case .fresh = self { return true }
        return false
    }

    private static func minutes(_ age: TimeInterval) -> Int {
        max(0, Int((age / 60).rounded(.down)))
    }
}

extension StationCurrent {
    /// このポートの値の鮮度。
    ///
    /// 判定にはフィードの閾値（`stale_after_s`）を使う。**サーバーと同じ式**を端末で
    /// 再現できるよう、閾値そのものが応答に入っている（データ辞書 §5.2）。
    public func freshness(feed: FeedStatus?, now: Date) -> Freshness {
        guard isPresent, let observedAt else { return .unknown }
        let age = now.timeIntervalSince(observedAt)
        let limit = TimeInterval(feed?.staleAfterSeconds ?? 0)
        if feed == nil || age <= limit {
            return .fresh(age: max(0, age))
        }
        return .stale(age: max(0, age))
    }

    /// 「借りられるか」の見た目。**予測ではなく実測**であることを崩さない。
    ///
    /// `bikes` が未観測（null）なら判断できない。`isRenting` が false なら台数があっても借りられない。
    public var canRentNow: Bool? {
        guard let bikes, let isRenting else { return nil }
        return isRenting && bikes >= 1
    }

    /// 「返せるか」の見た目。
    public var canReturnNow: Bool? {
        guard let docks, let isReturning else { return nil }
        return isReturning && docks >= 1
    }
}

extension StationsResponse {
    /// `system_id` から鮮度を引く。ポートごとに毎回線形探索しないための索引。
    public func feedIndex() -> [String: FeedStatus] {
        Dictionary(feeds.map { ($0.systemID, $0) }, uniquingKeysWith: { first, _ in first })
    }
}
