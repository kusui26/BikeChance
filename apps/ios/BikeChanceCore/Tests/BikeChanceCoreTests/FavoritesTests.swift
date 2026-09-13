import Foundation
import Testing

@testable import BikeChanceCore

/// お気に入り（`Favorites.swift`・`FavoriteRow.swift`・`FavoritesModel.swift`、W5 の PR G→H）。
///
/// **主題は「取れなかったときに何を出すか」。** 20 件を 1 件ずつ取りに行くので、
/// **一部だけ取れない**が普通に起きる。そこで全体を失敗にすると、19 件取れていても
/// 何も出せなくなる——**取れた行は新しく、取れなかった行は前の値を古さつきで**出す
/// （開発プラン §9.4）。
///
/// フィクスチャは **2026-09-13 に本番から取った実応答**（`station_detail.json` と
/// `station_detail_no_forecast.json`）。
@MainActor
@Suite("お気に入り")
struct FavoritesTests {
    static let jst = TimeZone(identifier: "Asia/Tokyo")!
    static let ja = Locale(identifier: "ja_JP")

    /// 使い捨てのディレクトリ。**本物のファイルに書く**——`FileFavoritesStore` の
    /// 「書ききるか、前のまま」を、本物の入出力で確かめるため。
    static func scratch() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("favorites-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    static func detail(_ name: String = "station_detail") throws -> StationDetailResponse {
        try ContractTests.decode(StationDetailResponse.self, name)
    }

    /// フィクスチャの `generated_at`。**時計はここに合わせる**（相対で動かない検査にする）。
    static func now(_ name: String = "station_detail") throws -> Date {
        try detail(name).generatedAt
    }

    // MARK: - 保存

    @Test("登録・並び・いつもの時刻が、書いて読み直しても残る")
    func persistsAcrossRestarts() throws {
        let store = FileFavoritesStore(directory: Self.scratch())
        let saved = [
            Favorite(systemID: "docomo-cycle", stationID: "1", name: "A", usualMinutes: 15),
            Favorite(systemID: "hellocycling", stationID: "1", name: "B"),
        ]
        try store.save(saved)
        #expect(try store.load() == saved)
    }

    @Test("**まだ 1 度も保存していなければ空**（初回起動を失敗にしない）")
    func startsEmpty() throws {
        #expect(try FileFavoritesStore(directory: Self.scratch()).load().isEmpty)
    }

    @Test("**同じ ID はシステムをまたいで衝突する**ので、鍵は系統込み")
    func idIncludesTheSystem() {
        let one = Favorite(systemID: "docomo-cycle", stationID: "10428", name: "A")
        let other = Favorite(systemID: "hellocycling", stationID: "10428", name: "B")
        #expect(one.id != other.id)
        #expect(one.id == "docomo-cycle/10428")
    }

    // MARK: - 行の組み立て

    @Test("取れた行は、現在値も「いつもの時刻」の確率も出る")
    func buildsALiveRow() throws {
        let detail = try Self.detail()
        let favorite = Favorite(station: detail.station, usualMinutes: 30)
        let row = FavoriteRow(
            favorite: favorite, detail: detail, source: .live, intent: .borrow,
            now: try Self.now(), locale: Self.ja, timeZone: Self.jst)
        #expect(row.note == nil)
        #expect(row.availability.count == 2)
        #expect(row.forecast.display != nil)
    }

    @Test("**「いつもの時刻」を決めていなければ確率を出さない**（現在値だけ）")
    func noForecastWithoutAUsualTime() throws {
        let detail = try Self.detail()
        let row = FavoriteRow(
            favorite: Favorite(station: detail.station), detail: detail, source: .live,
            intent: .borrow, now: try Self.now())
        #expect(row.forecast == .notRequested)
        #expect(row.availability.first?.value != StationDetail.unknownValue)
    }

    @Test("**確率は曲線に在る点をそのまま読む**（補間しない。契約 16）")
    func readsAPointOffTheCurve() throws {
        let detail = try Self.detail()
        let curve = try #require(detail.forecastCurve)
        let favorite = Favorite(station: detail.station, usualMinutes: 30)
        let row = FavoriteRow(
            favorite: favorite, detail: detail, source: .live, intent: .borrow,
            now: try Self.now(), locale: Self.ja, timeZone: Self.jst)
        let index = try #require(curve.nearestIndex(toMinutes: 30))
        #expect(curve.horizonsMinutes[index] == 30, "30 分は水平に在る")
        let raw = try #require(curve.probability(for: .borrow, at: index))
        #expect(row.forecast.display?.percent == ForecastDisplay.percent(of: raw))
    }

