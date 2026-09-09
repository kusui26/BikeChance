import Foundation
import Testing

@testable import BikeChanceCore

/// ポート詳細の組み立て（`StationDetail`、W3 プラン §5.11）。
///
/// **この 1 ファイルの主題は「分からないものを 0 と出さないこと」。**
/// 未観測・鮮度切れ・停止中はどれも「借りられない」ではなく「その数は出せない」で、
/// 混ぜると表示義務（CLAUDE.md §2 の 7・8）に反する。
@Suite("ポート詳細")
struct StationDetailTests {
    let now = Date(timeIntervalSince1970: 1_788_800_000)

    func feed(dynamicCapacity: Bool = false, staleAfter: Int = 960) -> FeedStatus {
        FeedStatus(
            systemID: "hellocycling",
            displayName: "HELLO CYCLING",
            dataUpdatedAt: now,
            expectedCadenceSeconds: 300,
            staleAfterSeconds: staleAfter,
            isStale: false,
            capacityIsDynamic: dynamicCapacity
        )
    }

    func station(
        name: String? = "丸の内中央口",
        observedAgo: TimeInterval? = 120,
        isPresent: Bool = true,
        capacity: Int? = 10,
        bikes: Int? = 3,
        docks: Int? = 7,
        isInstalled: Bool? = true,
        isRenting: Bool? = true,
        isReturning: Bool? = true,
        forecast: StationForecast? = nil
    ) -> StationCurrent {
        StationCurrent(
            systemID: "hellocycling",
            stationID: "10139",
            name: name,
            latitude: 35.681236,
            longitude: 139.767125,
            capacity: capacity,
            bikes: bikes,
            docks: docks,
            isInstalled: isInstalled,
            isRenting: isRenting,
            isReturning: isReturning,
            isPresent: isPresent,
            observedAt: observedAgo.map { now.addingTimeInterval(-$0) },
            lastChangedAt: now,
            forecast: forecast
        )
    }

    let attribution = Attribution(
        systemID: "hellocycling",
        provider: "公共交通オープンデータセンター",
        dataset: "HELLO CYCLING ステーション情報",
        license: "CC BY 4.0",
        licenseURL: URL(string: "https://creativecommons.org/licenses/by/4.0/")!
    )

    func detail(
        _ station: StationCurrent? = nil,
        feed: FeedStatus? = nil,
        attribution: Attribution? = nil,
        intent: RideIntent = .borrow,
        arrival: Date? = nil
    ) -> StationDetail {
        StationDetail(
            station: station ?? self.station(),
            feed: feed ?? self.feed(),
            attribution: attribution,
            intent: intent,
            arrival: arrival,
            now: now
        )
    }

    /// 30 分後に着く、という指定。**応答が指している時刻**を渡す。
    var arrival: Date { now.addingTimeInterval(30 * 60) }

    func forecast(bike: Double = 0.8, dock: Double = 0.4, confidence: Int = 3) -> StationForecast {
        StationForecast(
            rentProbability: bike, returnProbability: dock, confidence: confidence,
            baseObservedAt: now.addingTimeInterval(-120),
            modelVersion: "baseline-b3-v0-20260908")
    }

    func fact(_ detail: StationDetail, _ label: String) -> Fact? {
        detail.facts.first { $0.label == label }
    }

    // ── 見出し ────────────────────────────────────────────────
    @Test("名称と事業者を出す")
    func headline() {
        let result = detail()
        #expect(result.name == "丸の内中央口")
        #expect(result.systemName == "HELLO CYCLING")
    }

    @Test("名称がまだ取れていないときも、空欄にしない")
    func missingNameIsLabelled() {
        // 新しいポートは属性を最大 1 日持たない（データ辞書 §4.3）
        #expect(detail(station(name: nil)).name == "（名称未取得）")
    }

    @Test("事業者名が引けなければ system_id を出す")
    func systemIdIsTheFallback() {
        let result = StationDetail(station: station(), feed: nil, attribution: nil, now: now)
        #expect(result.systemName == "hellocycling")
    }

    // ── 台数 ──────────────────────────────────────────────────
    @Test("観測できていれば台数を出す")
    func countsAreShown() {
        let result = detail()
        #expect(result.availability.map(\.value) == ["3", "7"])
        #expect(result.availability.allSatisfy { $0.caution == nil })
    }

    @Test("**0 台は 0 と出す**（分からないのとは違う）")
    func zeroIsNotUnknown() {
        #expect(detail(station(bikes: 0)).availability[0].value == "0")
    }

    @Test("**未観測は「—」**。0 と区別する")
    func unobservedIsNotZero() {
        #expect(detail(station(bikes: nil)).availability[0].value == StationDetail.unknownValue)
        #expect(detail(station(bikes: nil)).availability[1].value == "7")
    }

