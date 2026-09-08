import Foundation
import Testing

@testable import BikeChanceCore

/// 画面の状態機械。**アプリで最も壊れやすいところ**なので、ここで全分岐を通す。
///
/// 守りたいのは 3 つ。
///   * 広すぎる範囲では**問い合わせない**（400 を貰いに行かない）
///   * 丸めたあと同じ矩形なら**投げ直さない**（地図の微動で叩かない）
///   * 「多すぎる」は**失敗ではなく操作の案内**として出す
@MainActor
@Suite("StationsModel")
struct StationsModelTests {
    /// 呼ばれた URL を記録する応答役。
    final class Recorder: @unchecked Sendable {
        var urls: [String] = []
    }

    func model(
        status: Int = 200,
        body: Data,
        recorder: Recorder = Recorder()
    ) -> (StationsModel, Recorder) {
        let client = V1Client(baseURL: URL(string: "https://example.test")!) { request in
            recorder.urls.append(request.url?.query ?? "")
            let response = HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            return (body, response)
        }
        return (StationsModel(client: client), recorder)
    }

    func stationsBody() throws -> Data { try ContractTests.fixture("stations") }

    /// 状態が落ち着くまで待つ。読み込みは Task で走るので 1 手番譲る。
    func settle(_ model: StationsModel) async {
        for _ in 0..<50 {
            if case .loading = model.state {
                try? await Task.sleep(for: .milliseconds(5))
                continue
            }
            if case .idle = model.state {
                try? await Task.sleep(for: .milliseconds(5))
                continue
            }
            return
        }
    }

    let tokyo = Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69)

    @Test("最初は何もしていない")
    func startsIdle() throws {
        let (model, _) = model(body: try stationsBody())
        #expect(model.state == .idle)
    }

    @Test("取れたら loaded になる")
    func loadsStations() async throws {
        let (model, recorder) = model(body: try stationsBody())
        model.viewportChanged(to: tokyo)
        await settle(model)
        guard case .loaded(let response) = model.state else {
            Issue.record("loaded になるはず: \(model.state)")
            return
        }
        #expect(response.count > 0)
        #expect(recorder.urls.count == 1)
    }

    @Test("**広すぎる範囲では問い合わせない**（400 を貰いに行かない）")
    func doesNotAskForTooWideAViewport() async throws {
        let (model, recorder) = model(body: try stationsBody())
        model.viewportChanged(to: Bbox(west: 139.0, south: 35.0, east: 140.0, north: 36.0))
        await settle(model)
        #expect(model.state == .needsZoom(StationsModel.zoomInMessage))
        #expect(recorder.urls.isEmpty)
    }

    @Test("**丸めたあと同じ矩形なら投げ直さない**")
    func skipsIdenticalViewports() async throws {
        let (model, recorder) = model(body: try stationsBody())
        model.viewportChanged(to: tokyo)
        await settle(model)
        // 格子（0.01 度）の内側の微動は同じ矩形に写る
        model.viewportChanged(
            to: Bbox(west: 139.7601, south: 35.6702, east: 139.7799, north: 35.6898))
        await settle(model)
        #expect(recorder.urls.count == 1)
    }

    @Test("矩形が変われば投げ直す")
    func reloadsWhenTheViewportMoves() async throws {
        let (model, recorder) = model(body: try stationsBody())
        model.viewportChanged(to: tokyo)
        await settle(model)
        model.viewportChanged(to: Bbox(west: 139.80, south: 35.70, east: 139.82, north: 35.72))
        await settle(model)
        #expect(recorder.urls.count == 2)
    }

    @Test("**「多すぎる」は失敗ではなく案内**")
    func tooManyStationsAsksToZoomIn() async throws {
        let (model, _) = model(status: 400, body: try ContractTests.fixture("problem_too_many"))
        model.viewportChanged(to: tokyo)
        await settle(model)
        guard case .needsZoom(let message) = model.state else {
            Issue.record("needsZoom になるはず: \(model.state)")
            return
        }
        #expect(message.contains("狭めて"))
    }

    @Test("通信の失敗は failed")
    func networkFailureIsFailed() async throws {
        let (model, _) = model(status: 503, body: Data("nope".utf8))
        model.viewportChanged(to: tokyo)
        await settle(model)
        guard case .failed(let message) = model.state else {
            Issue.record("failed になるはず: \(model.state)")
            return
        }
        #expect(message.contains("503"))
    }

    @Test("面積 0 の矩形は無視する（地図の初期化中に来る）")
    func ignoresDegenerateViewports() async throws {
        let (model, recorder) = model(body: try stationsBody())
        model.viewportChanged(to: Bbox(west: 139.76, south: 35.67, east: 139.76, north: 35.67))
        await settle(model)
        #expect(model.state == .idle)
        #expect(recorder.urls.isEmpty)
    }

    @Test("広すぎる状態から戻れば取りに行く")
    func recoversAfterZoomingIn() async throws {
        let (model, recorder) = model(body: try stationsBody())
        model.viewportChanged(to: Bbox(west: 139.0, south: 35.0, east: 140.0, north: 36.0))
        await settle(model)
        model.viewportChanged(to: tokyo)
        await settle(model)
        #expect(recorder.urls.count == 1)
        if case .loaded = model.state {} else { Issue.record("loaded になるはず: \(model.state)") }
    }

    @Test("明示的な再取得は同じ矩形でも投げる")
    func explicitReloadIgnoresTheCache() async throws {
        let (model, recorder) = model(body: try stationsBody())
        model.viewportChanged(to: tokyo)
        await settle(model)
        model.reloadCurrent()
        await settle(model)
        #expect(recorder.urls.count == 2)
    }
}

