import Foundation
import Observation

/// お気に入りの状態（W5 プラン §6.8）。
///
/// **`StationsModel` と違い、状態を 1 つの列挙にしない。** あちらは「1 つの矩形に 1 つの
/// 答え」だが、こちらは **20 件がそれぞれ取れたり取れなかったりする**。
/// 全体を `failed` にすると、19 件取れていても何も出せなくなる——
/// **取れた行は出し、取れなかった行は前の値を古さつきで出す**（開発プラン §9.4）。
@MainActor
@Observable
public final class FavoritesModel {
    /// 登録の一覧。**並びが表示順**（利用者が並べ替える）。
    public private(set) var favorites: [Favorite] = []
    /// 画面に出す行。`favorites` と同じ並び・同じ数。
    public private(set) var rows: [FavoriteRow] = []
    /// 取りに行っている最中か。**行は消さない**（前の値を出したまま）。
    public private(set) var isLoading = false

    /// 借りたいのか返したいのか。**切り替えても取りに行かない**（曲線に両方入っている）。
    public var intent: RideIntent = .borrow {
        didSet { if intent != oldValue { rebuild() } }
    }

    private let client: V1Client
    private let store: FavoritesStoring
    private let cache: FavoritesCaching
    private let clock: @Sendable () -> Date
    /// 最後に取れた応答（生のバイト）。**キャッシュから読んだものもここに入る。**
    private var bodies: [String: Data] = [:]
    /// いま出している行が、取れたものか前の値か。
    private var sources: [String: FavoriteRow.Source] = [:]
    private var task: Task<Void, Never>?

    public init(
        client: V1Client,
        store: FavoritesStoring,
        cache: FavoritesCaching,
        clock: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.client = client
        self.store = store
        self.cache = cache
        self.clock = clock
        load()
    }

    // MARK: - 登録簿

    /// 保存してあるものを読み、**キャッシュから行を組み立てる**（通信の前に何か出す）。
    private func load() {
        favorites = (try? store.load()) ?? []
        for favorite in favorites where bodies[favorite.id] == nil {
            bodies[favorite.id] = cache.read(favorite.id)
            sources[favorite.id] = bodies[favorite.id] == nil ? nil : .cached
        }
        rebuild()
    }

    public func contains(_ station: StationCurrent) -> Bool {
        favorites.contains { $0.id == station.id }
    }

    /// 上限に達しているか。**達していたら登録の口を閉じる**（押せるのに何も起きない、を作らない）。
    public var isFull: Bool { favorites.count >= Favorites.limit }

    /// 登録する。**すでに在れば何もしない。上限を超えたら足さない。**
    @discardableResult
    public func add(_ station: StationCurrent) -> Bool {
        guard !contains(station), !isFull else { return false }
        favorites.append(Favorite(station: station))
        persist()
        return true
    }

    /// 外す。**キャッシュも捨てる**（消したポートの値を端末に残さない）。
    public func remove(_ id: String) {
        favorites.removeAll { $0.id == id }
        bodies[id] = nil
        sources[id] = nil
        cache.forget(id)
        persist()
    }

    /// 一覧の編集（削除・並べ替え）から呼ぶ。
    public func remove(atOffsets offsets: IndexSet) {
        for index in offsets.sorted(by: >) where favorites.indices.contains(index) {
            remove(favorites[index].id)
        }
    }

    public func move(fromOffsets source: IndexSet, toOffset destination: Int) {
        favorites = FavoritesModel.moved(favorites, from: source, to: destination)
        persist()
    }

    /// SwiftUI の `onMove` と同じ意味で並べ替える。
    ///
    /// **自分で書くのは、`BikeChanceCore` に SwiftUI を持ち込まないため**
    /// （`Array.move(fromOffsets:toOffset:)` は SwiftUI が生やしている）。判断の置き場を
    /// 「画面を立ち上げないとテストできないもの」に寄せない、という同じ理由である。
    ///
    /// **`destination` は「取り除く前」の添字**という約束に合わせる——取り除いたぶんだけ
    /// 手前へ寄せないと、下へ動かしたときに 1 つずれる。
    static func moved<Item>(_ items: [Item], from source: IndexSet, to destination: Int) -> [Item] {
        let taken = source.sorted().compactMap { items.indices.contains($0) ? items[$0] : nil }
        var rest = items
        for index in source.sorted(by: >) where rest.indices.contains(index) {
            rest.remove(at: index)
        }
        let shift = source.filter { $0 < destination }.count
        let at = min(max(destination - shift, 0), rest.count)
        rest.insert(contentsOf: taken, at: at)
        return rest
    }

    /// 「いつもの時刻」を変える。**取り直さない**（曲線は取ってある）。
    public func setUsual(_ minutes: Int?, for id: String) {
        guard let index = favorites.firstIndex(where: { $0.id == id }) else { return }
        guard favorites[index].usualMinutes != minutes else { return }
        favorites[index].usualMinutes = minutes
        persist()
    }

    private func persist() {
        try? store.save(favorites)
        rebuild()
    }

    // MARK: - 取得

    /// 全件を取り直す。
    ///
    /// **1 件 1 要求で、件数ぶんしか投げない**（完了条件 3）。`/v1/stations/{system}/{id}` は
    /// 1 ポートを名指しで引く口で、束ねる口はまだ無い——だから上限が 20 件である。
    ///
    /// **1 件の失敗で全体を失敗にしない。** 取れた行は新しく、取れなかった行は前の値のまま
    /// 「いま取得できませんでした」を添えて出す。
    public func reload() {
        task?.cancel()
        guard !favorites.isEmpty else {
            isLoading = false
            rebuild()
            return
        }
        isLoading = true
        let asked = favorites
        task = Task { [client] in
            let results = await FavoritesModel.fetchAll(asked, client: client)
            guard !Task.isCancelled else { return }
            apply(results)
        }
    }

    /// 並列に取る。**返るのは（id, 本文）の組だけ**——`@MainActor` の状態には触らない。
    private static func fetchAll(
        _ favorites: [Favorite], client: V1Client
    ) async -> [(id: String, body: Data?)] {
        await withTaskGroup(of: (id: String, body: Data?).self) { group in
            for favorite in favorites {
                group.addTask {
                    let body = try? await client.stationDetailBody(
                        system: favorite.systemID, stationID: favorite.stationID)
                    return (favorite.id, body)
                }
            }
            var found: [(id: String, body: Data?)] = []
            for await one in group {
                found.append(one)
            }
            return found
        }
    }

    private func apply(_ results: [(id: String, body: Data?)]) {
        for one in results {
            if let body = one.body {
                bodies[one.id] = body
                sources[one.id] = .live
                cache.write(one.id, body)
            } else if bodies[one.id] != nil {
                // **前の値は消さない。** 古さは行が自分で持っている
                sources[one.id] = .cached
            }
        }
        isLoading = false
        rebuild()
    }

    // MARK: - 行の組み立て

    /// 手元のものだけで行を作り直す。**通信しない**（切替・並べ替え・時刻の変更で呼ぶ）。
    private func rebuild() {
        let now = clock()
        let decoder = V1Client.makeDecoder()
        rows = favorites.map { favorite in
            guard let body = bodies[favorite.id],
                let detail = try? decoder.decode(StationDetailResponse.self, from: body)
            else { return FavoriteRow(favorite: favorite) }
            return FavoriteRow(
                favorite: favorite,
                detail: detail,
                source: sources[favorite.id] ?? .cached,
                intent: intent,
                now: now
            )
        }
    }

    /// 「○分前の観測」を進めるために、時計だけを動かす（通信しない）。
    public func tick() {
        rebuild()
    }
}
