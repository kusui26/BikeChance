import Foundation
import Testing

@testable import BikeChanceCore

/// 行程チェックの状態機械（`TripModel.swift`）。
///
/// **主題は「断られたときに、直し方が分かる形で伝わるか」。** 行程は出発時刻と 2 つの
/// ポートで決まるので、**利用者が変えれば通る**断られ方がある（範囲外・同じポート）。
/// そこを「通信に失敗しました」に畳むと、何を直せばよいか分からなくなる。
/// 検査で使う「いま」。**型の外に置く**——`@MainActor` の中に置くと、
/// `clock` の `@Sendable` クロージャから参照できない（Swift 6 の並行性検査）。
private let fixedNow = Date(timeIntervalSince1970: 1_789_000_000)

@MainActor
@Suite("TripModel")
struct TripModelTests {
    /// 叩いた URL のクエリを記録する。
    final class Recorder: @unchecked Sendable {
        var queries: [String] = []
    }

    /// 所要を必ず返す見積もり役。**端末の経路案内の代わり。**
    struct FixedEstimate: RideEstimating {
        let minutes: Int?
        func rideMinutes(from: Coordinate, to: Coordinate) async -> Int? { minutes }
    }

    static var now: Date { fixedNow }

    static func model(
        status: Int = 200,
        body: Data,
        estimator: RideEstimating = NoRideEstimate(),
        recorder: Recorder = Recorder()
    ) -> (TripModel, Recorder) {
        let client = V1Client(baseURL: URL(string: "https://example.test")!) { request in
            recorder.queries.append(request.url?.query ?? "")
            let response = HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            return (body, response)
        }
        let model = TripModel(client: client, estimator: estimator, clock: { fixedNow })
        return (model, recorder)
    }

    static func ends() throws -> (StationCurrent, StationCurrent) {
        let response = try ContractTests.decode(TripCheckResponse.self, "trip_check")
        return (response.from, response.to)
    }

