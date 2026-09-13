import Foundation
import Testing

@testable import BikeChanceCore

/// お気に入りの状態機械（`FavoritesModel.swift`、W5 プラン §6.8）。
///
/// **主題は「何回叩くか」と「取れなかった行をどうするか」。**
/// 1 件 1 要求なので、**数え違えるとそのまま負荷になる**（完了条件 3）。
@MainActor
@Suite("FavoritesModel")
struct FavoritesModelTests {
    /// 叩いた経路を数える。**何回投げたかがそのまま完了条件 3。**
    ///
    /// **鍵を掛ける。** 20 件は**並列に**取りに行くので、素の配列に append すると
    /// 競合して**数が落ちる**（最初は 20 のうち 17 しか記録されず、そのうえ
    /// テストの後始末で落ちた）。**並行に叩く検査は、記録する側も安全にする。**
    final class Recorder: @unchecked Sendable {
        private let lock = NSLock()
        private var recorded: [String] = []
        private var failed: Set<String> = []

        var paths: [String] {
            lock.withLock { recorded }
        }

        var failing: Set<String> {
            get { lock.withLock { failed } }
            set { lock.withLock { failed = newValue } }
        }

        /// 1 回ぶん記録して、落とすかどうかを返す。**読みと書きを 1 つの鍵の中で行う。**
        func record(_ path: String) -> Bool {
            lock.withLock {
                recorded.append(path)
                return failed.contains(path)
            }
        }
    }

    /// 何も書き込まないキャッシュ（既定）。
    struct NoCache: FavoritesCaching {
        func read(_ id: String) -> Data? { nil }
        func write(_ id: String, _ body: Data) {}
        func forget(_ id: String) {}
    }

    /// 手元の辞書に持つキャッシュ。**書いたものが読めることまで見る。**
    final class MemoryCache: FavoritesCaching, @unchecked Sendable {
        var items: [String: Data] = [:]
        func read(_ id: String) -> Data? { items[id] }
        func write(_ id: String, _ body: Data) { items[id] = body }
        func forget(_ id: String) { items[id] = nil }
    }

    static func model(
        favorites: [Favorite] = [],
        cache: FavoritesCaching = NoCache(),
        recorder: Recorder = Recorder(),
        body: Data? = nil
    ) throws -> (FavoritesModel, Recorder, FavoritesStoring) {
        let payload = try body ?? ContractTests.fixture("station_detail")
        let client = V1Client(baseURL: URL(string: "https://example.test")!) { request in
            let path = request.url?.path ?? ""
            let status = recorder.record(path) ? 503 : 200
            let response = HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            return (payload, response)
        }
        let store = FileFavoritesStore(directory: FavoritesTests.scratch())
        try store.save(favorites)
        return (FavoritesModel(client: client, store: store, cache: cache), recorder, store)
    }

    static func settle(_ model: FavoritesModel) async {
        for _ in 0..<100 where model.isLoading {
            try? await Task.sleep(for: .milliseconds(5))
        }
    }

    static func many(_ count: Int) -> [Favorite] {
        (0..<count).map {
            Favorite(systemID: "docomo-cycle", stationID: "\($0)", name: "ポート \($0)")
        }
    }

    // MARK: - 何回叩くか

    @Test("**20 件で要求は 20 回**（完了条件 3）")
    func asksOncePerFavorite() async throws {
        let (model, recorder, _) = try Self.model(favorites: Self.many(Favorites.limit))
        model.reload()
        await Self.settle(model)
        #expect(recorder.paths.count == Favorites.limit)
        #expect(Set(recorder.paths).count == Favorites.limit, "同じポートを 2 度叩かない")
        #expect(recorder.paths.allSatisfy { $0.hasPrefix("/v1/stations/docomo-cycle/") })
    }

    @Test("**切り替えでは取りに行かない**（曲線に両方入っている）")
    func switchingIntentDoesNotFetch() async throws {
        let (model, recorder, _) = try Self.model(favorites: Self.many(3))
        model.reload()
        await Self.settle(model)
        let before = recorder.paths.count
        model.intent = .returnBike
        // **待ってから数える。** 取りに行くのは非同期なので、すぐ数えると
        // 「投げたのに 0 回」に見える（最初この検査は破壊を捕まえられなかった）
        await Self.settle(model)
        try? await Task.sleep(for: .milliseconds(30))
        #expect(recorder.paths.count == before)
        #expect(model.rows.first?.forecast == .notRequested, "いつもの時刻を決めていない")
    }

    @Test("**いつもの時刻を変えても取りに行かない**")
    func changingTheUsualTimeDoesNotFetch() async throws {
        let (model, recorder, _) = try Self.model(favorites: Self.many(2))
        model.reload()
        await Self.settle(model)
        let before = recorder.paths.count
        model.setUsual(30, for: model.favorites[0].id)
        await Self.settle(model)
        try? await Task.sleep(for: .milliseconds(30))
        #expect(recorder.paths.count == before)
        #expect(model.rows.first?.forecast.display != nil, "手元の曲線から読める")
    }