    @Test("**近い点を選んだことは、絶対時刻で画面に出る**（「約 N 分後」と書かない）")
    func showsTheClockOfThePointItUsed() throws {
        let detail = try Self.detail()
        let curve = try #require(detail.forecastCurve)
        // 水平に無い分数（35 分）。いちばん近いのは 30 分
        let row = FavoriteRow(
            favorite: Favorite(station: detail.station, usualMinutes: 35), detail: detail,
            source: .live, intent: .borrow, now: try Self.now(),
            locale: Self.ja, timeZone: Self.jst)
        let index = try #require(curve.nearestIndex(toMinutes: 35))
        #expect(curve.horizonsMinutes[index] == 30)
        let clock = Arrival.clockText(
            try #require(curve.arrival(at: index)), locale: Self.ja, timeZone: Self.jst)
        #expect(row.forecast.display?.arrival == "\(clock) 到着")
        #expect(row.forecast.display?.arrival.contains("分後") == false)
    }

    @Test("**同じ距離なら手前を採る**（到着が早いほうの確率を出す）")
    func prefersTheEarlierPointOnATie() throws {
        let curve = try #require(try Self.detail().forecastCurve)
        // **25 分はちょうど真ん中**（20 と 30 から 5 ずつ）。ここが同値の分かれ目で、
        // 17 分のような「片方に寄った値」では**同値の規則を 1 つも確かめられない**
        #expect(curve.horizonsMinutes.contains(20) && curve.horizonsMinutes.contains(30))
        let tied = try #require(curve.nearestIndex(toMinutes: 25))
        #expect(curve.horizonsMinutes[tied] == 20, "同じ距離なら手前")
        // 寄っているほうも確かめる（近いほうを選べていること自体）
        let leaning = try #require(curve.nearestIndex(toMinutes: 17))
        #expect(curve.horizonsMinutes[leaning] == 15)
    }

    @Test("**長さがそろっていなければ読まない**（欠けた表から数を作らない）")
    func refusesACurveWithMismatchedLengths() throws {
        let raw = try ContractTests.fixture("station_detail")
        var object = try #require(
            try JSONSerialization.jsonObject(with: raw) as? [String: Any])
        var curve = object["forecast_curve"] as! [String: Any]
        // 返せる確率だけ 1 つ短い。**添字がずれたまま読ませない**
        var dock = curve["p_dock"] as! [Any]
        dock.removeLast()
        curve["p_dock"] = dock
        object["forecast_curve"] = curve
        let detail = try V1Client.makeDecoder().decode(
            StationDetailResponse.self,
            from: try JSONSerialization.data(withJSONObject: object))
        #expect(detail.forecastCurve?.nearestIndex(toMinutes: 30) == nil)
        let row = FavoriteRow(
            favorite: Favorite(station: detail.station, usualMinutes: 30), detail: detail,
            source: .live, intent: .borrow, now: detail.generatedAt)
        #expect(row.forecast == .unavailable(ForecastState.unavailableText))
    }

    @Test("**借りると返すで別の数**（切り替えても取りに行かない）")
    func eachIntentReadsItsOwnValue() throws {
        let detail = try Self.detail()
        let favorite = Favorite(station: detail.station, usualMinutes: 30)
        let borrow = FavoriteRow(
            favorite: favorite, detail: detail, source: .live, intent: .borrow,
            now: try Self.now())
        let give = FavoriteRow(
            favorite: favorite, detail: detail, source: .live, intent: .returnBike,
            now: try Self.now())
        #expect(borrow.forecast.display?.headline.hasPrefix("借りられる") == true)
        #expect(give.forecast.display?.headline.hasPrefix("返せる") == true)
    }

    @Test("**予測の無いポートでも行は出る**（現在値は出す）")
    func showsAStationWithoutAForecast() throws {
        let detail = try Self.detail("station_detail_no_forecast")
        #expect(detail.forecastCurve == nil, "前提：サーバーが曲線を返していない")
        let row = FavoriteRow(
            favorite: Favorite(station: detail.station, usualMinutes: 30), detail: detail,
            source: .live, intent: .borrow, now: detail.generatedAt)
        #expect(row.forecast == .unavailable(ForecastState.unavailableText))
        #expect(row.note == nil, "取得そのものは成功している")
        #expect(row.name.isEmpty == false)
    }

