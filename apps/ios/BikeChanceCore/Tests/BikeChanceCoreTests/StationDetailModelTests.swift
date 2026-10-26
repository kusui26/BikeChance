import Foundation
import Testing

@testable import BikeChanceCore

/// ポート詳細の応答の取り方（`StationDetailModel.swift`、iOS-3）。
///
/// **主題は「取れなかったときに、前に取れたものを消さないこと」と「取り消しを失敗と言わないこと」。**
/// 画面から戻る・アプリを前面に戻すたびに取り直すので、取り消しも失敗も普通に起きる。
@MainActor
@Suite("ポート詳細の取得")
struct StationDetailModelTests {
    /// 答えを順に返す。**叩いた URL も記録する**（`at` を送っていないことを確かめる）。
    final class Responder: @unchecked Sendable {
        var answers: [Result<(Int, Data), Error>]
        var requested: [URL] = []
        /// 取りに行っている**最中**に呼ぶ。そのあいだの画面の状態を見るのに使う。
        var whileInFlight: (@Sendable () async -> Void)?

        init(_ answers: [Result<(Int, Data), Error>]) {
            self.answers = answers
        }

        func client() -> V1Client {
            V1Client(baseURL: URL(string: "https://example.test")!) { request in
                guard let url = request.url, !self.answers.isEmpty else { throw URLError(.badURL) }
                self.requested.append(url)
                await self.whileInFlight?()
                let (status, body) = try self.answers.removeFirst().get()
                guard
                    let response = HTTPURLResponse(
                        url: url, statusCode: status, httpVersion: nil, headerFields: nil)
                else { throw URLError(.badServerResponse) }
                return (body, response)
            }
        }
    }

    static func ok() throws -> Result<(Int, Data), Error> {
        .success((200, try ContractTests.fixture("station_detail")))
    }

    /// 404（RFC 9457）。**理由はサーバーの `detail` をそのまま出す。**
    static let notFound: Result<(Int, Data), Error> = .success(
        (
            404,
            Data(
                #"{"type":"/problems/station_not_found","title":"Not Found","status":404,"detail":"そのポートは見つかりません。","code":"station_not_found"}"#
                    .utf8)
        ))

    static func load(_ model: StationDetailModel, _ responder: Responder) async {
        await model.load(client: responder.client(), system: "docomo-cycle", stationID: "1")
    }

    @Test("最初は読み込み中")
    func startsLoading() {
        #expect(StationDetailModel().state == .loading)
    }

    @Test("**取れたら、その応答を持つ**（実応答をそのまま読む）")
    func keepsTheResponse() async throws {
        let model = StationDetailModel()
        await Self.load(model, Responder([try Self.ok()]))
        let expected = try ContractTests.decode(StationDetailResponse.self, "station_detail")
        #expect(model.state == .loaded(expected))
    }

    @Test("**`at` を送らない**（同じポートの URL が 5 分ごとに割れて CDN が効かなくなる）")
    func requestsTheStationPathWithoutAQuery() async throws {
        let responder = Responder([try Self.ok()])
        await Self.load(StationDetailModel(), responder)
        let url = try #require(responder.requested.first)
        #expect(url.path == "/v1/stations/docomo-cycle/1")
        #expect(url.query == nil)
    }

    @Test("**断られたら、サーバーの理由をそのまま出す**")
    func showsTheServersReason() async {
        let model = StationDetailModel()
        await Self.load(model, Responder([Self.notFound]))
        #expect(model.state == .failed("そのポートは見つかりません。"))
    }

    @Test("**読めない本文は「読み取れませんでした」**（通信の失敗と言わない）")
    func saysTheBodyWasUnreadable() async {
        let model = StationDetailModel()
        await Self.load(model, Responder([.success((200, Data("{}".utf8)))]))
        #expect(model.state == .failed(V1Error.malformedBody("").message))
    }

    @Test("**理由の分からない失敗は「通信に失敗しました」**")
    func saysTheConnectionFailed() async {
        let model = StationDetailModel()
        await Self.load(model, Responder([.failure(URLError(.notConnectedToInternet))]))
        #expect(model.state == .failed(StationDetailModel.failedText))
    }

    @Test("**前に取れた応答は、取り直しに失敗しても消さない**（古さは値が自分で言う）")
    func keepsTheLastResponseWhenARefreshFails() async throws {
        let model = StationDetailModel()
        let responder = Responder([try Self.ok(), .failure(URLError(.timedOut))])
        await Self.load(model, responder)
        let first = model.state
        await Self.load(model, responder)
        #expect(model.state == first)
        #expect(responder.requested.count == 2, "取り直しには行っている")
    }

    @Test("**失敗の後にもう一度押せば、取り直して出す**")
    func retriesAfterAFailure() async throws {
        let model = StationDetailModel()
        let responder = Responder([Self.notFound, try Self.ok()])
        await Self.load(model, responder)
        #expect(model.state == .failed("そのポートは見つかりません。"))
        await Self.load(model, responder)
        guard case .loaded = model.state else {
            Issue.record("取り直しても出なかった: \(model.state)")
            return
        }
    }

    /// 取りに行っている最中に見えた状態。
    @MainActor
    final class Seen {
        var states: [StationDetailModel.State] = []
    }

    @Test("**もう一度押したら、取りに行くあいだは読み込み中**（前の失敗の文言を出したままにしない）")
    func showsLoadingWhileRetrying() async throws {
        let model = StationDetailModel()
        let responder = Responder([Self.notFound, try Self.ok()])
        await Self.load(model, responder)
        let seen = Seen()
        responder.whileInFlight = { await MainActor.run { seen.states.append(model.state) } }
        await Self.load(model, responder)
        #expect(seen.states == [.loading])
    }

    @Test("**取り直すあいだも、前に取れた応答を出したまま**（読み込み中で消さない）")
    func keepsTheResponseWhileRefreshing() async throws {
        let model = StationDetailModel()
        let responder = Responder([try Self.ok(), try Self.ok()])
        await Self.load(model, responder)
        let before = model.state
        let seen = Seen()
        responder.whileInFlight = { await MainActor.run { seen.states.append(model.state) } }
        await Self.load(model, responder)
        #expect(seen.states == [before])
    }

    @Test("**取り消しは失敗として出さない**（画面が消えた・取り直しが始まった）")
    func ignoresCancellation() async {
        let model = StationDetailModel()
        await Self.load(model, Responder([.failure(CancellationError())]))
        #expect(model.state == .loading)
    }

    /// **取り消してから走らせる。** 検査は `@MainActor` で、`Task` も同じ所で動くので、
    /// `cancel()` は本体が始まる前に効く（順番が決まっていて、たまたま通る検査にならない）。
    static func loadCancelled(_ model: StationDetailModel, _ responder: Responder) async {
        let task = Task { await Self.load(model, responder) }
        task.cancel()
        await task.value
    }

    @Test("**取り消されたタスクは、取れた応答でも書き込まない**")
    func ignoresAResultAfterCancellation() async throws {
        let model = StationDetailModel()
        await Self.loadCancelled(model, Responder([try Self.ok()]))
        #expect(model.state == .loading)
    }

    @Test("**`URLSession` の取り消し（`URLError.cancelled`）も失敗と言わない**")
    func ignoresURLSessionCancellation() async {
        let model = StationDetailModel()
        await Self.loadCancelled(model, Responder([.failure(URLError(.cancelled))]))
        #expect(model.state == .loading)
    }
}
