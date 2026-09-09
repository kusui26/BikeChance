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

    /// いつ着くか。**既定は「いま」＝予測を頼まない**（応答の形も大きさも W2 のまま）。
    public private(set) var arrival: ArrivalChoice = .now

    /// 借りたいのか返したいのか。**確率が変わるだけで、要求は変わらない**
    /// （応答に両方の確率が入っている）。切り替えても取りに行かない。
    public var intent: RideIntent = .borrow

    private let client: V1Client
    private let clock: @Sendable () -> Date
    /// 直前に要求した矩形。**同じ矩形なら投げ直さない**（地図の微動で無駄に叩かない）。
    private var lastRequested: Bbox?
    /// 直前に**取りに行った**時刻。応答の `generated_at` ではなく要求の時刻で数える。
    /// CDN が返す応答は最大 3 分古いことがあり、それを基準にすると毎回投げ直してしまう。
    private var lastRequestedAt: Date?
    private var task: Task<Void, Never>?

    /// 範囲が広すぎるときの案内。**失敗ではなく操作の案内**として出す。
    public static let zoomInMessage = "地図を拡大すると、この範囲のポートを表示します。"

    /// 開いたまま置かれたときに取り直す間隔（秒）。
    ///
    /// **60 秒より短くしても新しい値は返らない。** `/v1` の CDN キャッシュが
    /// `s-maxage=60` なので、その内側の要求は同じ本文を返す（`V1_CACHE_CONTROL`）。
    /// 一方、いちばん厳しい鮮度の閾値はドコモの 303 秒なので、60 秒あれば
    /// 「更新が滞っています」に落ちる前に取り直せる。
    public static let refreshInterval: TimeInterval = 60

    /// - Parameter clock: 現在時刻。**検査が時間を進められるように**差し替え口にする。
    public init(client: V1Client, clock: @escaping @Sendable () -> Date = { Date() }) {
        self.client = client
        self.clock = clock
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

    /// 到着時刻が変わった。**URL が変わるので取り直す。**
    ///
    /// 利用者の操作なので**静かにはしない**（`loading` を出し、失敗も伝える）。地図の
    /// ピンは前の応答のまま残り、新しい応答が来たときに一度に切り替わる。
    public func select(arrival choice: ArrivalChoice) {
        guard choice != arrival else { return }
        arrival = choice
        reloadCurrent()
    }

    /// 画面を開いたまま置かれたときの自動再取得（W3 プラン §12 の 107）。
    ///
    /// **時刻を進めるだけでは値は新しくならない。** 地図を動かさない利用者には、
    /// データが古くなっていく様子だけが見えて、やがて全部が灰色になる。
    ///
    /// **静かに取り直す**：`loading` にせず、失敗しても前の値を消さない。
    /// 取れなければ表示はそのまま古くなり、**鮮度の表示がそれを伝える**。
    /// 一瞬の失敗で「通信に失敗しました」を出したり消したりするより、
    /// 「N 分前の観測」が伸びていくほうが正しく伝わる。
    ///
    /// - Returns: 実際に取りに行ったか。
    @discardableResult
    public func refreshIfDue(interval: TimeInterval = StationsModel.refreshInterval) -> Bool {
        switch state {
        // 出せている間と、失敗したあとだけ。`needsZoom` は拡大してもらうまで何度投げても同じで、
        // `loading` と `idle` はまだ最初の結果を待っている
        case .loaded, .failed: break
        case .idle, .loading, .needsZoom: return false
        }
        guard let lastRequested, let lastRequestedAt else { return false }
        guard clock().timeIntervalSince(lastRequestedAt) >= interval else { return false }
        reload(bbox: lastRequested, silently: true)
        return true
    }

    /// - Parameter silently: 自動の再取得か。真なら `loading` にせず、失敗も表示しない。
    private func reload(bbox: Bbox, silently: Bool = false) {
        task?.cancel()
        lastRequestedAt = clock()
        if !silently { state = .loading }
        // **到着時刻は投げる瞬間に決める。** 5 分の格子に丸めるので、時計が進めば
        // 次の格子に移る。開いたまま置かれても、予測は「いまから N 分後」を指し続ける
        let at = arrival.at(now: clock())
        task = Task { [client] in
            do {
                let response = try await client.stations(in: bbox, at: at)
                guard !Task.isCancelled else { return }
                state = .loaded(response)
            } catch let error as V1Error {
                guard !Task.isCancelled, !silently else { return }
                // 「多すぎる」は失敗ではなく操作の案内として出す
                state = error.needsNarrowerBbox ? .needsZoom(error.message) : .failed(error.message)
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled, !silently else { return }
                state = .failed("通信に失敗しました。")
            }
        }
    }
}
