import Foundation
import Testing

@testable import BikeChanceCore

/// **契約テスト**：`/v1` が実際に返した応答をそのままデコードする。
///
/// `Fixtures/` の JSON は 2026-09-08 に本番から取ったもの。サーバー側のスキーマを
/// 変えたら、ここが落ちて気づける（CLAUDE.md §3 の「契約テスト」）。
@Suite("契約（/v1 の実応答）")
struct ContractTests {
    static func fixture(_ name: String) throws -> Data {
        let url = try #require(
            Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures"),
            "フィクスチャが見つかりません: \(name).json"
        )
        return try Data(contentsOf: url)
    }

    static func decode<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
        try V1Client.makeDecoder().decode(type, from: fixture(name))
    }

    @Test("/v1/stations をデコードできる")
    func decodesStations() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        #expect(response.apiVersion == "v1")
        #expect(response.count == response.stations.count)
        #expect(response.count > 0)
        #expect(response.feeds.count == 2)
        #expect(response.attribution.count == 2)
    }

    @Test("実効矩形が格子にそろっている（サーバーと同じ丸め）")
    func serverEchoesTheQuantizedBbox() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        // このフィクスチャは 139.7654,35.6743,139.7712,35.6821 で要求したもの
        let requested = Bbox(west: 139.7654, south: 35.6743, east: 139.7712, north: 35.6821)
        #expect(response.bbox.bbox == requested.quantized())
    }

    @Test("返ってきたポートは実効矩形の中にある")
    func stationsAreInsideTheBbox() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        let bbox = response.bbox.bbox
        for station in response.stations {
            #expect(bbox.contains(latitude: station.latitude, longitude: station.longitude))
        }
    }

    @Test("未観測は 0 ではなく nil として読める")
    func missingValuesAreNil() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        // 実データに未観測が無いこともあるので、型として nil を受けられることを確かめる
        let json = """
            {"system_id":"docomo-cycle","station_id":"x","name":null,"lat":35.0,"lon":139.0,
             "capacity":null,"bikes":null,"docks":null,"is_installed":null,"is_renting":null,
             "is_returning":null,"is_present":false,"observed_at":null,
             "last_changed_at":"2026-09-08T00:00:00.000Z"}
            """
        let station = try V1Client.makeDecoder().decode(
            StationCurrent.self, from: Data(json.utf8))
        #expect(station.bikes == nil)
        #expect(station.isRenting == nil)
        #expect(station.observedAt == nil)
        #expect(!response.stations.isEmpty)
    }

    @Test("/v1/meta をデコードできる（クレジットと通知文つき）")
    func decodesMeta() throws {
        let meta = try Self.decode(MetaResponse.self, "meta")
        #expect(meta.apiVersion == "v1")
        #expect(meta.feeds.count == 2)
        #expect(meta.attribution.count == 2)
        #expect(meta.notice.contains("公共交通事業者への直接の問合せは行わないでください"))
        #expect(meta.disclaimer.contains("独自に予測"))
        // W4 の PR A から、いま配信している版が入る（それまでは常に nil だった）
        #expect(meta.modelVersion?.isEmpty == false)
    }

    @Test("鮮度の閾値が応答に入っている（端末で同じ判定ができる）")
    func feedsCarryTheStaleThreshold() throws {
        let meta = try Self.decode(MetaResponse.self, "meta")
        for feed in meta.feeds {
            #expect(feed.staleAfterSeconds > feed.expectedCadenceSeconds)
        }
    }

    @Test("クレジットを 1 行に組み立てられる")
    func buildsTheCreditLine() throws {
        let meta = try Self.decode(MetaResponse.self, "meta")
        let credit = try #require(meta.attribution.first).credit
        #expect(credit.contains("クリエイティブ・コモンズ"))
        #expect(credit.contains("creativecommons.org"))
    }

    // ── ポート詳細（W3 プラン §5.11）────────────────────────
    @Test("**実応答からポート詳細を組み立てられる**")
    func buildsTheDetailFromTheRealResponse() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        let feeds = response.feedIndex()
        let credits = response.attributionIndex()
        // 応答が作られた時刻を「いま」とみなす。実際の鮮度で判定するため
        let now = response.generatedAt

        for station in response.stations {
            let detail = StationDetail(
                station: station,
                feed: feeds[station.systemID],
                attribution: credits[station.systemID],
                now: now
            )
            // 名前が無いポートでも空欄にしない
            #expect(!detail.name.isEmpty)
            // **クレジットは全ポートで引ける**（表示義務。CC BY 4.0）
            #expect(detail.credit != nil)
            #expect(detail.facts.map(\.label) == ["容量", "設置", "座標", "ポート ID"])
            #expect(detail.availability.count == 2)
        }
    }

    @Test("実応答では大半のポートで台数が出る（鮮度も通る）")
    func realStationsShowTheirCounts() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        let feeds = response.feedIndex()
        let now = response.generatedAt
        let shown = response.stations.filter { station in
            let detail = StationDetail(
                station: station, feed: feeds[station.systemID], attribution: nil, now: now)
            return detail.availability[0].value != StationDetail.unknownValue
        }
        // 取得直後の応答なので、ほとんどのポートは「現在値」として出せるはず
        #expect(shown.count > response.stations.count / 2)
    }

    @Test("**動的な容量のポートには、実応答でも数を出さない**（W4 プラン §12 の 115）")
    func docomoStationsNeverShowACapacityNumber() throws {
        // このフィクスチャは 0035 より前に取ったもので、ドコモの capacity に数が入っている。
        // **サーバーが送ってきても画面には出さない**ことを、実データで固定する
        let response = try Self.decode(StationsResponse.self, "stations")
        let feeds = response.feedIndex()
        let dynamic = response.stations.filter { feeds[$0.systemID]?.capacityIsDynamic == true }
        #expect(!dynamic.isEmpty, "動的容量のポートがフィクスチャに無い")
        for station in dynamic {
            let detail = StationDetail(
                station: station, feed: feeds[station.systemID], attribution: nil,
                now: response.generatedAt)
            let capacity = try #require(detail.facts.first { $0.label == "容量" })
            #expect(capacity.value == StationDetail.unknownValue, "\(station.id) に容量の数が出ている")
            #expect(capacity.note == StationDetail.dynamicCapacityNote)
        }
    }

    @Test("**矛盾する数を画面に出さない**（容量より台数が多い行が実応答にある）")
    func theRealResponseNoLongerContradictsItself() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        let feeds = response.feedIndex()
        // 実応答には「capacity < bikes」の行が実在する（ドコモの 10.8%）。
        // **表示に回った時点で、その矛盾が残っていない**ことを見る
        let contradicting = response.stations.filter { station in
            guard let capacity = station.capacity, let bikes = station.bikes else { return false }
            return capacity < bikes
        }
        #expect(!contradicting.isEmpty, "矛盾する行がフィクスチャに無い（別の応答で測り直す）")
        for station in contradicting {
            let detail = StationDetail(
                station: station, feed: feeds[station.systemID], attribution: nil,
                now: response.generatedAt)
            let capacity = try #require(detail.facts.first { $0.label == "容量" })
            #expect(
                capacity.value == StationDetail.unknownValue,
                "\(station.id) は容量 \(station.capacity ?? -1)・台数 \(station.bikes ?? -1) で矛盾している")
        }
    }

    // ── 予測（W4 の PR A・PR B）────────────────────────────
    @Test("**到着を頼まなければ予測は入らない**（応答の形が W2 のまま）")
    func withoutAnArrivalThereIsNoForecast() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        #expect(response.forecastInMinutes == nil)
        #expect(response.forecastArrival == nil)
        #expect(response.stations.allSatisfy { $0.forecast == nil })
    }

    @Test("**`at` を送った応答から予測を読める**")
    func decodesTheForecast() throws {
        let response = try Self.decode(StationsResponse.self, "stations_arrival")
        let minutes = try #require(response.forecastInMinutes)
        #expect(minutes >= Arrival.minMinutes && minutes <= Arrival.maxMinutes)
        #expect(minutes % Arrival.stepMinutes == 0)
        // このフィクスチャは全ポートに予測が付いている（本番の実測で 99% 台）
        #expect(response.stations.allSatisfy { $0.forecast != nil })
        let forecast = try #require(response.stations.first?.forecast)
        #expect((0...1).contains(forecast.rentProbability))
        #expect((0...1).contains(forecast.returnProbability))
        #expect((0...3).contains(forecast.confidence))
        #expect(!forecast.modelVersion.isEmpty)
    }

    @Test("**確率が指す時刻は `generated_at ＋ forecast_in_min`**（§12 の 114）")
    func theForecastPointsAtAnAbsoluteTime() throws {
        let response = try Self.decode(StationsResponse.self, "stations_arrival")
        let arrival = try #require(response.forecastArrival)
        let minutes = try #require(response.forecastInMinutes)
        #expect(arrival == response.generatedAt.addingTimeInterval(TimeInterval(minutes * 60)))
        // 予測の基準になった観測は、応答よりも前
        for station in response.stations {
            guard let forecast = station.forecast else { continue }
            #expect(forecast.baseObservedAt <= response.generatedAt)
            #expect(arrival > forecast.baseObservedAt)
        }
    }

    @Test("**到着時刻を変えると確率が変わる**（本番の 2 応答で確かめる）")
    func theProbabilityDependsOnTheArrival() throws {
        let near = try Self.decode(StationsResponse.self, "stations_arrival")
        let far = try Self.decode(StationsResponse.self, "stations_arrival_late")
        #expect(near.forecastInMinutes != far.forecastInMinutes)
        let farByID = Dictionary(far.stations.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        let compared = near.stations.compactMap { station -> (Double, Double)? in
            guard let here = station.forecast?.rentProbability,
                let there = farByID[station.id]?.forecast?.rentProbability
            else { return nil }
            return (here, there)
        }
        #expect(compared.count > 10, "比べられるポートが少なすぎる")
        // **同じポート・同じ指標で、到着が違えば値も違う。** 同じなら補間か起点が壊れている
        #expect(compared.allSatisfy { $0.0 != $0.1 })
    }

    @Test("**実応答から予測の表示を作れる**（全ポートで出せる）")
    func buildsTheForecastDisplayFromTheRealResponse() throws {
        let response = try Self.decode(StationsResponse.self, "stations_arrival")
        let feeds = response.feedIndex()
        let arrival = try #require(response.forecastArrival)
        for station in response.stations {
            for intent in RideIntent.allCases {
                let state = ForecastState.make(
                    station: station, feed: feeds[station.systemID], intent: intent,
                    arrival: arrival)
                let display = try #require(state.display, "\(station.id) の予測が出せない")
                #expect(display.percent >= 0 && display.percent <= ForecastDisplay.maxPercent)
                #expect(display.headline.contains("可能性"))
                #expect(display.arrival.hasSuffix("到着"))
                #expect(display.basis.hasSuffix("時点の観測にもとづく予測"))
            }
        }
    }

    @Test("**予測の無いポートでも行は返り、台数は出る**（本番の実データ）")
    func stationsWithoutAForecastKeepTheirCounts() throws {
        // このフィクスチャの 2 件は貸出も返却も止まっているポート（W3 プラン §12 の 109）。
        // サーバーは行を返し、予測だけを null にする（W4-02）
        let response = try Self.decode(StationsResponse.self, "stations_partial_forecast")
        #expect(response.forecastInMinutes != nil)
        let missing = response.stations.filter { $0.forecast == nil }
        let present = response.stations.filter { $0.forecast != nil }
        #expect(!missing.isEmpty, "予測の無いポートがフィクスチャに無い")
        #expect(!present.isEmpty, "予測のあるポートがフィクスチャに無い")

        let feeds = response.feedIndex()
        let arrival = try #require(response.forecastArrival)
        for station in missing {
            let detail = StationDetail(
                station: station, feed: feeds[station.systemID], attribution: nil,
                arrival: arrival, now: response.generatedAt)
            // **「いまは予測できません」と出す。理由は言わない**
            #expect(detail.forecast == .unavailable(ForecastState.unavailableText))
            // **台数は出る**（現在値は別の情報）
            #expect(detail.facts.first { $0.label == "座標" } != nil)
            #expect(detail.availability.count == 2)
        }
    }

    @Test("**予測を出しても台数の表示は変わらない**（混ぜていない）")
    func theForecastDoesNotDisturbTheCounts() throws {
        let plain = try Self.decode(StationsResponse.self, "stations")
        let withForecast = try Self.decode(StationsResponse.self, "stations_arrival")
        let feeds = withForecast.feedIndex()
        let arrival = withForecast.forecastArrival
        let byID = Dictionary(plain.stations.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        for station in withForecast.stations {
            guard let before = byID[station.id] else { continue }
            let detail = StationDetail(
                station: station, feed: feeds[station.systemID], attribution: nil,
                arrival: arrival, now: withForecast.generatedAt)
            let plainDetail = StationDetail(
                station: before, feed: feeds[before.systemID], attribution: nil,
                now: plain.generatedAt)
            // 台数は観測が進めば変わるので、**「出す／出さない」の判断が同じ**ことを見る
            #expect(
                (detail.availability[0].value == StationDetail.unknownValue)
                    == (plainDetail.availability[0].value == StationDetail.unknownValue))
        }
    }

    @Test("Problem Details をデコードできる")
    func decodesProblems() throws {
        let missing = try Self.decode(Problem.self, "problem_bbox_missing")
        #expect(missing.code == "bbox_missing")
        #expect(missing.status == 400)
        #expect(!missing.needsNarrowerBbox)

        let tooMany = try Self.decode(Problem.self, "problem_too_many")
        #expect(tooMany.code == "too_many_stations")
        #expect(tooMany.needsNarrowerBbox)
        // 「どれだけ狭めればよいか」が本文に入っている
        #expect(tooMany.detail.contains("6649"))
    }
    // ────────────────────────────────────────────────────────────
    // ポート詳細（W5 の PR F。2026-09-13 に本番から取った）
    // ────────────────────────────────────────────────────────────
    @Test("/v1/stations/{system}/{station_id} をデコードできる")
    func decodesStationDetail() throws {
        let response = try Self.decode(StationDetailResponse.self, "station_detail")
        #expect(response.apiVersion == "v1")
        // **そのポートのシステムだけ。** 地図と違い 1 系統しか関係しない
        #expect(response.feeds.count == 1)
        #expect(response.attribution.count == 1)
        #expect(response.station.systemID == "docomo-cycle")
    }

    @Test("**`capacity_est` はドコモにも付く**（`capacity` は nil のまま。W5-14）")
    func capacityEstimateComesEvenWhenCapacityIsNil() throws {
        let response = try Self.decode(StationDetailResponse.self, "station_detail")
        // ドコモの公開する `capacity` は動的値なのでビューが nil にしている（0035）
        #expect(response.station.capacity == nil)
        // **実測からの大きさは出る。** これが W4-09 の宿題の答え
        #expect(response.station.capacityEstimate == 35)
        #expect(response.station.capacityDays == 7)
    }

    @Test("**曲線は補間されていない生の 10 点**（W5-13）")
    func theCurveIsRaw() throws {
        let curve = try #require(
            Self.decode(StationDetailResponse.self, "station_detail").forecastCurve)
        #expect(curve.horizonsMinutes == [5, 10, 15, 20, 30, 45, 60, 90, 120, 180])
        #expect(curve.rentProbabilities.count == curve.horizonsMinutes.count)
        #expect(curve.returnProbabilities.count == curve.horizonsMinutes.count)
        #expect(curve.modelVersion.isEmpty == false)
    }

    @Test("**水平は `generatedAt` からの分数**（端末が受け取った時刻からではない）")
    func theCurveIsAnchoredAtGeneratedAt() throws {
        let curve = try #require(
            Self.decode(StationDetailResponse.self, "station_detail").forecastCurve)
        let first = try #require(curve.arrival(at: 0))
        #expect(first == curve.generatedAt.addingTimeInterval(5 * 60))
        #expect(curve.arrival(at: curve.horizonsMinutes.count) == nil)
    }

    @Test("**直近 24 時間は 24 件を超えない**（進行中の時間は入らない）")
    func recentFitsInADay() throws {
        let response = try Self.decode(StationDetailResponse.self, "station_detail")
        #expect(response.recent.count <= 24)
        #expect(response.recent.isEmpty == false)
        // 古い順。グラフは左から右に時間が流れる
        #expect(response.recent == response.recent.sorted { $0.hourStart < $1.hourStart })
        #expect(response.recent.allSatisfy { $0.count > 0 })
    }

    @Test("**予測の無いポートでも 200 でデコードできる**（欄ごと nil。契約 2）")
    func decodesAStationWithoutAForecast() throws {
        let response = try Self.decode(StationDetailResponse.self, "station_detail_no_forecast")
        #expect(response.forecastCurve == nil)
        // 予測が無くても現在値と実績は返る
        #expect(response.station.capacityEstimate != nil)
        #expect(response.recent.isEmpty == false)
    }

    @Test("**地図の応答にも `capacity_est` が載る**（0045）")
    func stationsCarryTheCapacityEstimate() throws {
        let response = try Self.decode(StationsResponse.self, "stations_capacity")
        #expect(response.count > 0)
        #expect(response.stations.allSatisfy { $0.capacityEstimate != nil })
    }

    @Test("**古い応答も読める**（`capacity_est` が無い 2026-09-08 の実応答）")
    func oldResponsesStillDecode() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        #expect(response.stations.allSatisfy { $0.capacityEstimate == nil })
    }
}