    @Test("**鮮度が切れたら台数を出さない**（「現在値」として読ませない）")
    func staleCountsAreHidden() {
        let result = detail(station(observedAgo: 1200))
        #expect(result.availability.allSatisfy { $0.value == StationDetail.unknownValue })
        #expect(!result.freshness.isPresentable)
        #expect(result.freshness.label.contains("滞って"))
    }

    @Test("最新のフィードに現れなかったポートも台数を出さない")
    func absentStationsHideCounts() {
        let result = detail(station(observedAgo: nil, isPresent: false))
        #expect(result.freshness == .unknown)
        #expect(result.availability.allSatisfy { $0.value == StationDetail.unknownValue })
    }

    @Test("**停止中は数を出さず、理由を出す**")
    func suspendedShowsTheReasonInsteadOfTheCount() {
        // 「3 台あります」と出すと「借りられる」と読める。数を隠して理由を書く
        let result = detail(station(isRenting: false))
        #expect(result.availability[0].value == StationDetail.unknownValue)
        #expect(result.availability[0].caution == "貸出停止中")
        #expect(result.availability[1].value == "7")
        #expect(result.availability[1].caution == nil)
    }

    @Test("返却停止も同じ規則")
    func returningSuspensionFollowsTheSameRule() {
        let result = detail(station(isReturning: false))
        #expect(result.availability[1].value == StationDetail.unknownValue)
        #expect(result.availability[1].caution == "返却停止中")
    }

    @Test("停止しているか分からないときは注意書きを出さない")
    func unknownFlagsDoNotClaimSuspension() {
        let result = detail(station(isRenting: nil))
        #expect(result.availability[0].caution == nil)
        #expect(result.availability[0].value == "3")
    }

    // ── 容量 ──────────────────────────────────────────────────
    @Test("容量を出す")
    func capacityIsShown() {
        #expect(fact(detail(), "容量")?.value == "10")
        #expect(fact(detail(), "容量")?.note == nil)
    }

    @Test("容量がまだ取れていなければ「—」")
    func missingCapacity() {
        #expect(fact(detail(station(capacity: nil)), "容量")?.value == StationDetail.unknownValue)
    }

    @Test("**容量が動的なシステムでは数を出さない**（W4 プラン §12 の 115）")
    func dynamicCapacityIsNotShown() {
        // ドコモの capacity は日次同期の瞬間の bikes + docks で、ラック数ではない。
        // 出すと「容量 10・借りられる 27」のように同じ画面の 2 つの数が矛盾する
        let result = detail(station(capacity: 10, bikes: 27), feed: feed(dynamicCapacity: true))
        #expect(fact(result, "容量")?.value == StationDetail.unknownValue)
        #expect(result.availability[0].value == "27")
    }

    @Test("数を出さない代わりに、なぜ出せないかを添える")
    func dynamicCapacityIsExplained() {
        let result = detail(feed: feed(dynamicCapacity: true))
        #expect(fact(result, "容量")?.note == StationDetail.dynamicCapacityNote)
        #expect(fact(result, "容量")?.note?.contains("固定のラック数") == true)
    }

    @Test("**サーバーが数を送ってきても出さない**（古いキャッシュや別経路への備え）")
    func aStaleNumberIsStillNotShown() {
        // 0035 で `/v1` は NULL を返すが、CDN に残った古い応答は数を持っている
        let result = detail(station(capacity: 999), feed: feed(dynamicCapacity: true))
        #expect(fact(result, "容量")?.value == StationDetail.unknownValue)
    }

    @Test("固定のシステムはそのまま出す（注記も付けない）")
    func fixedCapacityIsShownAsIs() {
        let result = detail(feed: feed(dynamicCapacity: false))
        #expect(fact(result, "容量")?.value == "10")
        #expect(fact(result, "容量")?.note == nil)
    }

    @Test("**「未取得」と「公開していない」を区別する**")
    func anUnsyncedCapacityIsNotTheSameAsADynamicOne() {
        // 固定のシステムで属性がまだ無いだけなら、理由は言えない（注記は出さない）
        let unsynced = detail(station(capacity: nil), feed: feed(dynamicCapacity: false))
        #expect(fact(unsynced, "容量")?.value == StationDetail.unknownValue)
        #expect(fact(unsynced, "容量")?.note == nil)
        // 動的なシステムは「公開していない」と言える
        let dynamic = detail(station(capacity: nil), feed: feed(dynamicCapacity: true))
        #expect(fact(dynamic, "容量")?.note == StationDetail.dynamicCapacityNote)
    }

