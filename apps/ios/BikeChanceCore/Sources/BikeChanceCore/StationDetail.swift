import Foundation

/// ポート詳細の画面に出す内容を組み立てる（W3 プラン §5.11）。
///
/// **判断はすべてここに置き、View は並べるだけにする。** シミュレータを立ち上げずに
/// テストできる形にしておかないと、「未観測を 0 と出していないか」のような、
/// 表示義務に直結する規則を機械で守れない。
///
/// 守る規則は 4 つ（CLAUDE.md §2 の 7・8、データ辞書 §2）。
///   * **未観測は「—」。0 台と区別する**
///   * **鮮度を超えた値は「現在値」として出さない**
///   * **停止中は台数があっても借りられない**ことを言葉で添える
///   * **ドコモの「容量」は固定のラック数ではない**ことを添える
public struct StationDetail: Equatable, Sendable {
    /// 値が分からないときの表示。**0 と混同させない。**
    public static let unknownValue = "—"

    public let name: String
    public let systemName: String
    public let freshness: Freshness
    /// 借りられる／返せる。画面の一番上に大きく出す。
    public let availability: [Availability]
    /// 容量・設置・座標など、観測ではない情報。
    public let facts: [Fact]
    /// そのポートのデータ提供元。**表示は義務**（CC BY 4.0）。
    public let credit: String?

    public init(station: StationCurrent, feed: FeedStatus?, attribution: Attribution?, now: Date) {
        let freshness = station.freshness(feed: feed, now: now)
        self.name = station.name ?? "（名称未取得）"
        self.systemName = feed?.displayName ?? station.systemID
        self.freshness = freshness
        self.availability = [
            Availability(
                title: "借りられる",
                value: Self.count(station.bikes, enabled: station.isRenting, freshness: freshness),
                caution: station.isRenting == false ? "貸出停止中" : nil
            ),
            Availability(
                title: "返せる",
                value: Self.count(
                    station.docks, enabled: station.isReturning, freshness: freshness),
                caution: station.isReturning == false ? "返却停止中" : nil
            ),
        ]
        self.facts = Self.facts(station: station, feed: feed)
        self.credit = attribution?.credit
    }

    /// 台数の表示。**鮮度が切れていれば値を出さない。停止中も出さない。**
    ///
    /// 停止中に「3」と出すと「3 台あるから借りられる」と読める。数を隠して
    /// `caution` に理由を書くほうが、誤解が少ない。
    private static func count(_ value: Int?, enabled: Bool?, freshness: Freshness) -> String {
        guard freshness.isPresentable, enabled != false, let value else { return unknownValue }
        return "\(value)"
    }

    private static func facts(station: StationCurrent, feed: FeedStatus?) -> [Fact] {
        [
            Fact(
                label: "容量",
                value: station.capacity.map(String.init) ?? unknownValue,
                // ドコモの `capacity` は `bikes + docks` の動的値（開発プラン §3.6）
                note: feed?.capacityIsDynamic == true && station.capacity != nil
                    ? "固定の台数ではなく、その時点の台数と空き枠の合計です。" : nil
            ),
            Fact(label: "設置", value: installation(station.isInstalled), note: nil),
            Fact(label: "座標", value: coordinate(station), note: nil),
            Fact(label: "ポート ID", value: "\(station.systemID) / \(station.stationID)", note: nil),
        ]
    }

    private static func installation(_ isInstalled: Bool?) -> String {
        switch isInstalled {
        case true?: "設置されています"
        case false?: "撤去・休止中"
        default: unknownValue
        }
    }

    /// 座標。**小数 6 桁**（約 0.1 m）。端末の位置情報ではなくポートの座標なので丸めない。
    private static func coordinate(_ station: StationCurrent) -> String {
        String(format: "%.6f, %.6f", station.latitude, station.longitude)
    }
}

/// 「借りられる」「返せる」の 1 つぶん。
public struct Availability: Equatable, Sendable, Identifiable {
    public let title: String
    public let value: String
    /// 停止中などの理由。**数を出さない代わりに理由を出す。**
    public let caution: String?

    public var id: String { title }
}

/// 観測ではない情報の 1 行。
public struct Fact: Equatable, Sendable, Identifiable {
    public let label: String
    public let value: String
    public let note: String?

    public var id: String { label }
}

extension StationsResponse {
    /// `system_id` からクレジットを引く。詳細画面はそのポートのぶんだけを出す。
    public func attributionIndex() -> [String: Attribution] {
        Dictionary(attribution.map { ($0.systemID, $0) }, uniquingKeysWith: { first, _ in first })
    }
}
