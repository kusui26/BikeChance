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
