import Foundation

/// 地図で 1 つの印にまとめるポートの組（W8 プランの W8-11、W7 プランの所見 232）。
///
/// **同じ場所に 2 つの系統のポートが立っている**（25 か所が 0 m・同じ名前）。印を 2 つ重ねると、
/// 上の 1 つしか押せず、下の系統のポートには地図から辿り着けなかった。
///
/// **まとめた印の色は、中のどれか 1 つの色にしない**（灰色＋数）。HELLO の自転車は HELLO にしか
/// 返せないので、2 つの確率を 1 色に混ぜると「どちらの話か」が分からない（開発プラン §9.3）。
public struct MarkerGroup: Identifiable, Hashable, Sendable {
    /// まとめたポート。**入力の順**（サーバーの `system_id, station_id`）。1 つ以上。
    public let members: [StationCurrent]

    /// **1 つだけならそのポートの id**（まとまっていない印は、いままでと同じ同一性を保つ）。
    public var id: String {
        members.count == 1 ? members[0].id : "group:" + members.map(\.id).joined(separator: "+")
    }

    /// 印を置く位置と名前に使うポート（先頭）。
    public var anchor: StationCurrent { members[0] }

    /// 2 つ以上をまとめた印か。
    public var isGroup: Bool { members.count > 1 }
}

/// 近すぎて押し分けられないポートを 1 つの印にまとめる。
///
/// **まとめるのは 1 m 以内だけ**（所見 232：重なる 25 か所は 0 m、次に近い 001.関内駅は 22 m で、
/// いちばん拡大すれば押し分けられる）。**並びは入力の順で安定**——同じ範囲を見ているのに
/// 印の並びが揺れると再描画が起きる（`MarkerSelection` と同じ考え）。
public enum MarkerGrouping {
    /// まとめる距離（m）。
    public static let mergeDistanceMeters = 1.0

    /// 緯度 1 度の長さ（m）。**1 m を測るだけ**なので、球の近似で足りる。
    static let metersPerDegree = 111_320.0

    /// まとめる。**どのポートもちょうど 1 つの組に入る**。組の並びは、組の先頭の入力の位置。
    public static func group(_ stations: [StationCurrent]) -> [MarkerGroup] {
        var roots = UnionFind(count: stations.count)
        for (left, right) in closePairs(stations) {
            roots.union(left, right)
        }
        let grouped = Dictionary(grouping: stations.indices) { roots.find($0) }
        return grouped.values
            .map { indices in indices.sorted() }
            .sorted { $0[0] < $1[0] }
            .map { indices in MarkerGroup(members: indices.map { stations[$0] }) }
    }

    /// 1 m 以内の組（添字）。**緯度で並べて、緯度の差が 1 m を越えたら打ち切る**。
    static func closePairs(_ stations: [StationCurrent]) -> [(Int, Int)] {
        let order = stations.indices.sorted { stations[$0].latitude < stations[$1].latitude }
        let latitudeWindow = mergeDistanceMeters / metersPerDegree
        var pairs: [(Int, Int)] = []
        for (position, left) in order.enumerated() {
            for right in order[(position + 1)...] {
                if stations[right].latitude - stations[left].latitude > latitudeWindow { break }
                if distanceMeters(stations[left], stations[right]) <= mergeDistanceMeters {
                    pairs.append((left, right))
                }
            }
        }
        return pairs
    }

    /// 2 点の距離（m）。**経度の縮みを補正する**（東京で経度 1 度は緯度 1 度の約 0.81 倍）。
    static func distanceMeters(_ a: StationCurrent, _ b: StationCurrent) -> Double {
        let latitude = (a.latitude + b.latitude) / 2 * .pi / 180
        let dLat = (b.latitude - a.latitude) * metersPerDegree
        let dLon = (b.longitude - a.longitude) * metersPerDegree * cos(latitude)
        return (dLat * dLat + dLon * dLon).squareRoot()
    }
}

/// 組を作るための素集合（経路圧縮だけ）。**このファイルの中でしか使わない。**
private struct UnionFind {
    private var parent: [Int]

    init(count: Int) { parent = Array(0..<count) }

    mutating func find(_ index: Int) -> Int {
        var root = index
        while parent[root] != root { root = parent[root] }
        var current = index
        while parent[current] != root {
            let next = parent[current]
            parent[current] = root
            current = next
        }
        return root
    }

    /// **小さい添字を根にする**（組の顔ぶれだけでなく、根も入力の順で決まる）。
    mutating func union(_ left: Int, _ right: Int) {
        let (a, b) = (find(left), find(right))
        if a == b { return }
        parent[max(a, b)] = min(a, b)
    }
}

extension MarkerSelection {
    /// 組を中心に近い順に `limit` 個まで。**表示上限は印の数で数える**——同じ場所の 2 つの
    /// ポートで枠を 2 つ使わない。上限以下なら入力の順のまま返す（`nearest` と同じ）。
    public static func nearestGroups(
        _ groups: [MarkerGroup],
        toLatitude latitude: Double,
        longitude: Double,
        limit: Int = displayLimit
    ) -> [MarkerGroup] {
        guard groups.count > limit else { return groups }
        let anchors = groups.map(\.anchor)
        let kept = Set(
            closest(anchors, to: Coordinate(latitude: latitude, longitude: longitude), limit: limit)
                .map(\.id))
        return groups.filter { kept.contains($0.anchor.id) }
    }
}