    static func settle(_ model: TripModel) async {
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

    @Test("最初は何もしていない")
    func startsIdle() throws {
        let (model, _) = Self.model(body: try ContractTests.fixture("trip_check"))
        #expect(model.state == .idle)
        #expect(model.departureMinutes == Arrival.minMinutes)
    }

    @Test("取れたら loaded になり、画面の形が作れる")
    func loadsTheTrip() async throws {
        let (model, _) = Self.model(body: try ContractTests.fixture("trip_check"))
        let (from, to) = try Self.ends()
        model.check(from: from, to: to)
        await Self.settle(model)
        guard case .loaded = model.state else {
            Issue.record("loaded になるはず: \(model.state)")
            return
        }
        let plan = try #require(model.plan(now: Self.now))
        #expect(plan.outcome.display != nil)
    }

    // MARK: - 何を送るか

    @Test("**出発は絶対の `depart_at` で送る**（相対だと CDN 越しにずれる）")
    func sendsAnAbsoluteDeparture() async throws {
        let (model, recorder) = Self.model(body: try ContractTests.fixture("trip_check"))
        let (from, to) = try Self.ends()
        model.check(from: from, to: to)
        await Self.settle(model)
        let query = try #require(recorder.queries.first)
        #expect(query.contains("depart_at="))
        #expect(!query.contains("depart_in_min="))
        #expect(query.contains("system=docomo-cycle"))
        #expect(query.contains("from=4979"))
        #expect(query.contains("to=5560"))
    }

    @Test("**出発は 5 分の格子に乗る**（同じ時間帯の利用者が同じ URL を叩く）")
    func griddsTheDeparture() async throws {
        let (model, recorder) = Self.model(body: try ContractTests.fixture("trip_check"))
        let (from, to) = try Self.ends()
        model.departureMinutes = 30
        model.check(from: from, to: to)
        await Self.settle(model)
        let query = try #require(recorder.queries.first)
        let expected = Arrival.gridded(Self.now.addingTimeInterval(30 * 60), now: Self.now)
        // **`Z` で書く**ので、クエリに入れても符号化の問題が起きない（`V1Client.timestamp`）
        #expect(query.contains("depart_at=\(V1Client.timestamp(expected))"))
        #expect(query.hasSuffix("Z"), "実際に送った文字列: \(query)")
        // 格子の上に乗っている（5 分の倍数）
        #expect(expected.timeIntervalSince1970.truncatingRemainder(dividingBy: 300) == 0)
    }

    @Test("**端末が所要を出せたときだけ `ride_min` を載せる**")
    func sendsTheRideTimeOnlyWhenTheDeviceHasOne() async throws {
        let body = try ContractTests.fixture("trip_check")
        let (from, to) = try Self.ends()

        let (silent, silentRecorder) = Self.model(body: body)
        silent.check(from: from, to: to)
        await Self.settle(silent)
        #expect(silentRecorder.queries.first?.contains("ride_min") == false)

        let (measured, measuredRecorder) = Self.model(
            body: body, estimator: FixedEstimate(minutes: 9))
        measured.check(from: from, to: to)
        await Self.settle(measured)
        #expect(measuredRecorder.queries.first?.contains("ride_min=9") == true)
    }

    @Test("**経路案内は 1 回だけ叩く**（端末単位でスロットリングされる）")
    func asksTheDeviceForAnEstimateOnlyOnce() async throws {
        final class Counter: RideEstimating, @unchecked Sendable {
            var calls = 0
            func rideMinutes(from: Coordinate, to: Coordinate) async -> Int? {
                calls += 1
                return 9
            }
        }
        let counter = Counter()
        let (model, _) = Self.model(
            body: try ContractTests.fixture("trip_check"), estimator: counter)
        let (from, to) = try Self.ends()
        model.check(from: from, to: to)
        await Self.settle(model)
        #expect(counter.calls == 1, "代替候補ぶんは叩かない")
    }

    // MARK: - 断られたとき

    @Test("**範囲外は理由をそのまま出す**（完了条件 4。『通信に失敗』に畳まない）")
    func showsWhyTheTripWasRefused() async throws {
        let (model, _) = Self.model(
            status: 400, body: try ContractTests.fixture("problem_arrival_out_of_range"))
        let (from, to) = try Self.ends()
        model.check(from: from, to: to)
        await Self.settle(model)
        guard case .failed(let message) = model.state else {
            Issue.record("failed になるはず: \(model.state)")
            return
        }
        #expect(message.contains("180 分先まで"), "直し方が読める: \(message)")
        #expect(model.plan(now: Self.now) == nil, "断られたら画面の形は作らない")
    }

    @Test("Problem でない失敗も、画面が壊れずに 1 行で伝わる")
    func survivesANonProblemFailure() async throws {
        let (model, _) = Self.model(status: 503, body: Data("<html>".utf8))
        let (from, to) = try Self.ends()
        model.check(from: from, to: to)
        await Self.settle(model)
        #expect(model.state == .failed("通信に失敗しました（503）。"))
    }

    // MARK: - 出発時刻を変えたとき

    @Test("**出発時刻を変えたら前の結果を捨てる**（古い時刻の確率を出したままにしない）")
    func forgetsTheResultWhenTheDepartureChanges() async throws {
        let (model, _) = Self.model(body: try ContractTests.fixture("trip_check"))
        let (from, to) = try Self.ends()
        model.check(from: from, to: to)
        await Self.settle(model)
        #expect(model.plan(now: Self.now) != nil)
        model.departureMinutes = 60
        #expect(model.state == .idle)
        #expect(model.plan(now: Self.now) == nil)
    }

    @Test("同じ出発時刻を選び直しても結果は消えない")
    func keepsTheResultWhenTheDepartureIsUnchanged() async throws {
        let (model, _) = Self.model(body: try ContractTests.fixture("trip_check"))
        let (from, to) = try Self.ends()
        model.check(from: from, to: to)
        await Self.settle(model)
        model.departureMinutes = model.departureMinutes
        #expect(model.plan(now: Self.now) != nil)
    }

    @Test("出発の目盛りは最短（5 分）から始まる")
    func theDepartureChoicesStartAtTheMinimum() {
        #expect(TripModel.departureChoices.first == Arrival.minMinutes)
        #expect(TripModel.departureChoices.allSatisfy { $0 >= Arrival.minMinutes })
        #expect(TripModel.departureChoices.allSatisfy { $0 <= Arrival.maxMinutes })
        #expect(TripModel.departureChoices == TripModel.departureChoices.sorted())
    }
}
