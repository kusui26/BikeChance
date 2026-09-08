import Foundation

/// 地図の矩形。`/v1/stations` の `bbox` に対応する。
///
/// **規則の正は `packages/shared/src/bbox.ts`。** 同じ入力から同じ URL が出るように、
/// 量子化の刻みと丸めの向きをそちらと揃えてある（食い違うと CDN のキャッシュが効かず、
/// サーバーが返す実効矩形とも噛み合わなくなる）。
public struct Bbox: Equatable, Sendable {
    public let west: Double
    public let south: Double
    public let east: Double
    public let north: Double

    public init(west: Double, south: Double, east: Double, north: Double) {
        self.west = west
        self.south = south
        self.east = east
        self.north = north
    }

    /// 丸めの刻み（度）。東京では緯度 0.01° ≒ 1.11 km、経度 0.01° ≒ 0.90 km。
    ///
    /// **利用者の細かい位置をサーバーに送らないための刻みでもある**（CLAUDE.md §5）。
    public static let quantumDegrees = 0.01

    /// サーバーが受け付ける 1 辺の上限（度）。超えると 400 が返る。
    public static let serverMaxSpanDegrees = 0.5

    /// **こちらから要求してよい 1 辺の上限（度）。**
    ///
    /// サーバーの上限（0.5°）より厳しくしてある。実測で 0.4° 四方の東京は 6,649 ポートが
    /// 該当し、件数の上限 1,000 件を超えて 400 になる。0.1° なら最も混んだ地域でも
    /// 900 件前後で収まる（EDA #1 の密度）。**それでも超えることはある**ので、
    /// `tooManyStations` は必ず扱う。
    public static let requestMaxSpanDegrees = 0.1

    public var latSpan: Double { north - south }
    public var lonSpan: Double { east - west }

    /// 1 辺が要求してよい大きさに収まっているか。
    public var isRequestable: Bool {
        latSpan > 0 && lonSpan > 0
            && latSpan <= Bbox.requestMaxSpanDegrees
            && lonSpan <= Bbox.requestMaxSpanDegrees
    }

    /// 格子に**外側へ**丸める。要求した範囲は必ず結果の中に入る。
    ///
    /// 近い要求が同じ矩形に写るので、CDN のキャッシュが効く。サーバー側も同じ丸めを
    /// するため、応答の `bbox` はここで作った値と一致する。
    public func quantized(step: Double = Bbox.quantumDegrees) -> Bbox {
        Bbox(
            west: Bbox.snap(west, step: step, rule: .down),
            south: Bbox.snap(south, step: step, rule: .down),
            east: Bbox.snap(east, step: step, rule: .up),
            north: Bbox.snap(north, step: step, rule: .up)
        )
    }

    /// 中心を保ったまま 1 辺を上限まで縮める。地図を引きすぎたときに使う。
    ///
    /// **上限の内側なら何もしない。** 中心から計算し直すと浮動小数の端数が乗り、
    /// 同じ矩形なのに別の URL になってキャッシュが外れる。
    public func clampedToRequestable() -> Bbox {
        guard !isRequestable else { return self }
        let limit = Bbox.requestMaxSpanDegrees
        let centerLat = (north + south) / 2
        let centerLon = (east + west) / 2
        let halfLat = min(max(latSpan, 0), limit) / 2
        let halfLon = min(max(lonSpan, 0), limit) / 2
        return Bbox(
            west: (centerLon - halfLon).roundedToPlaces(9),
            south: (centerLat - halfLat).roundedToPlaces(9),
            east: (centerLon + halfLon).roundedToPlaces(9),
            north: (centerLat + halfLat).roundedToPlaces(9)
        )
    }

    public func contains(latitude: Double, longitude: Double) -> Bool {
        latitude >= south && latitude <= north && longitude >= west && longitude <= east
    }

    /// `west,south,east,north`。`/v1/stations` のクエリに載せる形。
    public var queryValue: String {
        [west, south, east, north].map(Bbox.format).joined(separator: ",")
    }

    // MARK: - 丸めの実装

    private enum Rule { case down, up }

    /// 量子の整数倍にそろえる。**端数を先に落としてから丸める**ので、同じ入力で同じ値になる。
    private static func snap(_ value: Double, step: Double, rule: Rule) -> Double {
        let scaled = (value / step).roundedToPlaces(9)
        let index = rule == .down ? scaled.rounded(.down) : scaled.rounded(.up)
        return (index * step).roundedToPlaces(6)
    }

    /// 浮動小数の端数を残さない表記。`139.76` を `139.76000000000001` にしない。
    private static func format(_ value: Double) -> String {
        let rounded = value.roundedToPlaces(6)
        return rounded == rounded.rounded() ? String(Int(rounded)) : String(rounded)
    }
}

extension Double {
    func roundedToPlaces(_ places: Int) -> Double {
        let factor = pow(10.0, Double(places))
        return (self * factor).rounded() / factor
    }
}
