import Foundation

/// ポート詳細の画面に出す内容を組み立てる（W3 プラン §5.11）。
///
/// **判断はすべてここに置き、View は並べるだけにする。** シミュレータを立ち上げずに
/// テストできる形にしておかないと、「未観測を 0 と出していないか」のような、
/// 表示義務に直結する規則を機械で守れない。
///
/// 守る規則は 5 つ（CLAUDE.md §2 の 7・8、データ辞書 §2）。
///   * **未観測は「—」。0 台と区別する**
///   * **鮮度を超えた値は「現在値」として出さない**
///   * **停止中は台数があっても借りられない**ことを言葉で添える
///   * **ドコモの「容量」は固定のラック数ではない**ので、**数を出さない**
///   * **予測と現在値を混ぜない。** 予測は別の欄に置き、出せなければ台数だけを出す
public struct StationDetail: Equatable, Sendable {
    /// 値が分からないときの表示。**0 と混同させない。**
    public static let unknownValue = "—"

    public let name: String
    public let systemName: String
    public let freshness: Freshness
    /// 借りられる／返せる。画面の一番上に大きく出す。
    public let availability: [Availability]
    /// 到着時刻の予測（W4 の PR B）。**現在値とは別の情報**なので、欄を分ける。
    public let forecast: ForecastState
    /// 容量・設置・座標など、観測ではない情報。
    public let facts: [Fact]
    /// そのポートのデータ提供元。**表示は義務**（CC BY 4.0）。
    public let credit: String?

    /// - Parameters:
    ///   - intent: 借りたいのか返したいのか。**予測はどちらか片方だけを出す。**
    ///   - arrival: **応答が指している到着時刻**（`StationsResponse.forecastArrival`）。
    ///     端末の時計から計算し直さない（W4 プラン §12 の 114）。nil なら予測を出さない。
    public init(
        station: StationCurrent,
        feed: FeedStatus?,
        attribution: Attribution?,
        intent: RideIntent = .borrow,
        arrival: Date? = nil,
        now: Date
    ) {
        let freshness = station.freshness(feed: feed, now: now)
        self.name = station.name ?? "（名称未取得）"
        self.systemName = feed?.displayName ?? station.systemID
        self.freshness = freshness
        self.availability = RideIntent.allCases.map { intent in
            Availability(
                title: intent.verb,
                value: station.countText(for: intent, freshness: freshness),
                caution: station.stopNote(for: intent)
            )
        }
        self.forecast = ForecastState.make(
            station: station, feed: feed, intent: intent, arrival: arrival)
        self.facts = Self.facts(station: station, feed: feed)
        self.credit = attribution?.credit
    }

    /// 動的な容量のシステムに添える説明。**「—」の理由を言う。**
    public static let dynamicCapacityNote =
        "この事業者は固定のラック数を公開していないため、表示していません。いまの受け入れ枠は上の 2 つの数の合計です。"

    /// 容量の表示。**容量が動的なシステムでは数を出さない**（W4 プラン §12 の 115）。
    ///
    /// ドコモの `capacity` は**日次同期の瞬間の `bikes + docks` が凍結された値**で、
    /// ラック数ではない（データ辞書 §4.3）。出すと「容量 5・借りられる 12」のように、
    /// **同じ画面の 2 つの数が矛盾する**（本番で 628 ポート・ドコモの 10.8%）。
    ///
    /// **サーバー（migration 0035）も NULL を返すが、ここでも受け取らない。** CDN に
    /// 残った古い応答や、将来の別の経路が数を持ってきても、画面には出さない。
    private static func capacity(_ value: Int?, feed: FeedStatus?) -> String {
        guard feed?.capacityIsDynamic != true, let value else { return unknownValue }
        return "\(value)"
    }

    private static func facts(station: StationCurrent, feed: FeedStatus?) -> [Fact] {
        [
            Fact(
                label: "容量",
                value: capacity(station.capacity, feed: feed),
                note: feed?.capacityIsDynamic == true ? dynamicCapacityNote : nil
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

extension StationCurrent {
    /// その意図に対応する実測の台数の表示（借りたいなら自転車、返したいなら空き）。
    ///
    /// **鮮度が切れていれば値を出さない。停止中も出さない。**
    /// 停止中に「3」と出すと「3 台あるから借りられる」と読める。数を隠して理由
    /// （`stopNote`）を出すほうが、誤解が少ない。
    ///
    /// **詳細画面と行程チェックが同じ実装を通る。** この規則（未観測を 0 と区別する・
    /// 鮮度切れを現在値として出さない）は表示義務に直結していて、2 か所に別々に
    /// 書くと片方だけ直したときに静かに食い違う（W5 §12 の 142 と同じ形）。
    public func countText(for intent: RideIntent, freshness: Freshness) -> String {
        let value = intent == .borrow ? bikes : docks
        let enabled = intent == .borrow ? isRenting : isReturning
        guard freshness.isPresentable, enabled != false, let value else {
            return StationDetail.unknownValue
        }
        return "\(value)"
    }

    /// 止まっている理由。**数を出さない代わりに出す。** 止まっていなければ nil。
    public func stopNote(for intent: RideIntent) -> String? {
        switch intent {
        case .borrow: isRenting == false ? "貸出停止中" : nil
        case .returnBike: isReturning == false ? "返却停止中" : nil
        }
    }
}