    @Test("**フィードが滞っていれば確率を伏せる**（地図と同じ判断）")
    func hidesTheForecastWhenTheFeedIsStale() throws {
        let raw = try ContractTests.fixture("station_detail")
        var object = try #require(
            try JSONSerialization.jsonObject(with: raw) as? [String: Any])
        var feeds = object["feeds"] as! [[String: Any]]
        for index in feeds.indices { feeds[index]["stale"] = true }
        object["feeds"] = feeds
        let detail = try V1Client.makeDecoder().decode(
            StationDetailResponse.self,
            from: try JSONSerialization.data(withJSONObject: object))
        let row = FavoriteRow(
            favorite: Favorite(station: detail.station, usualMinutes: 30), detail: detail,
            source: .live, intent: .borrow, now: detail.generatedAt)
        #expect(row.forecast == .unavailable(ForecastState.staleFeedText))
    }

    @Test("**台数は詳細画面と同じ規則**（停止中は数を出さず、理由を出す）")
    func countsFollowTheSharedRule() throws {
        let raw = try ContractTests.fixture("station_detail")
        var object = try #require(
            try JSONSerialization.jsonObject(with: raw) as? [String: Any])
        var station = object["station"] as! [String: Any]
        station["is_renting"] = false
        object["station"] = station
        let detail = try V1Client.makeDecoder().decode(
            StationDetailResponse.self,
            from: try JSONSerialization.data(withJSONObject: object))
        let row = FavoriteRow(
            favorite: Favorite(station: detail.station), detail: detail, source: .live,
            intent: .borrow, now: detail.generatedAt)
        #expect(row.availability.first?.value == StationDetail.unknownValue)
        #expect(row.availability.first?.caution == "貸出停止中")
    }

    // MARK: - オフライン

    @Test("**取れなかった行は、前の値を古さつきで出す**（開発プラン §9.4）")
    func showsTheLastValueWhenOffline() throws {
        let detail = try Self.detail()
        // 応答の 40 分後に開いた、を模す
        let later = detail.generatedAt.addingTimeInterval(40 * 60)
        let row = FavoriteRow(
            favorite: Favorite(station: detail.station), detail: detail, source: .cached,
            intent: .borrow, now: later, locale: Self.ja, timeZone: Self.jst)
        #expect(row.note == FavoriteRow.cachedNote)
        // **古さは値そのものが持っている**（観測時刻から作る。キャッシュの日付ではない）
        #expect(row.freshness.label.contains("分前の観測"))
        #expect(row.freshness.isPresentable == false, "鮮度を超えたら『現在値』として出さない")
        #expect(row.availability.first?.value == StationDetail.unknownValue)
    }

    @Test("**一度も取れていない行も、名前だけは出す**（空白を出さない）")
    func showsTheNameEvenBeforeTheFirstFetch() {
        let row = FavoriteRow(
            favorite: Favorite(systemID: "docomo-cycle", stationID: "1", name: "04.ドコモ東北ビル"))
        #expect(row.name == "04.ドコモ東北ビル")
        #expect(row.note == FavoriteRow.missingText)
        #expect(row.freshness == .unknown)
        #expect(row.availability.allSatisfy { $0.value == StationDetail.unknownValue })
    }

    // MARK: - 並べ替え

    @Test("**`onMove` と同じ意味で並べ替える**（下へ動かしたときに 1 つずれない）")
    func reordersLikeSwiftUI() {
        let items = ["a", "b", "c", "d"]
        // SwiftUI の約束：`destination` は「取り除く前」の添字
        #expect(
            FavoritesModel.moved(items, from: IndexSet(integer: 0), to: 2) == ["b", "a", "c", "d"])
        #expect(
            FavoritesModel.moved(items, from: IndexSet(integer: 3), to: 0) == ["d", "a", "b", "c"])
        #expect(
            FavoritesModel.moved(items, from: IndexSet(integer: 0), to: 4) == ["b", "c", "d", "a"])
        #expect(
            FavoritesModel.moved(items, from: IndexSet([0, 1]), to: 4) == ["c", "d", "a", "b"])
        #expect(FavoritesModel.moved(items, from: IndexSet(integer: 1), to: 1) == items)
    }

    @Test("範囲の外を渡しても落ちない")
    func survivesOutOfRangeMoves() {
        let items = ["a", "b"]
        #expect(FavoritesModel.moved(items, from: IndexSet(integer: 9), to: 0) == items)
        #expect(FavoritesModel.moved(items, from: IndexSet(integer: 0), to: 99) == ["b", "a"])
    }

    // MARK: - 上限

    @Test("**上限は 20 件**（1 件 1 要求だから）")
    func stopsAtTheLimit() {
        #expect(Favorites.limit == 20)
    }

    @Test("「いつもの時刻」の選択肢は水平の部分集合で、先頭は「いま」")
    func usualChoicesAreHorizons() throws {
        #expect(Favorites.usualChoices.first == Int?.none)
        #expect(Favorites.usualLabel(nil) == "いま")
        #expect(Favorites.usualLabel(30) == "30 分後")
        let curve = try #require(try Self.detail().forecastCurve)
        for choice in Favorites.usualChoices.compactMap({ $0 }) {
            #expect(curve.horizonsMinutes.contains(choice), "\(choice) 分は水平に在る")
        }
    }
}