    @Test("**0 件なら 1 回も叩かない**")
    func doesNotFetchWithoutFavorites() async throws {
        let (model, recorder, _) = try Self.model()
        model.reload()
        await Self.settle(model)
        #expect(recorder.paths.isEmpty)
        #expect(model.rows.isEmpty)
    }

    // MARK: - 一部だけ取れない

    @Test("**1 件落ちても、ほかの行は新しくなる**")
    func oneFailureDoesNotHideTheRest() async throws {
        let recorder = Recorder()
        recorder.failing = ["/v1/stations/docomo-cycle/1"]
        let (model, _, _) = try Self.model(favorites: Self.many(3), recorder: recorder)
        model.reload()
        await Self.settle(model)
        #expect(model.rows.count == 3)
        #expect(model.rows[0].source == .live)
        #expect(model.rows[2].source == .live)
        // 取れず、前の値も無い
        #expect(model.rows[1].note == FavoriteRow.missingText)
    }

    @Test("**取れなかった行は、前の値を古さつきで出す**（開発プラン §9.4）")
    func keepsTheLastValueWhenAFetchFails() async throws {
        let recorder = Recorder()
        let cache = MemoryCache()
        let (model, _, _) = try Self.model(
            favorites: Self.many(1), cache: cache, recorder: recorder)
        model.reload()
        await Self.settle(model)
        #expect(model.rows[0].source == .live)
        #expect(cache.items.count == 1, "取れたら書いておく")

        // 次は落とす。**行は消えず、前の値のまま注記が付く**
        recorder.failing = ["/v1/stations/docomo-cycle/0"]
        model.reload()
        await Self.settle(model)
        #expect(model.rows.count == 1)
        #expect(model.rows[0].source == .cached)
        #expect(model.rows[0].note == FavoriteRow.cachedNote)
        #expect(model.rows[0].availability.isEmpty == false)
    }

    @Test("**通信の前にキャッシュから出す**（開いた瞬間に空白を出さない）")
    func showsCachedRowsBeforeFetching() async throws {
        let cache = MemoryCache()
        cache.items["docomo-cycle/0"] = try ContractTests.fixture("station_detail")
        let (model, recorder, _) = try Self.model(favorites: Self.many(1), cache: cache)
        // **まだ reload していない**
        #expect(recorder.paths.isEmpty)
        #expect(model.rows.count == 1)
        #expect(model.rows[0].source == .cached)
        #expect(model.rows[0].name.isEmpty == false)
    }

    // MARK: - 登録簿

    @Test("**上限を超えたら足さない**（押せるのに何も起きない、を作らない）")
    func stopsAtTheLimit() async throws {
        let (model, _, _) = try Self.model(favorites: Self.many(Favorites.limit))
        #expect(model.isFull)
        let station = try ContractTests.decode(StationDetailResponse.self, "station_detail").station
        #expect(model.add(station) == false)
        #expect(model.favorites.count == Favorites.limit)
    }

    @Test("**同じポートは 2 度登録しない**")
    func doesNotAddTwice() async throws {
        let (model, _, _) = try Self.model()
        let station = try ContractTests.decode(StationDetailResponse.self, "station_detail").station
        #expect(model.add(station))
        #expect(model.contains(station))
        #expect(model.add(station) == false)
        #expect(model.favorites.count == 1)
    }

    @Test("**外したらキャッシュも捨てる**（消したポートの値を端末に残さない）")
    func forgetsTheCacheWhenRemoved() async throws {
        let cache = MemoryCache()
        let (model, _, _) = try Self.model(favorites: Self.many(1), cache: cache)
        model.reload()
        await Self.settle(model)
        #expect(cache.items.count == 1)
        model.remove(model.favorites[0].id)
        #expect(model.favorites.isEmpty)
        #expect(cache.items.isEmpty)
        #expect(model.rows.isEmpty)
    }

    @Test("**登録・並べ替え・いつもの時刻は、すぐ書き出す**（落としても残る）")
    func persistsEveryChange() async throws {
        let (model, _, store) = try Self.model(favorites: Self.many(3))
        model.move(fromOffsets: IndexSet(integer: 0), toOffset: 3)
        model.setUsual(30, for: "docomo-cycle/1")
        let saved = try store.load()
        #expect(saved.map(\.stationID) == ["1", "2", "0"])
        #expect(saved.first { $0.stationID == "1" }?.usualMinutes == 30)
    }

    @Test("削除は複数まとめて渡しても正しく外れる")
    func removesSeveralAtOnce() async throws {
        let (model, _, _) = try Self.model(favorites: Self.many(4))
        model.remove(atOffsets: IndexSet([0, 2]))
        #expect(model.favorites.map(\.stationID) == ["1", "3"])
    }

    @Test("**時計を進めても取りに行かない**（「○分前」だけが伸びる）")
    func tickDoesNotFetch() async throws {
        let (model, recorder, _) = try Self.model(favorites: Self.many(1))
        model.reload()
        await Self.settle(model)
        let before = recorder.paths.count
        model.tick()
        await Self.settle(model)
        try? await Task.sleep(for: .milliseconds(30))
        #expect(recorder.paths.count == before)
    }
}
