import Foundation
import Testing

@testable import BikeChanceCore

/// 行程チェックの判断（`TripCheck.swift`、W5 プラン §6.7）。
///
/// **主題は「掛け算を出してよいのはどんなときか」。** 画面のいちばん大きい字になるのは
/// 2 つの確率を掛けた 1 つの数で、**条件を 1 つ落とすと、根拠のない数字がそこに出る。**
///
/// フィクスチャは **2026-09-13（日）に本番から取った実応答**である。日曜なので成果物に
/// `sun_holiday` のセルが無く、**`confidence` は両端とも 2**（W5 §12 の 149）。
/// 「小さいほうを取る」を 2 と 3 で見分けたい検査は、応答を書き換えて作る。
@Suite("行程チェック")
struct TripCheckTests {
    /// 東京（JST）で固定して読む。**端末の設定で表示が変わらないようにする。**
    static let jst = TimeZone(identifier: "Asia/Tokyo")!
    static let ja = Locale(identifier: "ja_JP")

    static func response(_ name: String = "trip_check") throws -> TripCheckResponse {
        try ContractTests.decode(TripCheckResponse.self, name)
    }

    /// フィクスチャの JSON を書き換えてから読む。**実応答に無い形を作るための口。**
    static func patched(
        _ name: String = "trip_check", _ change: (inout [String: Any]) -> Void
    ) throws -> TripCheckResponse {
        let raw = try ContractTests.fixture(name)
        var object = try #require(
            try JSONSerialization.jsonObject(with: raw) as? [String: Any], "object のはず")
        change(&object)
        let patched = try JSONSerialization.data(withJSONObject: object)
        return try V1Client.makeDecoder().decode(TripCheckResponse.self, from: patched)
    }

    static func plan(_ response: TripCheckResponse, now: Date? = nil) -> TripPlan {
        TripPlan(
            response: response, now: now ?? response.generatedAt, locale: ja, timeZone: jst)
    }

    // MARK: - 掛け算を出してよい条件

    @Test("両端の確率が出ていれば、行程の見通しも出る")
    func showsTheTripWhenBothEndsHaveProbabilities() throws {
        let plan = Self.plan(try Self.response())
        #expect(plan.from.forecast.display != nil)
        #expect(plan.to.forecast.display != nil)
        let display = try #require(plan.outcome.display)
        // 0.05 × 0.029 ＝ 0.001 → 5% 刻みで 0%。**0 でも欄は出す**（出せない、とは違う）
        #expect(display.percent == 0)
        #expect(display.headline == "両方うまくいく可能性 0%")
    }

    @Test("**片側の予測が無ければ、欄ごと消える**（0% と出さない。完了条件 2・契約 10）")
    func hidesTheTripWhenOneEndHasNoForecast() throws {
        let response = try Self.response("trip_check_no_trip")
        #expect(response.trip == nil, "前提：サーバーも trip を返していない")
        let plan = Self.plan(response)
        #expect(plan.to.forecast.display == nil, "到着側の予測が無い")
        #expect(plan.outcome == .unavailable(TripOutcomeState.unavailableText))
        #expect(plan.outcome.display == nil)
    }

    @Test("**フィードが滞っていれば、サーバーが trip を返していても消す**")
    func hidesTheTripWhenTheFeedIsStale() throws {
        let response = try Self.patched { object in
            var feeds = object["feeds"] as! [[String: Any]]
            for index in feeds.indices where feeds[index]["system_id"] as? String == "docomo-cycle"
            {
                feeds[index]["stale"] = true
            }
            object["feeds"] = feeds
        }
        #expect(response.trip != nil, "前提：サーバーは trip を返している")
        let plan = Self.plan(response)
        // 両端の確率は `ForecastState` が伏せる（`staleFeedText`）
        #expect(plan.from.forecast == .unavailable(ForecastState.staleFeedText))
        #expect(plan.to.forecast == .unavailable(ForecastState.staleFeedText))
        // **隠したはずの数から作った掛け算だけが残らない**
        #expect(plan.outcome.display == nil)
    }

    @Test("**注記の無い確率は出さない**（W4-24。独立仮定を伏せた数は断定になる）")
    func refusesToShowATripProbabilityWithoutItsNotice() throws {
        let response = try Self.patched { object in
            var trip = object["trip"] as! [String: Any]
            trip["notice"] = ""
            object["trip"] = trip
        }
        #expect(response.trip?.notice == "")
        #expect(Self.plan(response).outcome.display == nil)
    }

    @Test("出せるときは注記が必ず付いている")
    func theTripAlwaysCarriesItsNotice() throws {
        let display = try #require(Self.plan(try Self.response()).outcome.display)
        #expect(!display.notice.isEmpty)
        #expect(display.notice.contains("互いに影響しないものとみなして"))
        // **読み上げも注記まで読む**（見える文字と読まれる文字を食い違わせない）
        #expect(display.accessibilityLabel.hasSuffix(display.notice))
    }

    // MARK: - confidence は行程のもの

    @Test("**参考値かどうかは行程の confidence で決まる**（端点のものではない）")
    func referenceFlagComesFromTheTripConfidence() throws {
        let response = try Self.patched { object in
            // 端点は確かで（3）、行程だけが薄い（1）——ありえない組み合わせだが、
            // **どちらを見ているか**はこれでしか見分けられない
            for side in ["from", "to"] {
                var station = object[side] as! [String: Any]
                var forecast = station["forecast"] as! [String: Any]
                forecast["confidence"] = 3
                station["forecast"] = forecast
                object[side] = station
            }
            var trip = object["trip"] as! [String: Any]
            trip["confidence"] = 1
            object["trip"] = trip
        }
        let plan = Self.plan(response)
        #expect(plan.from.forecast.display?.isReference == false, "端点は参考値ではない")
        #expect(plan.outcome.display?.isReference == true, "行程は参考値")
    }

    @Test("行程の confidence が 2 以上なら参考値にしない")
    func aConfidentTripIsNotMarkedAsReference() throws {
        #expect(Self.plan(try Self.response()).outcome.display?.isReference == false)
    }

    // MARK: - 時刻は絶対で、generated_at から

    @Test("**時刻は `generated_at` から数える**（端末の「いま」からではない）")
    func clocksAreAnchoredOnGeneratedAt() throws {
        let response = try Self.response()
        // 応答が CDN に 3 分留まっていた、を模す。**表示は 1 分も動かない**
        let stale = Self.plan(response, now: response.generatedAt.addingTimeInterval(180))
        let fresh = Self.plan(response, now: response.generatedAt)
        #expect(stale.from.clock == fresh.from.clock)
        #expect(stale.to.clock == fresh.to.clock)
        #expect(stale.outcome.display?.window == fresh.outcome.display?.window)
    }

    @Test("出発と到着は depart_in_min・arrive_in_min のぶんだけ先")
    func clocksMatchTheRequestedOffsets() throws {
        let response = try Self.response()
        #expect(response.departInMinutes == 10)
        #expect(response.arriveInMinutes == 24)
        #expect(response.departure.timeIntervalSince(response.generatedAt) == 600)
        #expect(response.arrival.timeIntervalSince(response.generatedAt) == 1440)
        let plan = Self.plan(response)
        let departure = Arrival.clockText(response.departure, locale: Self.ja, timeZone: Self.jst)
        #expect(plan.from.clock == "\(departure) 出発")
        #expect(plan.outcome.display?.window.hasPrefix("\(departure) 出発 → ") == true)
    }

    @Test("**「約 N 分後」とは書かない**（表示に相対の語を混ぜない）")
    func doesNotWriteRelativeTimes() throws {
        let plan = Self.plan(try Self.response())
        let texts = [
            plan.from.clock, plan.to.clock, plan.outcome.display?.window ?? "",
            plan.outcome.display?.headline ?? "",
        ]
        for text in texts {
            #expect(!text.contains("分後"), "相対の語が混ざっている: \(text)")
            #expect(!text.contains("約"), "曖昧な語が混ざっている: \(text)")
        }
    }

    // MARK: - 端点

    @Test("借りる側は自転車、返す側は空きを見る")
    func eachEndLooksAtItsOwnProbability() throws {
        let response = try Self.response()
        let plan = Self.plan(response)
        #expect(plan.from.intent == .borrow)
        #expect(plan.to.intent == .returnBike)
        let borrow = try #require(response.from.forecast).rentProbability
        let give = try #require(response.to.forecast).returnProbability
        #expect(plan.from.forecast.display?.percent == ForecastDisplay.percent(of: borrow))
        #expect(plan.to.forecast.display?.percent == ForecastDisplay.percent(of: give))
    }

    @Test("**台数は詳細画面と同じ規則を通る**（停止中は数を出さず、理由を出す）")
    func countsFollowTheSameRuleAsTheDetailScreen() throws {
        let response = try Self.patched { object in
            var station = object["from"] as! [String: Any]
            station["is_renting"] = false
            object["from"] = station
        }
        let plan = Self.plan(response)
        #expect(plan.from.count.value == StationDetail.unknownValue)
        #expect(plan.from.count.caution == "貸出停止中")
        #expect(plan.to.count.value == "0", "到着側は空きが 0 台（観測された 0 は隠さない）")
    }

    @Test("**`feeds` は 2 系統ぶん来る。行程の系統のものを引く**")
    func picksTheFeedOfTheTripSystem() throws {
        let response = try Self.response()
        #expect(response.feeds.count == 2, "前提：地図と同じ形で 2 系統ぶん来る")
        #expect(response.feeds.first?.systemID != response.systemID, "前提：先頭は別の系統")
        #expect(response.feed?.systemID == "docomo-cycle")
        #expect(Self.plan(response).systemName == "ドコモ・バイクシェア")
    }

    @Test("クレジットも行程の系統のものを出す（表示は義務）")
    func creditIsForTheTripSystem() throws {
        let plan = Self.plan(try Self.response())
        let credit = try #require(plan.credit)
        #expect(credit.contains("ドコモ"))
        #expect(credit.contains("creativecommons.org"))
    }

    // MARK: - 代替候補

    @Test("**代替候補を並べ替えない**（順序はサーバーが決める。W4-25）")
    func keepsTheServersOrderOfAlternatives() throws {
        let response = try Self.response()
        let plan = Self.plan(response)
        let sameSystem = { (side: [TripAlternative]) in
            side.filter { $0.station.systemID == response.systemID }.map(\.station.stationID)
        }
        #expect(plan.from.alternatives.map(\.stationID) == sameSystem(response.alternatives.from))
        #expect(plan.to.alternatives.map(\.stationID) == sameSystem(response.alternatives.to))
    }

    @Test("**別事業者のポートは候補にしない**（借りられるのは同じ事業者の自転車だけ）")
    func dropsAlternativesFromAnotherSystem() throws {
        let response = try Self.response()
        // **このフィクスチャは壊れた本番の応答である**（2026-09-13、§12 の 158）。
        // 虎ノ門の行程に厚木・新横浜・蘇我のポートが「80〜362 m 先」として入っていた
        let intruders = response.alternatives.from.filter {
            $0.station.systemID != response.systemID
        }
        #expect(!intruders.isEmpty, "前提：この応答には別系統が混ざっている")
        let plan = Self.plan(response)
        #expect(plan.from.alternatives.count == response.alternatives.from.count - intruders.count)
        for intruder in intruders {
            #expect(!plan.from.alternatives.contains { $0.stationID == intruder.station.stationID })
        }
    }

    @Test("代替候補の確率も、その端点の意図で出す")
    func alternativesUseTheIntentOfTheirEnd() throws {
        let response = try Self.response()
        let plan = Self.plan(response)
        let first = try #require(response.alternatives.from.first?.station.forecast)
        #expect(
            plan.from.alternatives.first?.forecast.display?.percent
                == ForecastDisplay.percent(of: first.rentProbability))
        let arriving = try #require(response.alternatives.to.first?.station.forecast)
        #expect(
            plan.to.alternatives.first?.forecast.display?.percent
                == ForecastDisplay.percent(of: arriving.returnProbability))
    }

    @Test("距離は m で出す（徒歩で歩く距離）")
    func showsTheWalkingDistance() throws {
        let response = try Self.response()
        let plan = Self.plan(response)
        let meters = try #require(response.alternatives.from.first?.distanceMeters)
        #expect(plan.from.alternatives.first?.distance == "\(meters) m")
        #expect(meters <= 400, "サーバーは 400 m までしか返さない")
    }

    // MARK: - 乗車時間

    @Test("**概算には但し書きを付ける**（楽観側に外れることを言う）")
    func saysWhenTheRideTimeIsAnEstimate() throws {
        let response = try Self.response()
        #expect(response.rideMinutesEstimated)
        let plan = Self.plan(response)
        #expect(plan.ride.label == "自転車 14 分")
        #expect(plan.ride.note == RideSummary.estimatedNote)
        #expect(plan.ride.note?.contains("長く") == true, "短く出ることを伝えている")
    }

    @Test("端末が所要を出したときは但し書きを付けない")
    func noNoteWhenTheDeviceSuppliedTheRideTime() throws {
        let response = try Self.patched { object in object["ride_min_estimated"] = false }
        let plan = Self.plan(response)
        #expect(!plan.ride.isEstimated)
        #expect(plan.ride.note == nil)
    }

    // MARK: - 丸め

    @Test("確率の丸めは地図と同じ（5% 刻み・上限 99%）")
    func roundsLikeTheMap() throws {
        let response = try Self.patched { object in
            var trip = object["trip"] as! [String: Any]
            trip["p_trip"] = 0.999
            object["trip"] = trip
        }
        #expect(Self.plan(response).outcome.display?.percent == ForecastDisplay.maxPercent)
    }
}
