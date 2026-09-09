import Foundation
import Testing

@testable import BikeChanceCore

/// 予測の見せ方（`ForecastDisplay` / `ForecastState`、開発プラン §9.3）。
///
/// **この 1 ファイルの主題は「言い過ぎないこと」。** 守るのは 4 つ。
///   * **断定しない**（「借りられます」ではなく「借りられる可能性 80%」）
///   * **過度な精度を見せない**（5% 刻み）。**100% とは言わない**（上限 99%）
///   * **予測と現在値を混ぜない**（出せないときも台数は出す）
///   * **指しているのは到着の時刻**。端末の「いま」からの分数ではない（§12 の 114）
@Suite("予測の見せ方")
struct ForecastTests {
    let jst = TimeZone(identifier: "Asia/Tokyo")!
    let ja = Locale(identifier: "ja_JP")
    /// 2026-09-09 14:03:20 JST。
    let now = Date(timeIntervalSince1970: 1_788_930_200)

    func forecast(
        bike: Double = 0.8, dock: Double = 0.4, confidence: Int = 3, basedAgo: TimeInterval = 180
    )
        -> StationForecast
    {
        StationForecast(
            rentProbability: bike,
            returnProbability: dock,
            confidence: confidence,
            baseObservedAt: now.addingTimeInterval(-basedAgo),
            modelVersion: "baseline-b3-v0-20260908"
        )
    }

    func station(forecast: StationForecast?) -> StationCurrent {
        StationCurrent(
            systemID: "hellocycling", stationID: "10139", name: "丸の内", latitude: 35.68,
            longitude: 139.77, capacity: 10, bikes: 3, docks: 7, isInstalled: true,
            isRenting: true, isReturning: true, isPresent: true, observedAt: now,
            lastChangedAt: now, forecast: forecast
        )
    }

    func feed(stale: Bool = false) -> FeedStatus {
        FeedStatus(
            systemID: "hellocycling", displayName: "HELLO CYCLING", dataUpdatedAt: now,
            expectedCadenceSeconds: 300, staleAfterSeconds: 960, isStale: stale,
            capacityIsDynamic: false)
    }

    func state(
        forecast: StationForecast? = nil,
        feedIsStale: Bool = false,
        intent: RideIntent = .borrow,
        arrival: Date? = nil
    ) -> ForecastState {
        ForecastState.make(
            station: station(forecast: forecast),
            feed: feed(stale: feedIsStale),
            intent: intent,
            arrival: arrival ?? now.addingTimeInterval(30 * 60),
            locale: ja,
            timeZone: jst
        )
    }

    // ── 百分率 ────────────────────────────────────────────────
    @Test("**5% 刻みに丸める**（過度な精度を見せない）")
    func roundsToFivePercentSteps() {
        #expect(ForecastDisplay.percent(of: 0.8) == 80)
        #expect(ForecastDisplay.percent(of: 0.82) == 80)
        #expect(ForecastDisplay.percent(of: 0.83) == 85)
        #expect(ForecastDisplay.percent(of: 0.114) == 10)
    }

    @Test("**100% とは言わない**（上限 99%）")
    func neverPromisesCertainty() {
        #expect(ForecastDisplay.percent(of: 1.0) == 99)
        #expect(ForecastDisplay.percent(of: 0.999) == 99)
        #expect(ForecastDisplay.percent(of: 0.98) == 99)
    }

    @Test("0 は 0 のまま（「低い」と「出せない」は別）")
    func zeroStaysZero() {
        #expect(ForecastDisplay.percent(of: 0) == 0)
        #expect(ForecastDisplay.percent(of: 0.006) == 0)
    }

    @Test("**どんな確率でも 5 の倍数か、上限の 99**（例外は上限だけ）")
    func everyProbabilityLandsOnTheGrid() {
        // 5% 刻みと「100% と言わない」は上の端でぶつかる。100 を 99 に落とすので、
        // **99 だけが刻みから外れる**。落とし先を 95 にすると 0.96 と 0.999 が同じ表示になる
        for step in 0...1000 {
            let percent = ForecastDisplay.percent(of: Double(step) / 1000)
            let onGrid = percent % ForecastDisplay.stepPercent == 0
            #expect(onGrid || percent == ForecastDisplay.maxPercent, "\(percent)% は出せない値")
            #expect(percent >= 0 && percent <= ForecastDisplay.maxPercent)
        }
    }

    @Test("上限に落ちるのは 97.5% 以上のときだけ")
    func onlyTheVeryTopIsCapped() {
        #expect(ForecastDisplay.percent(of: 0.974) == 95)
        #expect(ForecastDisplay.percent(of: 0.975) == ForecastDisplay.maxPercent)
    }

