import Foundation
import Testing

@testable import BikeChanceCore

/// お気に入りのシートのクレジット（`FavoritesCredits.swift`、W7 プランの W7-08、W6 プランの所見 219）。
///
/// **主題は「行に出しているデータの出典を、応答の言葉のまま出す」こと。** お気に入りは
/// シートで開くので、地図の下の 1 行のクレジットが隠れる。
@Suite("お気に入りのクレジット（W7-08）")
struct FavoritesCreditsTests {
    /// 本番の前置き（`meta_sources.json` と同じ）。
    static let preface = "このアプリは、以下の著作物を改変して利用しています。"

    /// 本番の `/v1/meta`（PR J の後）の、系統ごとの `attribution`（JSON のまま）。
    static func attributionJSON(_ system: String) throws -> [String: Any] {
        let meta = try #require(
            try JSONSerialization.jsonObject(with: ContractTests.fixture("meta_sources"))
                as? [String: Any])
        let all = try #require(meta["attribution"] as? [[String: Any]])
        return try #require(all.first { $0["system_id"] as? String == system })
    }

    /// 本番の形（`station_detail.json`）を元に、系統と前置きだけを差し替えた応答の本文。
    /// **前置きに nil を渡すと欄ごと消える**（PR J より前のサーバーの形）。
    static func detailBody(system: String = "docomo-cycle", preface: String? = preface) throws
        -> Data
    {
        try detailBody(attribution: attributionJSON(system), preface: preface)
    }

    static func detailBody(attribution: [String: Any], preface: String?) throws -> Data {
        var object = try #require(
            try JSONSerialization.jsonObject(with: ContractTests.fixture("station_detail"))
                as? [String: Any])
        object["attribution_preface"] = preface
        object["attribution"] = [attribution]
        return try JSONSerialization.data(withJSONObject: object)
    }

    /// 架空の系統の応答。**並びを確かめるため**——本番の 2 系統だけでは、辞書の順でも
    /// 半分の確率でたまたま合ってしまう（変異で確かめた）。
    static func fakeDetail(_ system: String) throws -> StationDetailResponse {
        var attribution = try attributionJSON("docomo-cycle")
        attribution["system_id"] = system
        attribution["provider"] = "提供元 \(system)"
        return try V1Client.makeDecoder().decode(
            StationDetailResponse.self,
            from: detailBody(attribution: attribution, preface: preface))
    }

    static func detail(system: String = "docomo-cycle", preface: String? = preface) throws
        -> StationDetailResponse
    {
        try V1Client.makeDecoder().decode(
            StationDetailResponse.self, from: detailBody(system: system, preface: preface))
    }

    /// 期待する 1 行。**文言は端末で組まない**ので、応答から読んだものと比べる。
    static func credit(_ system: String) throws -> String {
        let meta = try ContractTests.decode(MetaResponse.self, "meta_sources")
        return try #require(meta.attribution.first { $0.systemID == system }).credit
    }

    @Test("1 系統なら、前置きとクレジットの 1 行")
    func oneSystem() throws {
        let credits = try #require(FavoritesCredits.collect(from: [Self.detail()]))
        #expect(credits.preface == Self.preface)
        #expect(credits.credits == [try Self.credit("docomo-cycle")])
    }

    @Test("**2 系統なら 2 行、system_id の順**（お気に入りの並びに左右されない）")
    func twoSystemsInStableOrder() throws {
        let hello = try Self.detail(system: "hellocycling")
        let docomo = try Self.detail(system: "docomo-cycle")
        let expected = [try Self.credit("docomo-cycle"), try Self.credit("hellocycling")]

        #expect(FavoritesCredits.collect(from: [hello, docomo])?.credits == expected)
        #expect(FavoritesCredits.collect(from: [docomo, hello])?.credits == expected)
    }

    @Test("**並びは system_id の順**（系統が増えても。辞書の順に頼らない）")
    func ordersBySystemID() throws {
        let systems = (0..<8).map { "system-\($0)" }
        let details = try systems.reversed().map(Self.fakeDetail)
        let creditOf = Dictionary(
            uniqueKeysWithValues: details.flatMap(\.attribution).map { ($0.systemID, $0.credit) })
        let credits = try #require(FavoritesCredits.collect(from: details))
        #expect(credits.credits == systems.compactMap { creditOf[$0] })
    }

    @Test("同じ系統のポートが並んでも、クレジットは 1 行")
    func oneLinePerSystem() throws {
        let details = try (0..<3).map { _ in try Self.detail() }
        #expect(FavoritesCredits.collect(from: details)?.credits.count == 1)
    }

    @Test("**応答が 1 つも無ければ出さない**（出しているデータが無い）")
    func nothingToCredit() {
        #expect(FavoritesCredits.collect(from: []) == nil)
    }

    @Test("前置きを返さない古い応答だけなら、前置きは出さず、クレジットは出す")
    func oldServerHasNoPreface() throws {
        let credits = try #require(
            FavoritesCredits.collect(from: [Self.detail(preface: nil)]))
        #expect(credits.preface == nil)
        #expect(credits.credits.count == 1)
    }

    @Test("古い応答と新しい応答が混ざっても、前置きは出す（取っておいた古い行があるとき）")
    func prefaceFromAnyResponse() throws {
        let old = try Self.detail(system: "hellocycling", preface: nil)
        let new = try Self.detail(system: "docomo-cycle")
        #expect(FavoritesCredits.collect(from: [old, new])?.preface == Self.preface)
    }
}
