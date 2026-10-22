import Foundation
import Testing

@testable import BikeChanceCore

/// 「データについて」（W6 の PR J、W6-31、契約 42）。
///
/// **主題は 3 つ。**
///   * **足した欄（前置き・出典）が無い古い応答も読む**（PR J より前のサーバー、古いフィクスチャ）
///   * **取れなくても表示義務を落とさない**：最後に取れた内容を、それを作った時刻つきで出す
///   * **壊れた本文で、取っておいた内容を上書きしない**
@MainActor
@Suite("データについて（W6 の PR J）")
struct CreditsTests {
    static let preface = "このアプリは、以下の著作物を改変して利用しています。"
    static let japan = Locale(identifier: "ja_JP")
    static let tokyo = TimeZone(identifier: "Asia/Tokyo")!

    /// 本番の `/v1/meta`（2026-09-09、PR J より前の形）に、PR J のサーバーが足す 2 つの欄を
    /// 足したもの。**契約テストではない**——PR J のデプロイ（10/14）の後に本番の応答で足す。
    static func newMeta() throws -> Data {
        let old = try ContractTests.fixture("meta")
        var object = try #require(try JSONSerialization.jsonObject(with: old) as? [String: Any])
        object["attribution_preface"] = preface
        object["sources"] = [
            [
                "id": "open-meteo",
                "text": "Open-Meteo、天気予報（気象庁 MSM ほか。予測の材料に加工して利用）、"
                    + "クリエイティブ・コモンズ・ライセンス 表示4.0国際（https://creativecommons.org/licenses/by/4.0/deed.ja）",
                "link_label": "Weather data by Open-Meteo.com",
                "url": "https://open-meteo.com/",
            ],
            [
                "id": "cao-holidays",
                "text": "「国民の祝日について」（内閣府）"
                    + "（https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html）を加工して作成",
                "link_label": "内閣府「国民の祝日について」",
                "url": "https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html",
            ],
        ]
        return try JSONSerialization.data(withJSONObject: object)
    }

    // MARK: - 読む

    @Test("**PR J より前の応答も読む**（前置きは nil、出典は空）")
    func decodesTheOldShape() throws {
        let meta = try ContractTests.decode(MetaResponse.self, "meta")
        #expect(meta.attributionPreface == nil)
        #expect(meta.sources.isEmpty)
        #expect(meta.attribution.count == 2)
    }

    @Test("前置きと出典を読む（リンクの文言と行き先もサーバーのまま）")
    func decodesTheNewFields() throws {
        let meta = try V1Client.makeDecoder().decode(MetaResponse.self, from: Self.newMeta())
        #expect(meta.attributionPreface == Self.preface)
        #expect(meta.sources.map(\.id) == ["open-meteo", "cao-holidays"])
        #expect(meta.sources.first?.linkLabel == "Weather data by Open-Meteo.com")
        #expect(meta.sources.first?.url == URL(string: "https://open-meteo.com/"))
    }

    // MARK: - 取っておく

    /// 手元の変数に持つ置き場所。**書いたバイトがそのまま読めるか**まで見る。
    final class MemoryCache: MetaCaching, @unchecked Sendable {
        private let lock = NSLock()
        private var body: Data?
        var stored: Data? { lock.withLock { body } }
        init(_ body: Data? = nil) { self.body = body }
        func read() -> Data? { lock.withLock { body } }
        func write(_ body: Data) { lock.withLock { self.body = body } }
    }

    enum Reply {
        case body(Data)
        case status(Int)
        case offline
    }

    /// 途中で通信の具合を変えられる口（起動時は取れて、あとで切れる、を作る。W7-08）。
    final class Line: @unchecked Sendable {
        private let lock = NSLock()
        private var current: Reply
        init(_ reply: Reply) { current = reply }
        var reply: Reply {
            get { lock.withLock { current } }
            set { lock.withLock { current = newValue } }
        }
    }

    static func model(_ reply: Reply, cache: MetaCaching) -> CreditsModel {
        model(Line(reply), cache: cache)
    }

    static func model(_ line: Line, cache: MetaCaching) -> CreditsModel {
        let client = V1Client(baseURL: URL(string: "https://example.test")!) { request in
            let url = try #require(request.url)
            func response(_ code: Int) -> HTTPURLResponse {
                HTTPURLResponse(url: url, statusCode: code, httpVersion: nil, headerFields: nil)!
            }
            switch line.reply {
            case .body(let data):
                return (data, response(200))
            case .status(let code):
                return (Data(), response(code))
            case .offline:
                throw URLError(.notConnectedToInternet)
            }
        }
        return CreditsModel(client: client, cache: cache)
    }

    @Test("**取れたら、受け取ったバイトをそのまま取っておく**")
    func keepsTheBytesItReceived() async throws {
        let body = try Self.newMeta()
        let cache = MemoryCache()
        let model = Self.model(.body(body), cache: cache)
        await model.load()
        guard case .live(let meta) = model.state else {
            Issue.record("live を期待した：\(model.state)")
            return
        }
        #expect(meta.sources.count == 2)
        #expect(cache.stored == body)
    }

