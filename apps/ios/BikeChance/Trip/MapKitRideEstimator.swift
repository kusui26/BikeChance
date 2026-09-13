import BikeChanceCore
import Foundation
import MapKit

/// 端末の経路案内で自転車の所要を出す（開発プラン §9.6 の検証項目 1）。
///
/// **出せなければ nil。** そのときはサーバーが直線距離から概算し、画面が但し書きを出す
/// （`RideSummary.estimatedNote`）。**黙って直線距離に落とさない**のはここではなく
/// サーバーの仕事で、`ride_min_estimated` がどちらだったかを伝える。
///
/// **叩く回数を絞る。** `MKDirections` は**端末単位でスロットリング**され、超えると
/// しばらく何も返らなくなる（`MKError.loadingThrottled`）。`TripModel` は 1 回の
/// 行程につき 1 度しか呼ばない——代替候補ぶんは叩かない。
///
/// **応答の `transportType` は信用しない**（既知のバグ。開発プラン §9.1）。
/// 要求に `.cycling` を入れたうえで、返ってきた所要をそのまま使う。
struct MapKitRideEstimator: RideEstimating {
    /// これを超えたら諦める。**画面を待たせない**ための上限。
    static let timeoutSeconds: TimeInterval = 4

    func rideMinutes(from: Coordinate, to: Coordinate) async -> Int? {
        do {
            // **要求も応答も、この中だけで使い切る。** `MKDirections.Request` も
            // `ETAResponse` も `Sendable` ではないので、境界をまたがせると Swift 6 の
            // 並行性検査が弾く。またぐのは `Coordinate` と秒数だけにする
            let seconds = try await withTimeout(seconds: MapKitRideEstimator.timeoutSeconds) {
                let request = MKDirections.Request()
                request.source = MapKitRideEstimator.item(from)
                request.destination = MapKitRideEstimator.item(to)
                request.transportType = .cycling
                return try await MKDirections(request: request).calculateETA().expectedTravelTime
            }
            // **切り上げる。** 「14.2 分」を 14 分にすると、到着が早いほうへ寄る
            return max(0, Int((seconds / 60).rounded(.up)))
        } catch {
            // 断られた理由は画面に出さない。**出せなかった、で十分**
            // （サーバーの概算に落ちることは `ride_min_estimated` が伝える）
            return nil
        }
    }

    private static func item(_ point: Coordinate) -> MKMapItem {
        MKMapItem(
            location: CLLocation(latitude: point.latitude, longitude: point.longitude),
            address: nil)
    }
}

/// 時間切れを付ける。**`MKDirections` は自前のタイムアウトを持たない。**
private func withTimeout<T: Sendable>(
    seconds: TimeInterval, _ work: @escaping @Sendable () async throws -> T
) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask { try await work() }
        group.addTask {
            try await Task.sleep(for: .seconds(seconds))
            throw CancellationError()
        }
        defer { group.cancelAll() }
        guard let first = try await group.next() else { throw CancellationError() }
        return first
    }
}
