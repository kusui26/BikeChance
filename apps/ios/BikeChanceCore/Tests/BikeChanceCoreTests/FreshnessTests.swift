import Foundation
import Testing

@testable import BikeChanceCore

/// 鮮度と「いま借りられるか」の判定。
///
/// **ODPT ガイドライン 2.1 と CLAUDE.md §2 の 7・8** に直結する。実測値には観測時刻を
/// 添え、ttl を超えた値を「現在値」として出さない。ここが崩れると表示義務に反する。
@Suite("鮮度")
struct FreshnessTests {
    let now = Date(timeIntervalSince1970: 1_788_800_000)

    func feed(staleAfter: Int = 960) -> FeedStatus {
        FeedStatus(
            systemID: "hellocycling",
            displayName: "HELLO CYCLING",
            dataUpdatedAt: now,
            expectedCadenceSeconds: 300,
            staleAfterSeconds: staleAfter,
            isStale: false,
            capacityIsDynamic: false
        )
    }

    func station(
        observedAgo: TimeInterval?,
        isPresent: Bool = true,
        bikes: Int? = 3,
        docks: Int? = 4,
        isRenting: Bool? = true,
        isReturning: Bool? = true
    ) -> StationCurrent {
        StationCurrent(
            systemID: "hellocycling",
            stationID: "a",
            name: "テスト",
            latitude: 35.68,
            longitude: 139.77,
            capacity: 10,
            bikes: bikes,
            docks: docks,
            isInstalled: true,
            isRenting: isRenting,
            isReturning: isReturning,
            isPresent: isPresent,
            observedAt: observedAgo.map { now.addingTimeInterval(-$0) },
            lastChangedAt: now,
            forecast: nil
        )
    }

    @Test("閾値の内側なら新しい")
    func freshInsideTheThreshold() {
        let result = station(observedAgo: 120).freshness(feed: feed(), now: now)
        #expect(result == .fresh(age: 120))
        #expect(result.isPresentable)
        #expect(result.label == "2 分前の観測")
    }

    @Test("閾値を超えたら古い（値は返すが「いま」とは言わない）")
    func staleBeyondTheThreshold() {
        let result = station(observedAgo: 1200).freshness(feed: feed(), now: now)
        #expect(result == .stale(age: 1200))
        #expect(!result.isPresentable)
        #expect(result.label.contains("滞って"))
    }

    @Test("閾値ちょうどはまだ新しい")
    func theThresholdItselfIsFresh() {
        #expect(station(observedAgo: 960).freshness(feed: feed(), now: now).isPresentable)
        #expect(!station(observedAgo: 961).freshness(feed: feed(), now: now).isPresentable)
    }

    @Test("最新のフィードに現れなかったポートは「不明」")
    func absentStationsAreUnknown() {
        let result = station(observedAgo: nil, isPresent: false).freshness(feed: feed(), now: now)
        #expect(result == .unknown)
        #expect(!result.isPresentable)
        #expect(result.label == "観測時刻が不明")
    }

    @Test("観測時刻が無ければ「不明」（present でも）")
    func missingTimestampIsUnknown() {
        #expect(station(observedAgo: nil).freshness(feed: feed(), now: now) == .unknown)
    }

    @Test("時計のずれで未来になっても 0 分として扱う")
    func clockSkewDoesNotProduceNegativeAges() {
        let result = station(observedAgo: -30).freshness(feed: feed(), now: now)
        #expect(result.label == "0 分前の観測")
    }

    @Test("システムごとに閾値が違う（ドコモは 303 秒）")
    func thresholdVariesBySystem() {
        let docomo = feed(staleAfter: 303)
        #expect(station(observedAgo: 300).freshness(feed: docomo, now: now).isPresentable)
        #expect(!station(observedAgo: 400).freshness(feed: docomo, now: now).isPresentable)
    }
}

@Suite("いま借りられるか")
struct AvailabilityTests {
    let helper = FreshnessTests()

    @Test("台数があって貸出可なら借りられる")
    func canRent() {
        #expect(helper.station(observedAgo: 60).canRentNow == true)
    }

    @Test("0 台なら借りられない")
    func cannotRentWhenEmpty() {
        #expect(helper.station(observedAgo: 60, bikes: 0).canRentNow == false)
    }

    @Test("**貸出停止なら台数があっても借りられない**")
    func cannotRentWhenSuspended() {
        #expect(helper.station(observedAgo: 60, isRenting: false).canRentNow == false)
    }

    @Test("未観測は「分からない」（false ではない）")
    func unknownIsNotFalse() {
        #expect(helper.station(observedAgo: 60, bikes: nil).canRentNow == nil)
        #expect(helper.station(observedAgo: 60, isRenting: nil).canRentNow == nil)
    }

    @Test("返却も同じ規則")
    func returningFollowsTheSameRule() {
        #expect(helper.station(observedAgo: 60).canReturnNow == true)
        #expect(helper.station(observedAgo: 60, docks: 0).canReturnNow == false)
        #expect(helper.station(observedAgo: 60, isReturning: false).canReturnNow == false)
        #expect(helper.station(observedAgo: 60, docks: nil).canReturnNow == nil)
    }
}
