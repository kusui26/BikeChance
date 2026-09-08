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
        // W2 時点でモデルは無い
        #expect(meta.modelVersion == nil)
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

    @Test("ドコモのポートには動的容量の注記が付く")
    func docomoStationsExplainTheirCapacity() throws {
        let response = try Self.decode(StationsResponse.self, "stations")
        let feeds = response.feedIndex()
        let docomo = try #require(
            response.stations.first {
                feeds[$0.systemID]?.capacityIsDynamic == true && $0.capacity != nil
            },
            "動的容量のポートがフィクスチャに無い"
        )
        let detail = StationDetail(
            station: docomo, feed: feeds[docomo.systemID], attribution: nil,
            now: response.generatedAt)
        let capacity = try #require(detail.facts.first { $0.label == "容量" })
        #expect(capacity.note?.contains("固定の台数ではなく") == true)
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
}
