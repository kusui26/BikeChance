import Foundation
import Observation

/// ポート詳細の応答（`/v1/stations/{system}/{station_id}`）を取る（iOS-3）。
///
/// **地図の 1 行では足りないものだけ**をこの応答から出す——到着時刻ごとの予測と、直近 24 時間。
/// 台数と鮮度は地図の行のまま（ピンと同じ値。地図が 60 秒ごとに新しくする）。
///
/// **開くたびに 1 度取る。開いたまま叩き続けない**（お気に入りと同じ。応答は CDN に 60 秒留まる）。
/// `StationsModel` と同じく、状態を 1 つの列挙で持つ。
@MainActor
@Observable
public final class StationDetailModel {
    public enum State: Equatable, Sendable {
        case loading
        case loaded(StationDetailResponse)
        /// 取れず、前に取れた応答も無い。**理由をそのまま出す**（サーバーの `detail`）。
        case failed(String)
    }

    public private(set) var state: State = .loading

    /// 理由が分からない失敗。
    public static let failedText = "通信に失敗しました。"

    public init() {}

    /// 取りに行く。**画面の `.task` から呼ぶ**（画面が消えれば取り消される）。
    ///
    /// **前に取れた応答は、取れるまで出したままにする。取れなくても消さない**——予測には
    /// 基づく観測の時刻が、直近 24 時間には観測の時刻が付いていて、古さは値が自分で言う。
    /// 失敗の後に呼んだとき（「もう一度」）だけ、読み込み中に戻す。
    ///
    /// **取り消しは失敗として出さない**（画面が消えた・取り直しが始まった）。`URLSession` は
    /// 取り消しを `URLError.cancelled` で返すので、エラーの型ではなく `Task.isCancelled` で見る。
    public func load(client: V1Client, system: String, stationID: String) async {
        if case .failed = state { state = .loading }
        do {
            let response = try await client.stationDetail(system: system, stationID: stationID)
            guard !Task.isCancelled else { return }
            state = .loaded(response)
        } catch {
            guard !Task.isCancelled, !(error is CancellationError) else { return }
            if case .loaded = state { return }
            state = .failed((error as? V1Error)?.message ?? StationDetailModel.failedText)
        }
    }
}
