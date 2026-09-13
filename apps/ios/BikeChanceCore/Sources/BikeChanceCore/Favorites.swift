import Foundation

/// 登録したポート 1 つ（W5 プラン §6.8、開発プラン §9.2）。
///
/// **名前を持つ。** サーバーが名前を返せなくても（属性の同期がまだ届いていないポートは
/// 実在する）、一覧に「（名称未取得）」が並ばないようにするため。登録したときの名前を
/// 覚えておき、新しい名前が取れたら差し替える。
///
/// **「いつもの時刻」は分で持つ**（`nil` なら「いま」）。壁掛け時計の時刻ではないのは、
/// 予測が出せるのが 3 時間先までだからで、「毎朝 8:30」を覚えても夕方には答えられない。
/// **「この駅にはいつも 15 分後に着く」**なら、いつ開いても意味がある。
public struct Favorite: Codable, Equatable, Sendable, Identifiable {
    public let systemID: String
    public let stationID: String
    /// 一覧に出す名前。取れたら差し替える。
    public var name: String
    /// いつもの到着（分）。**`nil` は「いま」**＝現在値だけを見る。
    public var usualMinutes: Int?

    /// システムをまたいで一意（`StationCurrent.id` と同じ作り方）。
    public var id: String { "\(systemID)/\(stationID)" }

    public init(systemID: String, stationID: String, name: String, usualMinutes: Int? = nil) {
        self.systemID = systemID
        self.stationID = stationID
        self.name = name
        self.usualMinutes = usualMinutes
    }

    private enum CodingKeys: String, CodingKey {
        case systemID = "system_id"
        case stationID = "station_id"
        case name
        case usualMinutes = "usual_min"
    }
}

extension Favorite {
    /// 地図で見ているポートから作る。
    public init(station: StationCurrent, usualMinutes: Int? = nil) {
        self.init(
            systemID: station.systemID,
            stationID: station.stationID,
            name: station.name ?? station.stationID,
            usualMinutes: usualMinutes
        )
    }
}

/// お気に入りの決まりごと。
public enum Favorites {
    /// 登録できる上限。
    ///
    /// **1 件 1 要求だから**である（`/v1/stations/{system}/{id}`）。増やすなら
    /// **bbox で束ねる口を先に足す**——20 件を超えて 1 件ずつ叩くのは、
    /// 「上限を置いた意味が無い」状態に自分で近づくことになる（W5 プラン §6.8）。
    public static let limit = 20

    /// 「いつもの時刻」に選べる分数。
    ///
    /// **`HORIZONS_MIN` の部分集合**にしてある。サーバーが返す曲線は**補間していない
    /// 生の 10 点**（契約 16）で、**その点をそのまま読めば端末は補間しなくてよい**。
    /// 補間を端末にも書くと、同じ規則の実装が 2 つになる（§12 の 142）。
    public static let usualChoices: [Int?] = [nil, 15, 30, 60, 120]

    /// 選択肢に出す短い名前。
    public static func usualLabel(_ minutes: Int?) -> String {
        guard let minutes else { return "いま" }
        return Arrival.relativeText(minutes: minutes)
    }
}

/// お気に入りの読み書き。**差し替え口**にしておき、検査は一時ディレクトリを渡す。
public protocol FavoritesStoring: Sendable {
    func load() throws -> [Favorite]
    func save(_ favorites: [Favorite]) throws
}

/// JSON 1 ファイルに書く。
///
/// **SwiftData を使わない**（W5 プラン §6.8）。20 件の配列に、問い合わせも移行も要らない。
///
/// **置き場所は呼ぶ側が決める。** 既定は App Group のコンテナで、Widget（W9）から
/// 同じファイルが読める——**いまはまだ権限を宣言していない**ので、`AppEnvironment` が
/// アプリ自身の Documents に落とす。**その日が来ても、ここは 1 行も変わらない。**
public struct FileFavoritesStore: FavoritesStoring {
    /// 置き場所の中のファイル名。Widget（W9）も同じ名前で読む。
    public static let fileName = "favorites.json"

    private let directory: URL

    public init(directory: URL) {
        self.directory = directory
    }

    private var url: URL { directory.appendingPathComponent(Self.fileName) }

    /// **無ければ空**。初回起動を失敗にしない。
    public func load() throws -> [Favorite] {
        guard let data = try? Data(contentsOf: url) else { return [] }
        return try JSONDecoder().decode([Favorite].self, from: data)
    }

    /// **書ききるか、前のまま。** 途中で落ちて壊れた JSON を残さない。
    public func save(_ favorites: [Favorite]) throws {
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        try encoder.encode(favorites).write(to: url, options: .atomic)
    }
}

/// 最後に取れた応答をそのまま取っておく（オフライン用。開発プラン §9.4）。
///
/// **中身を解釈せずにバイトで持つ。** 型に写してから書き戻すと、**サーバーが足した欄が
/// 端末のキャッシュで消える**——読み直したものが、受け取ったものと違う状態を作らない。
///
/// **「○分前」はキャッシュの日付から作らない。** 応答の中の `observed_at` から作る
/// （`Freshness`）ので、**値そのものが自分の古さを持っている**。バックアップから戻した
/// 端末でも、観測時刻は嘘にならない。
public protocol FavoritesCaching: Sendable {
    func read(_ id: String) -> Data?
    func write(_ id: String, _ body: Data)
    func forget(_ id: String)
}

public struct FileFavoritesCache: FavoritesCaching {
    private let directory: URL

    public init(directory: URL) {
        self.directory = directory
    }

    /// `/` を含む id をそのままファイル名にしない。
    private func url(_ id: String) -> URL {
        directory.appendingPathComponent(id.replacingOccurrences(of: "/", with: "_") + ".json")
    }

    public func read(_ id: String) -> Data? {
        try? Data(contentsOf: url(id))
    }

    public func write(_ id: String, _ body: Data) {
        try? FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true)
        try? body.write(to: url(id), options: .atomic)
    }

    public func forget(_ id: String) {
        try? FileManager.default.removeItem(at: url(id))
    }
}
