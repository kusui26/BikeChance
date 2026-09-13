import Foundation

/// 地図に出すポートの選び方。
///
/// **表示上限は約 300**（開発プラン §9.1）。それを超えたときに何を捨てるかを決めるのが
/// ここで、答えは「画面の中心から遠いものから捨てる」。利用者が見ているのは中心だからで、
/// ランダムや ID 順で捨てると、同じ場所を見ているのに出たり消えたりする。
public enum MarkerSelection {
    /// 地図に同時に出す上限。これを超えると描画が重くなる（開発プラン §9.1）。
    public static let displayLimit = 300

    /// 中心に近い順に `limit` 件を返す。**並びは安定**（同じ入力なら同じ結果）。
    ///
    /// **上限を超えていなければ、そのまま返す。** 地図は全部描くので並べ替える意味が無く、
    /// 入力の順（サーバーが決めた `system_id, station_id`）のほうが再描画も少ない。
    /// **近い順に並んでいてほしいときは `closest` を使う。**
    public static func nearest(
        _ stations: [StationCurrent],
        toLatitude latitude: Double,
        longitude: Double,
        limit: Int = displayLimit
    ) -> [StationCurrent] {
        guard stations.count > limit else { return stations }
        return closest(
            stations, to: Coordinate(latitude: latitude, longitude: longitude), limit: limit)
    }

    /// 近い順に並べて `limit` 件。**必ず並べ替える。**
    ///
    /// 目的地の最寄りポートを選ばせるときに使う——**そこでは並びそのものが答え**なので、
    /// `nearest` の「上限を超えていなければそのまま」では足りない。
    public static func closest(
        _ stations: [StationCurrent], to point: Coordinate, limit: Int
    ) -> [StationCurrent] {
        stations
            .map {
                (station: $0, distance: squaredDistance($0, point.latitude, point.longitude))
            }
            // 距離が同じときは id で決める。並びが揺れると再描画が起きる
            .sorted { ($0.distance, $0.station.id) < ($1.distance, $1.station.id) }
            .prefix(limit)
            .map(\.station)
    }

    /// 緯度経度の二乗距離。**順位付けにしか使わない**ので平方根を取らない。
    ///
    /// 経度 1 度は緯度 1 度より短い（東京で約 0.81 倍）。この補正を入れないと、
    /// 東西に細長い範囲で選ばれ方が歪む。
    private static func squaredDistance(
        _ station: StationCurrent, _ latitude: Double, _ longitude: Double
    ) -> Double {
        let dLat = station.latitude - latitude
        let dLon = (station.longitude - longitude) * cos(latitude * .pi / 180)
        return dLat * dLat + dLon * dLon
    }
}