    @Test("**通信できなければ、最後に取れた内容を出す**（契約 42）", arguments: [Reply.offline, .status(503)])
    func fallsBackToTheLastMeta(_ reply: Reply) async throws {
        let saved = try ContractTests.fixture("meta")
        let model = Self.model(reply, cache: MemoryCache(saved))
        await model.load()
        guard case .cached(let meta) = model.state else {
            Issue.record("cached を期待した：\(model.state)")
            return
        }
        #expect(meta.modelVersion == "baseline-b3-v0-20260908")
    }

    @Test("**壊れた本文で、取っておいた内容を上書きしない**")
    func doesNotOverwriteWithABrokenBody() async throws {
        let saved = try ContractTests.fixture("meta")
        let cache = MemoryCache(saved)
        let model = Self.model(.body(Data("{\"api_version\":".utf8)), cache: cache)
        await model.load()
        guard case .cached = model.state else {
            Issue.record("cached を期待した：\(model.state)")
            return
        }
        #expect(cache.stored == saved)
    }

    @Test("**起動時に 1 度取れていれば、あとで通信できなくても内容を出す**（W7-08。地図とシートで同じモデル）")
    func launchLoadKeepsTheCreditsForLater() async throws {
        let line = Line(.body(try Self.newMeta()))
        let model = Self.model(line, cache: MemoryCache())
        await model.load()  // 起動時
        line.reply = .offline
        await model.load()  // 「データについて」を開いたとき
        guard case .cached(let meta) = model.state else {
            Issue.record("cached を期待した：\(model.state)")
            return
        }
        #expect(meta.sources.count == 2)
    }

    @Test("**取れず、取っておいた内容も無ければ、取れなかったと言う**", arguments: [nil, Data("壊れた".utf8)])
    func saysItCouldNotLoad(_ saved: Data?) async {
        let model = Self.model(.offline, cache: MemoryCache(saved))
        await model.load()
        #expect(model.state == .failed(CreditsDisplay.failedText))
    }

    @Test("ファイルの置き場所は、書いたバイトをそのまま返す（無ければ nil）")
    func fileCacheRoundTrips() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("credits-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = FileMetaCache(directory: directory)
        #expect(cache.read() == nil)
        let body = try Self.newMeta()
        cache.write(body)
        #expect(cache.read() == body)
    }

    // MARK: - 外側の言葉

    @Test("**いつの内容かは、サーバーがそれを作った時刻で言う**（オフラインでも）")
    func tellsWhenTheContentWasMade() throws {
        let meta = try ContractTests.decode(MetaResponse.self, "meta")
        let live = try #require(
            CreditsDisplay(state: .live(meta), locale: Self.japan, timeZone: Self.tokyo))
        let offline = try #require(
            CreditsDisplay(state: .cached(meta), locale: Self.japan, timeZone: Self.tokyo))
        // generated_at は 2026-09-09T05:43:24.890Z ＝ 9/9 14:43（日本時間）
        #expect(live.asOf == "9/9 14:43 時点の内容です。")
        #expect(!live.isOffline)
        #expect(offline.asOf == "通信できないため、9/9 14:43 に取得した内容を表示しています。")
        #expect(offline.isOffline)
    }

    @Test("**配っている予測の版を出す**（W6-32）。まだ無ければそう言う")
    func showsTheModelVersion() throws {
        let meta = try ContractTests.decode(MetaResponse.self, "meta")
        let display = try #require(CreditsDisplay(state: .live(meta)))
        #expect(display.modelVersion == "予測モデル：baseline-b3-v0-20260908")

        var object = try #require(
            try JSONSerialization.jsonObject(with: ContractTests.fixture("meta")) as? [String: Any])
        object["model_version"] = NSNull()
        let none = try V1Client.makeDecoder().decode(
            MetaResponse.self, from: JSONSerialization.data(withJSONObject: object))
        #expect(CreditsDisplay(state: .live(none))?.modelVersion == "予測はまだ配信していません")
    }

    @Test("出す内容が無い状態では作らない")
    func hasNothingToShowWhileLoadingOrFailed() {
        #expect(CreditsDisplay(state: .loading) == nil)
        #expect(CreditsDisplay(state: .failed("x")) == nil)
    }
}

/// 予測時刻と観測時刻（W6 の PR J、W6-32、所見 196）。
@Suite("予測時刻と観測時刻（W6 の PR J）")
struct FreshnessLabelTests {
    static let japan = Locale(identifier: "ja_JP")
    static let tokyo = TimeZone(identifier: "Asia/Tokyo")!

