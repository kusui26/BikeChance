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

    /// **こちらから要求してよい、丸めた後の長辺（度）。** サーバーがポートで返す上限と
    /// 同じ値である（`packages/shared/src/cells.ts` の `CELL_STATION_MAX_SPAN_DEG`。W6 の契約 41）。
    ///
    /// 超えるとサーバーはポートの代わりに格子のセル（`aggregation: cell`）を返す。以前は
    /// 丸める前の 1 辺 0.1 度で、丸めると最大約 0.12 度になり、セルが返って読めずに失敗の帯を
    /// 出していた（W6 プランの所見 194）。**同じ値を両側の検査が書いている**ので、変えるときは
    /// サーバーと同時に直す。件数の上限（1,000 件）は、東京駅まわりの実測（0.10 度で 1,091 件、
    /// 0.05 度で 295 件）の間なので普通は超えないが、**超えることはある**ので
    /// `tooManyStations` は必ず扱う。
    public static let requestMaxSpanDegrees = 0.08

    /// `requestMaxSpanDegrees` を格子の数にしたもの（8）。**判定はこちらで行う。**
    public static let requestMaxSpanQuanta = gridIndex(requestMaxSpanDegrees)

    public var latSpan: Double { north - south }
    public var lonSpan: Double { east - west }

    /// 丸めた後の長辺（格子の数）。**格子の番号の差で数える。**
    ///
    /// 度の引き算で比べない。丸めた矩形でも、経度 139.84 − 139.76 は 0.0800000000000125 に
    /// なり、`<= 0.08` で比べると境目がずれる（W6 プランの所見 205）。サーバーも同じ理由で
    /// 格子の数で比べる。
    public var roundedLongSideQuanta: Int {
        let grid = quantized()
        let lat = Bbox.gridIndex(grid.north) - Bbox.gridIndex(grid.south)
        let lon = Bbox.gridIndex(grid.east) - Bbox.gridIndex(grid.west)
        return max(lat, lon)
    }

    /// 要求してよいか。**丸めた後の長辺が `requestMaxSpanQuanta` 以下**（W6 の契約 41）。
    public var isRequestable: Bool {
        latSpan > 0 && lonSpan > 0 && roundedLongSideQuanta <= Bbox.requestMaxSpanQuanta
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

    /// 中心を保ったまま、**丸めても上限に収まる大きさ**まで縮める。地図を引きすぎたときに使う。
    ///
    /// 縮める先は上限より 1 格子狭い（0.07 度）。外側へ丸めると両端で合わせて最大 1 格子
    /// 広がるので、**どこに置いても丸めた後の長辺が上限（8 格子）を超えない**。
    ///
    /// **上限の内側なら何もしない。** 中心から計算し直すと浮動小数の端数が乗り、
    /// 同じ矩形なのに別の URL になってキャッシュが外れる。
    public func clampedToRequestable() -> Bbox {
        guard !isRequestable else { return self }
        let limit = Bbox.requestMaxSpanDegrees - Bbox.quantumDegrees
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

    /// 格子の番号（`value` を刻みで割って丸めた整数）。**丸めた後の座標と、刻みの倍数にだけ使う。**
    private static func gridIndex(_ value: Double) -> Int {
        Int((value / quantumDegrees).rounded())
    }

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

/// 緯度経度の 1 点。**MapKit を `BikeChanceCore` に持ち込まないため**の、最小の座標型。
///
/// `CLLocationCoordinate2D` を使うと Core が CoreLocation に依存し、判断の置き場が
/// 「地図を立ち上げないとテストできないもの」に寄っていく。アプリ側で詰め替える。
public struct Coordinate: Equatable, Sendable {
    public let latitude: Double
    public let longitude: Double

    public init(latitude: Double, longitude: Double) {
        self.latitude = latitude
        self.longitude = longitude
    }

    /// この点を中心にした正方形の矩形（1 辺 `spanDegrees` 度）。
    ///
    /// 目的地の周りのポートを引くのに使う。**要求する前に量子化されるので**（`V1Client`）、
    /// 中心の細かい位置はそのままサーバーへは行かない（CLAUDE.md §5）。
    public func square(spanDegrees: Double) -> Bbox {
        let half = spanDegrees / 2
        return Bbox(
            west: longitude - half, south: latitude - half,
            east: longitude + half, north: latitude + half
        )
    }
}

extension StationCurrent {
    /// このポートの座標。
    public var coordinate: Coordinate {
        Coordinate(latitude: latitude, longitude: longitude)
    }
}