/// 開いたまま置かれたときの自動再取得（W3 プラン §12 の 107）。
///
/// **守りたいのは 3 つ。**
///   * 時刻を進めるだけでは値は新しくならない。**間隔が来たら取り直す**
///   * 取り直しは**静かに**：`loading` にせず、失敗しても前の値を消さない
///   * 拡大待ちや最初の読み込み中には投げない
@MainActor
@Suite("自動再取得")
struct AutoRefreshTests {
    /// 時刻を進められる時計。
    final class Clock: @unchecked Sendable {
        var now = Date(timeIntervalSince1970: 1_788_800_000)
        func advance(_ seconds: TimeInterval) { now += seconds }
    }

    /// 呼ばれた回数と、次に返す状態コードを持つ応答役。
    final class Server: @unchecked Sendable {
        var calls = 0
        var status = 200
    }

    let tokyo = Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69)
    let tooWide = Bbox(west: 130, south: 30, east: 145, north: 45)

    func make(body: Data) -> (StationsModel, Clock, Server) {
        let clock = Clock()
        let server = Server()
        let client = V1Client(baseURL: URL(string: "https://example.test")!) { request in
            server.calls += 1
            let response = HTTPURLResponse(
                url: request.url!, statusCode: server.status, httpVersion: nil, headerFields: nil)!
            return (body, response)
        }
        return (StationsModel(client: client, clock: { clock.now }), clock, server)
    }

    func body() throws -> Data { try ContractTests.fixture("stations") }

    /// 要求が `count` 件になるまで待つ。**静かな取り直しは `loading` にならない**ので、
    /// 状態ではなく要求の数で待つ。
    func settle(_ server: Server, until count: Int) async {
        for _ in 0..<200 {
            if server.calls >= count { break }
            try? await Task.sleep(for: .milliseconds(5))
        }
        // 応答の反映まで 1 手番譲る
        try? await Task.sleep(for: .milliseconds(20))
    }

    // ── 間隔 ──────────────────────────────────────────────────
    @Test("**間隔が来ていなければ取りに行かない**")
    func waitsForTheInterval() async throws {
        let (model, clock, server) = make(body: try body())
        model.viewportChanged(to: tokyo)
        await settle(server, until: 1)

        clock.advance(StationsModel.refreshInterval - 1)
        #expect(model.refreshIfDue() == false)
        #expect(server.calls == 1)
    }

    @Test("**間隔が来たら取り直す**")
    func refreshesAfterTheInterval() async throws {
        let (model, clock, server) = make(body: try body())
        model.viewportChanged(to: tokyo)
        await settle(server, until: 1)

        clock.advance(StationsModel.refreshInterval)
        #expect(model.refreshIfDue() == true)
        await settle(server, until: 2)
        #expect(server.calls == 2)
    }

    @Test("取り直したら、また間隔ぶん待つ")
    func theIntervalRestartsAfterEachRequest() async throws {
        let (model, clock, server) = make(body: try body())
        model.viewportChanged(to: tokyo)
        await settle(server, until: 1)

        clock.advance(StationsModel.refreshInterval)
        model.refreshIfDue()
        await settle(server, until: 2)
        // 時刻を進めなければ 2 度目は投げない
        #expect(model.refreshIfDue() == false)
        #expect(server.calls == 2)
    }

    @Test("間隔は呼ぶ側で変えられる")
    func theIntervalIsAnArgument() async throws {
        let (model, clock, server) = make(body: try body())
        model.viewportChanged(to: tokyo)
        await settle(server, until: 1)

        clock.advance(5)
        #expect(model.refreshIfDue(interval: 1) == true)
        await settle(server, until: 2)
    }

    // ── 静かに取り直す ────────────────────────────────────────
    @Test("**取り直しの最中も表示を消さない**（`loading` にしない）")
    func silentRefreshKeepsTheCurrentResponse() async throws {
        let (model, clock, server) = make(body: try body())
        model.viewportChanged(to: tokyo)
        await settle(server, until: 1)
        guard case .loaded(let before) = model.state else {
            Issue.record("最初の読み込みに失敗した")
            return
        }

        clock.advance(StationsModel.refreshInterval)
        model.refreshIfDue()
        // 投げた直後も loaded のまま。地図のピンが消えない
        #expect(model.state == .loaded(before))
        await settle(server, until: 2)
        #expect(model.state == .loaded(before))
    }

    @Test("**取り直しが失敗しても前の値を消さない**")
    func silentFailureKeepsTheCurrentResponse() async throws {
        let (model, clock, server) = make(body: try body())
        model.viewportChanged(to: tokyo)
        await settle(server, until: 1)
        guard case .loaded(let before) = model.state else {
            Issue.record("最初の読み込みに失敗した")
            return
        }

        server.status = 500
        clock.advance(StationsModel.refreshInterval)
        model.refreshIfDue()
        await settle(server, until: 2)
        // 一瞬の失敗でエラーを出したり消したりしない。表示はそのまま古くなり、
        // 「N 分前の観測」がそれを伝える
        #expect(model.state == .loaded(before))
    }

    @Test("**明示的な再取得は今までどおり**（`loading` になり、失敗も出す）")
    func explicitReloadStillReportsFailures() async throws {
        let (model, _, server) = make(body: try body())
        model.viewportChanged(to: tokyo)
        await settle(server, until: 1)

        server.status = 500
        model.reloadCurrent()
        await settle(server, until: 2)
        guard case .failed = model.state else {
            Issue.record("明示的な再取得の失敗は表示すべき")
            return
        }
    }

    // ── 投げない場面 ──────────────────────────────────────────
    @Test("まだ何も取っていなければ投げない")
    func doesNotRefreshBeforeTheFirstLoad() throws {
        let (model, clock, server) = make(body: try body())
        clock.advance(StationsModel.refreshInterval * 10)
        #expect(model.refreshIfDue() == false)
        #expect(server.calls == 0)
    }

    @Test("**拡大待ちのときは投げない**（何度投げても同じ）")
    func doesNotRefreshWhileZoomedOut() async throws {
        let (model, clock, server) = make(body: try body())
        model.viewportChanged(to: tooWide)
        #expect(model.state == .needsZoom(StationsModel.zoomInMessage))

        clock.advance(StationsModel.refreshInterval * 10)
        #expect(model.refreshIfDue() == false)
        #expect(server.calls == 0)
    }

    @Test("**失敗のあとは取り直す**（自分で戻れる）")
    func recoversAfterAFailure() async throws {
        let (model, clock, server) = make(body: try body())
        server.status = 500
        model.viewportChanged(to: tokyo)
        await settle(server, until: 1)
        guard case .failed = model.state else {
            Issue.record("最初の読み込みは失敗しているはず")
            return
        }

        server.status = 200
        clock.advance(StationsModel.refreshInterval)
        #expect(model.refreshIfDue() == true)
        await settle(server, until: 2)
        guard case .loaded = model.state else {
            Issue.record("取り直しで戻るべき")
            return
        }
    }

    @Test("間隔の既定は 60 秒（CDN の s-maxage に合わせる）")
    func theDefaultIntervalMatchesTheCdn() {
        // これより短くしても、CDN が同じ本文を返すので新しい値にならない
        #expect(StationsModel.refreshInterval == 60)
    }
}