    static func trip(_ name: String, editing edit: (inout [String: Any]) -> Void = { _ in })
        throws -> TripCheckResponse
    {
        var object = try #require(
            try JSONSerialization.jsonObject(with: ContractTests.fixture(name)) as? [String: Any])
        edit(&object)
        return try V1Client.makeDecoder().decode(
            TripCheckResponse.self, from: JSONSerialization.data(withJSONObject: object))
    }

    static func plan(_ response: TripCheckResponse) -> TripPlan {
        TripPlan(response: response, now: response.generatedAt, locale: japan, timeZone: tokyo)
    }

    @Test("**行程に予測時刻を添える**（両端の観測は 2026-09-13 06:19:51Z ＝ 15:19）")
    func tripCarriesTheForecastBasis() throws {
        let plan = Self.plan(try Self.trip("trip_check"))
        #expect(plan.basis == "15:19 時点の観測にもとづく予測")
    }

    @Test("**両端の観測が違えば、古いほうで言う**")
    func tripUsesTheOlderBasis() throws {
        let response = try Self.trip("trip_check") { object in
            var to = object["to"] as? [String: Any] ?? [:]
            var forecast = to["forecast"] as? [String: Any] ?? [:]
            forecast["base_observed_at"] = "2026-09-13T06:24:51.000Z"
            to["forecast"] = forecast
            object["to"] = to
        }
        #expect(Self.plan(response).basis == "15:19 時点の観測にもとづく予測")
    }

    @Test("片側だけ予測があれば、その時刻で言う。どちらにも無ければ出さない")
    func tripBasisNeedsAForecast() throws {
        #expect(Self.plan(try Self.trip("trip_check_no_trip")).basis == "15:19 時点の観測にもとづく予測")
        let none = try Self.trip("trip_check_no_trip") { object in
            var from = object["from"] as? [String: Any] ?? [:]
            from["forecast"] = NSNull()
            object["from"] = from
        }
        #expect(Self.plan(none).basis == nil)
    }

    @Test("**クレジットの前置きはサーバーのものを使う**（古い応答には無い）")
    func tripCarriesTheServerPreface() throws {
        #expect(Self.plan(try Self.trip("trip_check")).creditPreface == nil)
        let preface = "このアプリは、以下の著作物を改変して利用しています。"
        let response = try Self.trip("trip_check") { $0["attribution_preface"] = preface }
        #expect(Self.plan(response).creditPreface == preface)
    }

    // MARK: - 目的地の候補の「いま 返せる N」

    let now = Date(timeIntervalSince1970: 1_788_800_000)

    func feed(staleAfter: Int = 960) -> FeedStatus {
        FeedStatus(
            systemID: "hellocycling", displayName: "HELLO CYCLING", dataUpdatedAt: now,
            expectedCadenceSeconds: 300, staleAfterSeconds: staleAfter, isStale: false,
            capacityIsDynamic: false)
    }

    func station(observedAgo: TimeInterval, isPresent: Bool = true, unbounded: Bool = false)
        -> StationCurrent
    {
        StationCurrent(
            systemID: "hellocycling", stationID: "a", name: "テスト", latitude: 35.68,
            longitude: 139.77, capacity: 10, capacityEstimate: 12, capacityDays: 7,
            capacityUnbounded: unbounded, bikes: 3, docks: 4, isInstalled: true, isRenting: true,
            isReturning: true, isPresent: isPresent,
            observedAt: now.addingTimeInterval(-observedAgo), lastChangedAt: now, forecast: nil)
    }

    @Test("**いまの空きに観測時刻を添える**")
    func currentLineCarriesTheObservationTime() {
        #expect(
            station(observedAgo: 120).currentLine(for: .returnBike, feed: feed(), now: now)
                == "いま 返せる 4・2 分前の観測")
    }

    @Test("**上限なしのポートは「いま 返せる 上限なし」**（番兵の数を空き枠として読ませない。W7-05）")
    func currentLineForAnUnboundedStation() {
        let unbounded = station(observedAgo: 120, unbounded: true)
        #expect(
            unbounded.currentLine(for: .returnBike, feed: feed(), now: now)
                == "いま 返せる 上限なし・2 分前の観測")
        #expect(
            unbounded.currentLine(for: .borrow, feed: feed(), now: now) == "いま 借りられる 3・2 分前の観測",
            "台数は番兵ではないので、そのまま出す")
        #expect(
            station(observedAgo: 1_200, unbounded: true).currentLine(
                for: .returnBike, feed: feed(), now: now)
                == "いま 返せる —・20 分前の観測（更新が滞っています）",
            "古ければ「上限なし」も現在値として出さない")
    }

    @Test("**フィードを渡すと、古い値を現在値として出さない**（渡さないと閾値が分からなかった）")
    func currentLineHidesAStaleCount() {
        let stale = station(observedAgo: 1_200)
        #expect(
            stale.currentLine(for: .returnBike, feed: feed(), now: now)
                == "いま 返せる —・20 分前の観測（更新が滞っています）")
        #expect(
            stale.currentLine(for: .returnBike, feed: nil, now: now) == "いま 返せる 4・20 分前の観測",
            "フィード無しでは古さを判断できない（これが目的地の候補で起きていた）")
    }

    @Test("フィードに現れなかったポートは、観測時刻が不明と言う")
    func currentLineForAnAbsentStation() {
        #expect(
            station(observedAgo: 60, isPresent: false).currentLine(
                for: .returnBike, feed: feed(), now: now) == "いま 返せる —・観測時刻が不明")
    }
}