    // ── 帯 ────────────────────────────────────────────────────
    @Test("帯の境目は 85% と 60%（開発プラン §9.3 の表）")
    func theBandsMatchThePlan() {
        #expect(ProbabilityBand.of(percent: 85) == .high)
        #expect(ProbabilityBand.of(percent: 84) == .medium)
        #expect(ProbabilityBand.of(percent: 60) == .medium)
        #expect(ProbabilityBand.of(percent: 59) == .low)
        #expect(ProbabilityBand.of(percent: 0) == .low)
    }

    @Test("**帯は丸めた後の数で決める**（85% と出して「余裕を持って」にしない）")
    func theBandFollowsTheDisplayedNumber() throws {
        // 0.849 は 85% に丸まる。帯も高でなければ、画面の中で食い違う
        let display = try #require(state(forecast: forecast(bike: 0.849)).display)
        #expect(display.percent == 85)
        #expect(display.band == .high)
    }

    // ── 文言 ──────────────────────────────────────────────────
    @Test("**断定しない**（「借りられます」ではなく「借りられる可能性 80%」）")
    func neverStatesItAsAFact() throws {
        let display = try #require(state(forecast: forecast()).display)
        #expect(display.headline == "借りられる可能性 80%")
        #expect(!display.headline.contains("借りられます"))
    }

    @Test("返すほうは返すほうの確率を出す（混ぜない）")
    func theReturnIntentUsesTheDockProbability() throws {
        let display = try #require(
            state(forecast: forecast(bike: 0.8, dock: 0.4), intent: .returnBike).display)
        #expect(display.headline == "返せる可能性 40%")
    }

    @Test("**指しているのは到着の時刻**（応答が持っている値を出す）")
    func theArrivalComesFromTheResponse() throws {
        // 端末の「いま」から数え直さない。応答が指す時刻をそのまま出す
        let display = try #require(
            state(forecast: forecast(), arrival: Date(timeIntervalSince1970: 1_788_932_100)).display
        )
        #expect(display.arrival == "14:35 到着")
    }

    @Test("**何時時点の観測にもとづくかを添える**（開発プラン §9.2 の表示義務）")
    func theBasisIsAlwaysShown() throws {
        let display = try #require(state(forecast: forecast(basedAgo: 180)).display)
        #expect(display.basis == "14:00 時点の観測にもとづく予測")
    }

    @Test("確度が低ければ参考値（新設ポート・鮮度不良・再配置直後）")
    func lowConfidenceIsMarkedAsAReference() throws {
        #expect(try #require(state(forecast: forecast(confidence: 3)).display).isReference == false)
        #expect(try #require(state(forecast: forecast(confidence: 2)).display).isReference == false)
        #expect(try #require(state(forecast: forecast(confidence: 1)).display).isReference)
        #expect(try #require(state(forecast: forecast(confidence: 0)).display).isReference)
    }

    @Test("読み上げは時刻から始める（数字だけを読ませない）")
    func theAccessibilityLabelLeadsWithTheTime() throws {
        let display = try #require(state(forecast: forecast(confidence: 1)).display)
        #expect(display.accessibilityLabel.hasPrefix(display.arrival))
        #expect(display.accessibilityLabel.contains("借りられる可能性 80%"))
        #expect(display.accessibilityLabel.contains("参考値"))
    }

    // ── 出せないとき ──────────────────────────────────────────
    @Test("**到着を選んでいなければ予測を出さない**（現在値は予測ではない）")
    func noArrivalMeansNoForecast() {
        // 予測そのものは持っていても、頼まれていなければ出さない
        let result = ForecastState.make(
            station: station(forecast: forecast()), feed: feed(), intent: .borrow, arrival: nil)
        #expect(result == .notRequested)
    }

    @Test("予測が無ければ「いまは予測できません」（理由は言わない）")
    func missingForecastSaysSoWithoutAReason() {
        #expect(state(forecast: nil) == .unavailable(ForecastState.unavailableText))
        #expect(ForecastState.unavailableText == "いまは予測できません")
    }

    @Test("**フィードが滞っていれば予測を隠す**（開発プラン §9.2）")
    func aStaleFeedHidesTheForecast() {
        // 台数が信用できないときの確率は、数字の形をしているぶんだけ余計に信用される
        #expect(
            state(forecast: forecast(), feedIsStale: true)
                == .unavailable(ForecastState.staleFeedText))
    }

    @Test("出せないときは帯も無い（地図は灰色になる）")
    func thereIsNoBandWhenThereIsNoForecast() {
        #expect(state(forecast: nil).band == nil)
        #expect(ForecastState.notRequested.band == nil)
        #expect(state(forecast: forecast()).band == .medium)
    }
}
