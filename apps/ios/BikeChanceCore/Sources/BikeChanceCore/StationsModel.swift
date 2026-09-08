import Foundation
import Observation

/// 地図に出すポートの状態。
///
/// **画面の状態を 1 つの列挙で持つ。** 「読み込み中なのに前の結果も出ている」のような
/// 中途半端な組み合わせを型で作れなくするため。
@MainActor
@Observable
public final class StationsModel {
    public enum State: Equatable, Sendable {
        case idle
        case loading
        case loaded(StationsResponse)
        /// 地図を拡大してもらう必要がある（範囲が広すぎる／該当が多すぎる）。
        case needsZoom(String)
        case failed(String)
    }

    public private(set) var state: State = .idle
    private let client: V1Client
    /// 直前に要求した矩形。**同じ矩形なら投げ直さない**（地図の微動で無駄に叩かない）。
    private var lastRequested: Bbox?
    private var task: Task<Void, Never>?

    /// 範囲が広すぎるときの案内。**失敗ではなく操作の案内**として出す。
    public static let zoomInMessage = "地図を拡大すると、この範囲のポートを表示します。"

    public init(client: V1Client) {
        self.client = client
    }

    /// 表示中の矩形が変わったときに呼ぶ。
    ///
    /// 呼ばれる頻度が高いので、**丸めたあとで同じなら何もしない**。丸めの刻みは
    /// サーバーと同じ 0.01 度なので、地図を少し動かしただけでは再取得が起きない。
    public func viewportChanged(to bbox: Bbox) {
        guard bbox.latSpan > 0, bbox.lonSpan > 0 else { return }
        guard bbox.isRequestable else {
            task?.cancel()
            lastRequested = nil
            state = .needsZoom(StationsModel.zoomInMessage)
            return
        }
        let requested = bbox.quantized()
        guard requested != lastRequested else { return }
        lastRequested = requested
        reload(bbox: requested)
    }

    /// 明示的な再取得（引っぱって更新など）。
    public func reloadCurrent() {
        guard let lastRequested else { return }
        reload(bbox: lastRequested)
    }

    private func reload(bbox: Bbox) {
        task?.cancel()
        state = .loading
        task = Task { [client] in
            do {
                let response = try await client.stations(in: bbox)
                guard !Task.isCancelled else { return }
                state = .loaded(response)
            } catch let error as V1Error {
                guard !Task.isCancelled else { return }
                // 「多すぎる」は失敗ではなく操作の案内として出す
                state = error.needsNarrowerBbox ? .needsZoom(error.message) : .failed(error.message)
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled else { return }
                state = .failed("通信に失敗しました。")
            }
        }
    }
}
