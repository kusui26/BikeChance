import Foundation
import Testing

@testable import BikeChanceCore

/// 表示上限を超えたときに何を捨てるか。
///
/// **同じ場所を見ているのに出たり消えたりしない**ことが要点。ランダムや ID 順で捨てると
/// 再描画のたびに違うポートが消える。
@Suite("表示するマーカーの選び方")
struct MarkerSelectionTests {
    func station(_ id: String, lat: Double, lon: Double) -> StationCurrent {
        StationCurrent(
            systemID: "t", stationID: id, name: id, latitude: lat, longitude: lon,
            capacity: 10, capacityEstimate: 12, capacityDays: 7,
            bikes: 1, docks: 1, isInstalled: true, isRenting: true,
            isReturning: true, isPresent: true, observedAt: nil,
            lastChangedAt: Date(timeIntervalSince1970: 0), forecast: nil
        )
    }

    @Test("上限以下なら何も捨てない（順番も変えない）")
    func keepsEverythingUnderTheLimit() {
        let stations = (0..<10).map {
            station("s\($0)", lat: 35.0 + Double($0) * 0.001, lon: 139.0)
        }
        let selected = MarkerSelection.nearest(stations, toLatitude: 35.0, longitude: 139.0)
        #expect(selected.map(\.id) == stations.map(\.id))
    }

    @Test("上限を超えたら中心に近いものを残す")
    func keepsTheNearest() {
        let stations = (0..<10).map { station("s\($0)", lat: 35.0 + Double($0) * 0.01, lon: 139.0) }
        let selected = MarkerSelection.nearest(
            stations, toLatitude: 35.0, longitude: 139.0, limit: 3)
        #expect(selected.map(\.stationID) == ["s0", "s1", "s2"])
    }

    @Test("経度の縮みを補正する（東西に細長い範囲で歪まない）")
    func correctsForLongitudeCompression() {
        // 緯度 35 度では経度 1 度は緯度 1 度の約 0.82 倍の距離。
        // 補正が無いと「経度 0.012 度」より「緯度 0.011 度」を近いと誤判定する
        let byLatitude = station("lat", lat: 35.011, lon: 139.0)
        let byLongitude = station("lon", lat: 35.0, lon: 139.012)
        let selected = MarkerSelection.nearest(
            [byLatitude, byLongitude], toLatitude: 35.0, longitude: 139.0, limit: 1)
        #expect(selected.map(\.stationID) == ["lon"])
    }

    @Test("同じ入力からは同じ結果（再描画で揺れない）")
    func selectionIsStable() {
        let stations = (0..<20).map {
            station("s\($0)", lat: 35.0, lon: 139.0 + Double($0) * 0.001)
        }
        let first = MarkerSelection.nearest(stations, toLatitude: 35.0, longitude: 139.0, limit: 5)
        let second = MarkerSelection.nearest(
            stations.reversed(), toLatitude: 35.0, longitude: 139.0, limit: 5)
        #expect(first.map(\.id) == second.map(\.id))
    }

    @Test("距離が同じでも順番が決まる")
    func tiesAreBrokenDeterministically() {
        let east = station("b", lat: 35.0, lon: 139.01)
        let west = station("a", lat: 35.0, lon: 138.99)
        let selected = MarkerSelection.nearest(
            [east, west], toLatitude: 35.0, longitude: 139.0, limit: 1)
        #expect(selected.map(\.stationID) == ["a"])
    }

    @Test("上限は 300（開発プラン §9.1）")
    func theLimitIsThreeHundred() {
        #expect(MarkerSelection.displayLimit == 300)
    }
}
