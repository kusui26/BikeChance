import Foundation
import Testing

@testable import BikeChanceCore

/// 到着時刻の決め方（`Arrival` / `ArrivalChoice`）。
///
/// **この 1 ファイルの主題は「サーバーが受け取れる形にすること」。** 守るのは 3 つ。
///   * **5 分の格子に乗せる**（同じ時間帯の利用者が同じ URL を叩き、CDN が効く）
///   * **サーバーの範囲（5〜180 分）に必ず収める**（外れると 400 で予測が消える）
///   * **絶対の時刻で持つ**（相対だと、CDN から返った応答が別の到着を指す。§12 の 114）
@Suite("到着時刻")
struct ArrivalTests {
    let jst = TimeZone(identifier: "Asia/Tokyo")!
    let ja = Locale(identifier: "ja_JP")

    /// 2026-09-09 14:03:20 JST。**格子から外れた時刻**を基準にする。
    let now = Date(timeIntervalSince1970: 1_788_930_200)

    func minutes(from now: Date, to date: Date) -> Double {
        date.timeIntervalSince(now) / 60
    }

    // ── 格子 ──────────────────────────────────────────────────
    @Test("**5 分の格子に乗る**（同じ時間帯の要求が同じ URL になる）")
    func snapsToTheGrid() {
        for choice in ArrivalChoice.choices {
            guard let at = choice.at(now: now) else { continue }
            #expect(
                at.timeIntervalSince1970.truncatingRemainder(dividingBy: 300) == 0,
                "\(choice.pickerLabel) が格子から外れた")
        }
    }

    @Test("いちばん近い格子に丸める（切り捨てではない）")
    func roundsToTheNearestGridPoint() throws {
        // 14:03:20 の 30 分後は 14:33:20。近いのは 14:35
        let at = try #require(ArrivalChoice.later(minutes: 30).at(now: now))
        #expect(Arrival.clockText(at, locale: ja, timeZone: jst) == "14:35")
    }

    @Test("**選んだ分数と厳密には一致しない**（格子に乗せるため）")
    func theGridShiftsTheArrivalSlightly() throws {
        let at = try #require(ArrivalChoice.later(minutes: 30).at(now: now))
        let actual = minutes(from: now, to: at)
        #expect(actual != 30)
        // ずれは刻みの半分まで。だから画面には「30 分後」ではなく時刻を出す
        #expect(abs(actual - 30) <= Double(Arrival.stepMinutes) / 2)
    }

    // ── 範囲 ──────────────────────────────────────────────────
    @Test("**どの選択肢もサーバーの範囲に収まる**（400 を貰いに行かない）")
    func everyChoiceStaysInsideTheServerRange() {
        // 1 秒ずつずらして 5 分ぶん試す。格子との位相をすべて通す
        for offset in stride(from: 0.0, to: 300.0, by: 1.0) {
            let base = now.addingTimeInterval(offset)
            for choice in ArrivalChoice.choices {
                guard let at = choice.at(now: base) else { continue }
                let ahead = minutes(from: base, to: at)
                #expect(
                    ahead >= Double(Arrival.minMinutes) && ahead <= Double(Arrival.maxMinutes),
                    "\(choice.pickerLabel) が範囲の外（\(ahead) 分）")
            }
        }
    }

    @Test("**いちばん先の選択肢でも 180 分を超えない**（丸めで飛び出さない）")
    func theFarthestChoiceNeverExceedsTheLimit() throws {
        // 14:03:20 の 180 分後は 17:03:20。近い格子は 17:05 で、これは 181.7 分先になる。
        // 1 段戻して 17:00（176.7 分先）にする
        let at = try #require(ArrivalChoice.later(minutes: 180).at(now: now))
        #expect(Arrival.clockText(at, locale: ja, timeZone: jst) == "17:00")
        #expect(minutes(from: now, to: at) <= Double(Arrival.maxMinutes))
    }

    @Test("**手前に寄りすぎたら 1 段先へ**（5 分より手前は受け取ってもらえない）")
    func clampsUpWhenTooClose() {
        // 14:02:00 の 5 分後は 14:07:00。近い格子は 14:05 で、これは 3 分先しかない
        let base = Date(timeIntervalSince1970: 1_788_930_120)
        let at = Arrival.gridded(base.addingTimeInterval(5 * 60), now: base)
        #expect(minutes(from: base, to: at) >= Double(Arrival.minMinutes))
        #expect(Arrival.clockText(at, locale: ja, timeZone: jst) == "14:10")
    }

    @Test("刻みと範囲はサーバーと同じ値")
    func theConstantsMatchTheServer() {
        // packages/shared/src/forecast.ts の ARRIVAL_STEP_MIN / ARRIVAL_MIN_MIN / ARRIVAL_MAX_MIN
        #expect(Arrival.stepMinutes == 5)
        #expect(Arrival.minMinutes == 5)
        #expect(Arrival.maxMinutes == 180)
    }

    // ── 「いま」 ──────────────────────────────────────────────
    @Test("**「いま」は予測を頼まない**（現在値は予測ではない）")
    func nowDoesNotAskForAForecast() {
        #expect(ArrivalChoice.now.at(now: now) == nil)
        #expect(ArrivalChoice.now.wantsForecast == false)
        #expect(ArrivalChoice.later(minutes: 30).wantsForecast)
    }

    // ── 表示 ──────────────────────────────────────────────────
    @Test("**選択中の表示は時刻**（「約 30 分後」と言わない。§12 の 114）")
    func theLabelIsAClockTime() {
        #expect(
            ArrivalChoice.later(minutes: 30).label(now: now, locale: ja, timeZone: jst)
                == "14:35 到着")
        #expect(ArrivalChoice.now.label(now: now, locale: ja, timeZone: jst) == "いまの空き")
    }

    @Test("ピッカーの名前は分と時間を使い分ける")
    func thePickerLabelsReadNaturally() {
        #expect(ArrivalChoice.now.pickerLabel == "いま")
        #expect(ArrivalChoice.later(minutes: 15).pickerLabel == "15 分後")
        #expect(ArrivalChoice.later(minutes: 60).pickerLabel == "1 時間後")
        #expect(ArrivalChoice.later(minutes: 90).pickerLabel == "1 時間 30 分後")
        #expect(ArrivalChoice.later(minutes: 180).pickerLabel == "3 時間後")
    }

    @Test("選択肢は「いま」から始まり、重複しない")
    func theChoicesAreOrderedAndUnique() {
        #expect(ArrivalChoice.choices.first == .now)
        #expect(Set(ArrivalChoice.choices.map(\.id)).count == ArrivalChoice.choices.count)
        let later = ArrivalChoice.choices.compactMap { choice -> Int? in
            guard case .later(let minutes) = choice else { return nil }
            return minutes
        }
        #expect(later == later.sorted())
        #expect(later.allSatisfy { $0 >= Arrival.minMinutes && $0 <= Arrival.maxMinutes })
    }
}
