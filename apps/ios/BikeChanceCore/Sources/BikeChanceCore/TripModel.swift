import Foundation
import Observation

/// 2 点間の自転車の所要（分）を見積もるもの。
///
/// **`BikeChanceCore` は MapKit を持たない。** 経路案内は端末の機能で、判断の置き場を
/// 「地図を立ち上げないとテストできないもの」に寄せないために、口だけをここに置く
/// （`V1Client.Transport` と同じ差し替え方）。
///
/// **返せなければ nil。** そのときはサーバーが直線距離から概算する
/// （`ride_min_estimated` が true で返り、画面が但し書きを出す）。
public protocol RideEstimating: Sendable {
    func rideMinutes(from: Coordinate, to: Coordinate) async -> Int?
}

/// 見積もらない。**既定はこちら**——サーバーの概算に任せる。
public struct NoRideEstimate: RideEstimating {
    public init() {}

    public func rideMinutes(from: Coordinate, to: Coordinate) async -> Int? { nil }
}

/// 行程チェックの状態（W5 プラン §6.7）。
///
/// **`StationsModel` と同じ形にしてある**：画面の状態を 1 つの列挙で持ち、
/// 「読み込み中なのに前の結果も出ている」を型で作れなくする。
@MainActor
@Observable
public final class TripModel {
    public enum State: Equatable, Sendable {
        case idle
        case loading
        case loaded(TripCheckResponse)
        /// 断られた・届かなかった。**理由をそのまま出す**（サーバーの `detail`）。
        case failed(String)
    }

    public private(set) var state: State = .idle

    /// 何分後に出発するか。**「いま」は無い**——`/v1/trip-check` は 5 分以上先しか
    /// 受けない（`Arrival.minMinutes`）。丸めと範囲は `Arrival.gridded` が見る。
    public var departureMinutes: Int = Arrival.minMinutes {
        didSet { if departureMinutes != oldValue { state = .idle } }
    }

    /// ピッカーに並べる出発の目盛り。**最短（5 分）を先頭に置く。**
    ///
    /// **90 分を入れない**のは、「1 時間 30 分後」が横並びのピッカーで切れるから
    /// （実測：`1 時間 3…` になった）。**目盛りの粗さより、読めることを採る**——
    /// 到着の上限が 180 分なので、120 分から先は乗車時間がほとんど取れない。
    public static let departureChoices = [5, 15, 30, 60, 120]

    private let client: V1Client
    private let estimator: RideEstimating
    private let clock: @Sendable () -> Date
    private var task: Task<Void, Never>?

    /// - Parameters:
    ///   - estimator: 端末の経路案内。既定は使わない（サーバーが概算する）。
    ///   - clock: 現在時刻。**検査が時間を進められるように**差し替え口にする。
    public init(
        client: V1Client,
        estimator: RideEstimating = NoRideEstimate(),
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.client = client
        self.estimator = estimator
        self.clock = clock
    }

    /// 行程を調べる。
    ///
    /// **系統は出発ポートのもの 1 つ。** 到着ポートが別系統なら行程は成立しないので、
    /// 呼ぶ側が同じ系統から選ばせる（W4-21）。
    ///
    /// **端末の経路案内は先に 1 回だけ叩く。** `MKDirections` は端末単位で
    /// スロットリングされるので（開発プラン §9.6）、代替候補ぶんは叩かない——
    /// 候補の確率はサーバーが同じ時刻で出しており、そこに端末の所要は要らない。
    public func check(from: StationCurrent, to: StationCurrent) {
        task?.cancel()
        state = .loading
        let departAt = Arrival.gridded(
            clock().addingTimeInterval(TimeInterval(departureMinutes * 60)), now: clock())
        task = Task { [client, estimator] in
            let ride = await estimator.rideMinutes(from: from.coordinate, to: to.coordinate)
            guard !Task.isCancelled else { return }
            await load(from: from, to: to, departAt: departAt, ride: ride, client: client)
        }
    }

    /// 同じ条件で取り直す。**利用者の操作なので静かにはしない**（`loading` を出す）。
    public func reload(from: StationCurrent, to: StationCurrent) {
        check(from: from, to: to)
    }

    private func load(
        from: StationCurrent,
        to: StationCurrent,
        departAt: Date,
        ride: Int?,
        client: V1Client
    ) async {
        do {
            let response = try await client.tripCheck(
                system: from.systemID, from: from.stationID, to: to.stationID,
                departAt: departAt, rideMinutes: ride)
            guard !Task.isCancelled else { return }
            state = .loaded(response)
        } catch let error as V1Error {
            guard !Task.isCancelled else { return }
            // **断られた理由をそのまま出す。** 範囲外（`arrival_out_of_range`）のように、
            // 利用者が出発時刻を変えれば通るものがある——「通信に失敗しました」に
            // 畳むと、直し方が分からなくなる（§6.7 の完了条件 4）
            state = .failed(error.message)
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled else { return }
            state = .failed("通信に失敗しました。")
        }
    }

    /// 画面に出す形。**応答が無ければ nil。**
    public func plan(
        now: Date, locale: Locale = .current, timeZone: TimeZone = .current
    ) -> TripPlan? {
        guard case .loaded(let response) = state else { return nil }
        return TripPlan(response: response, now: now, locale: locale, timeZone: timeZone)
    }
}