    // ── その他の情報 ──────────────────────────────────────────
    @Test("設置の状態を言葉で出す")
    func installationState() {
        #expect(fact(detail(), "設置")?.value == "設置されています")
        #expect(fact(detail(station(isInstalled: false)), "設置")?.value == "撤去・休止中")
        #expect(
            fact(detail(station(isInstalled: nil)), "設置")?.value == StationDetail.unknownValue)
    }

    @Test("座標は 6 桁で出す（ポートの座標なので丸めない）")
    func coordinateIsNotRounded() {
        // 端末の位置は量子化して送るが（CLAUDE.md §5）、ポートの座標は公開データ
        #expect(fact(detail(), "座標")?.value == "35.681236, 139.767125")
    }

    @Test("ポート ID はシステムと組で出す")
    func portIdIncludesTheSystem() {
        #expect(fact(detail(), "ポート ID")?.value == "hellocycling / 10139")
    }

    @Test("情報の並びが決まっている")
    func factsAreOrdered() {
        #expect(detail().facts.map(\.label) == ["容量", "設置", "座標", "ポート ID"])
    }

    // ── クレジット ────────────────────────────────────────────
    @Test("**クレジットを出す**（CC BY 4.0 の表示義務）")
    func creditIsShown() {
        let result = detail(attribution: attribution)
        #expect(result.credit?.contains("公共交通オープンデータセンター") == true)
        #expect(result.credit?.contains("CC BY 4.0") == true)
    }

    @Test("クレジットが引けなければ nil（作り話をしない）")
    func missingCreditIsNil() {
        #expect(detail().credit == nil)
    }

    @Test("クレジットはシステムごとに引ける")
    func creditIndexIsKeyedBySystem() {
        let response = StationsResponse(
            apiVersion: "1",
            generatedAt: now,
            bbox: BboxEnvelope(west: 139.7, south: 35.6, east: 139.8, north: 35.7),
            count: 0,
            isStale: false,
            forecastInMinutes: nil,
            feeds: [],
            stations: [],
            attribution: [attribution]
        )
        #expect(response.attributionIndex()["hellocycling"] == attribution)
        #expect(response.attributionIndex()["docomo-cycle"] == nil)
    }

    // ── 一覧の identity ───────────────────────────────────────
    @Test("行の id が重ならない（ForEach が崩れない）")
    func rowIdentitiesAreUnique() {
        let result = detail()
        #expect(Set(result.availability.map(\.id)).count == result.availability.count)
        #expect(Set(result.facts.map(\.id)).count == result.facts.count)
    }
}

/// 詳細画面の予測（W4 の PR B）。
///
/// **主題は「予測と現在値を混ぜないこと」。** 予測が出せなくても台数は出るし、
/// 台数が出せなくても予測は出る。片方の欠けがもう片方を消してはいけない。
@Suite("ポート詳細の予測")
struct StationDetailForecastTests {
    let base = StationDetailTests()

    @Test("**到着を選んでいなければ予測の欄は出ない**")
    func noArrivalMeansNoForecastSection() {
        #expect(base.detail().forecast == .notRequested)
    }

    @Test("到着を選べば確率が出る")
    func showsTheProbabilityForTheArrival() throws {
        let station = base.station(forecast: base.forecast())
        let display = try #require(
            base.detail(station, arrival: base.arrival).forecast.display)
        #expect(display.headline == "借りられる可能性 80%")
    }

    @Test("借りる／返すで見る数が変わる")
    func theIntentPicksTheProbability() throws {
        let station = base.station(forecast: base.forecast(bike: 0.8, dock: 0.4))
        let borrow = try #require(base.detail(station, arrival: base.arrival).forecast.display)
        let giveBack = try #require(
            base.detail(station, intent: .returnBike, arrival: base.arrival).forecast.display)
        #expect(borrow.percent == 80)
        #expect(giveBack.percent == 40)
    }

    @Test("**予測が無くても台数は出す**（現在値は別の情報）")
    func theCountsSurviveAMissingForecast() {
        let result = base.detail(base.station(forecast: nil), arrival: base.arrival)
        #expect(result.forecast == .unavailable(ForecastState.unavailableText))
        #expect(result.availability[0].value == "3")
        #expect(result.availability[1].value == "7")
    }

    @Test("**台数が出せなくても予測は出す**（鮮度切れでも予測は別に判定する）")
    func theForecastSurvivesUnknownCounts() throws {
        // 未観測のポート。台数は「—」になるが、予測が来ていれば出す
        let station = base.station(bikes: nil, docks: nil, forecast: base.forecast())
        let result = base.detail(station, arrival: base.arrival)
        #expect(result.availability[0].value == StationDetail.unknownValue)
        #expect(try #require(result.forecast.display).percent == 80)
    }

    @Test("停止中でも予測の欄は独立している（サーバーが出さない側で決まる）")
    func aStoppedStationStillHasItsOwnForecastRule() {
        // 貸出停止のポートには、そもそもサーバーが予測を作らない（is_predictable）
        let station = base.station(isRenting: false, forecast: nil)
        let result = base.detail(station, arrival: base.arrival)
        #expect(result.availability[0].caution == "貸出停止中")
        #expect(result.forecast == .unavailable(ForecastState.unavailableText))
    }
}
