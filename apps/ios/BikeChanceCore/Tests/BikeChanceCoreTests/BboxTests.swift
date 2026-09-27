import Foundation
import Testing

@testable import BikeChanceCore

/// `Bbox` の規則は **`packages/shared/src/bbox.ts` と同じでなければならない。**
/// 食い違うと CDN のキャッシュが効かず、サーバーが返す実効矩形とも噛み合わない。
/// TypeScript 側のテストと**同じ入力・同じ期待値**を並べてある。
@Suite("Bbox")
struct BboxTests {
    /// 東京駅まわりの小さな矩形（TS 側のテストと同じ値）。
    let tokyo = Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69)

    @Test("外側へ丸める（要求した範囲は必ず入る）")
    func quantizesOutward() {
        let quantized = Bbox(west: 139.7654, south: 35.6789, east: 139.7712, north: 35.6821)
            .quantized()
        #expect(quantized == Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69))
    }

    @Test("格子ちょうどの矩形は変わらない")
    func quantizingAlignedBboxIsIdentity() {
        #expect(tokyo.quantized() == tokyo)
    }

    @Test("丸めた結果は必ず元の範囲を含む")
    func quantizedContainsOriginal() {
        let samples = [
            Bbox(west: 139.7654321, south: 35.6789012, east: 139.7654322, north: 35.6789013),
            Bbox(west: -0.0001, south: -0.0001, east: 0.0001, north: 0.0001),
            Bbox(west: 139.99999, south: 35.99999, east: 140.00001, north: 36.00001),
        ]
        for bbox in samples {
            let quantized = bbox.quantized()
            #expect(quantized.west <= bbox.west)
            #expect(quantized.south <= bbox.south)
            #expect(quantized.east >= bbox.east)
            #expect(quantized.north >= bbox.north)
        }
    }

    @Test("近い 2 つの要求が同じ矩形に写る（CDN が効く）")
    func nearbyRequestsShareOneURL() {
        let a = Bbox(west: 139.7601, south: 35.6701, east: 139.7799, north: 35.6899).quantized()
        let b = Bbox(west: 139.7609, south: 35.6709, east: 139.7791, north: 35.6891).quantized()
        #expect(a.queryValue == b.queryValue)
    }

    @Test("浮動小数の端数を残さない（同じ入力が同じ URL になる）")
    func queryValueHasNoFloatingDust() {
        let value = Bbox(west: 139.767, south: 35.681, east: 139.768, north: 35.682)
            .quantized().queryValue
        #expect(value == "139.76,35.68,139.77,35.69")
        #expect(!value.contains("0000"))
    }

    @Test("刻みは 1 km 程度（位置を細かく送らない）")
    func quantumIsAboutOneKilometre() {
        #expect(Bbox.quantumDegrees == 0.01)
    }

    @Test("上限を超えた矩形は中心を保って、上限より 1 格子狭く縮める")
    func clampsAroundTheCentre() {
        let wide = Bbox(west: 139.0, south: 35.0, east: 140.0, north: 36.0)
        let clamped = wide.clampedToRequestable()
        let inner = Bbox.requestMaxSpanDegrees - Bbox.quantumDegrees
        #expect(abs(clamped.latSpan - inner) < 1e-9)
        #expect(abs(clamped.lonSpan - inner) < 1e-9)
        #expect(clamped.isRequestable)
        // 中心は動かさない
        #expect(abs((clamped.north + clamped.south) / 2 - 35.5) < 1e-9)
        #expect(abs((clamped.east + clamped.west) / 2 - 139.5) < 1e-9)
    }

    @Test("**縮めた矩形は、どこに置いても丸めた後に上限に収まる**（外側へ丸めても 1 格子しか広がらない）")
    func clampedBboxesStayWithinTheLimitAfterRounding() {
        let centres = (0..<200).map { 139.0 + Double($0) * 0.0037 }
        for centre in centres {
            let wide = Bbox(west: centre - 0.3, south: 35.2, east: centre + 0.3, north: 35.9)
            #expect(wide.clampedToRequestable().roundedLongSideQuanta <= Bbox.requestMaxSpanQuanta)
        }
    }

    @Test("上限の内側なら縮めない")
    func doesNotShrinkSmallBboxes() {
        #expect(tokyo.clampedToRequestable() == tokyo)
    }

    @Test("要求してよい上限はサーバーの上限より厳しい")
    func clientLimitIsStricterThanTheServer() {
        // 実測で 0.4 度四方の東京は 6,649 ポートあり、件数の上限 1,000 件を超える
        #expect(Bbox.requestMaxSpanDegrees < Bbox.serverMaxSpanDegrees)
    }

    @Test("要求できるかの判定")
    func requestability() {
        #expect(tokyo.isRequestable)
        #expect(!Bbox(west: 139.0, south: 35.0, east: 140.0, north: 36.0).isRequestable)
        #expect(!Bbox(west: 139.76, south: 35.67, east: 139.76, north: 35.69).isRequestable)
    }

    // MARK: - 丸めた後の上限（W6 の契約 41、所見 194・205）

    @Test("**上限はサーバーがポートで返す上限と同じ 0.08 度**（両側の検査が同じ値を書く）")
    func theLimitIsTheServersCellThreshold() {
        // サーバー側は `packages/shared/src/cells.test.ts` が `CELL_STATION_MAX_SPAN_DEG` を 0.08 と書く
        #expect(Bbox.requestMaxSpanDegrees == 0.08)
        #expect(Bbox.requestMaxSpanQuanta == 8)
    }

    /// 西端を格子に揃えた、東西に `span` 度の矩形（南北は 0.03 度）。
    func eastward(_ span: Double) -> Bbox {
        Bbox(west: 139.76, south: 35.67, east: 139.76 + span, north: 35.70)
    }

    @Test(
        "丸めの境：要求 0.079・0.080 度は 8 格子、0.081 度は 9 格子",
        arguments: [(0.079, 8, true), (0.080, 8, true), (0.081, 9, false)])
    func roundingEdges(span: Double, quanta: Int, requestable: Bool) {
        #expect(eastward(span).roundedLongSideQuanta == quanta)
        #expect(eastward(span).isRequestable == requestable)
    }

    @Test("**丸めで 1 格子広がる矩形は要求しない**（0.079 度でも、格子をまたげば 9 格子）")
    func roundingCanPushOverTheLimit() {
        let shifted = Bbox(west: 139.765, south: 35.67, east: 139.844, north: 35.70)
        #expect(shifted.roundedLongSideQuanta == 9)
        #expect(!shifted.isRequestable)
    }

    @Test("**経度の引き算の端数で境目がずれない**（139.84 − 139.76 ＝ 0.0800000000000125）")
    func floatingDustDoesNotMoveTheEdge() {
        let eight = Bbox(west: 139.76, south: 35.67, east: 139.84, north: 35.70)
        #expect(eight.lonSpan > Bbox.requestMaxSpanDegrees)
        #expect(eight.roundedLongSideQuanta == 8)
        #expect(eight.isRequestable)
    }

    @Test("**経度 139 度台のどこでも**、8 格子は要求し・9 格子は要求しない")
    func everyWestEdgeAgreesWithTheServer() {
        for index in 0..<100 {
            let west = 139.0 + Double(index) * Bbox.quantumDegrees
            let eight = Bbox(west: west, south: 35.67, east: west + 0.08, north: 35.70)
            let nine = Bbox(west: west, south: 35.67, east: west + 0.09, north: 35.70)
            #expect(eight.isRequestable, "west = \(west)")
            #expect(!nine.isRequestable, "west = \(west)")
        }
    }

    @Test("境界を含めて判定する")
    func containsIncludesTheEdges() {
        #expect(tokyo.contains(latitude: 35.68, longitude: 139.77))
        #expect(tokyo.contains(latitude: 35.67, longitude: 139.76))
        #expect(!tokyo.contains(latitude: 35.66, longitude: 139.77))
    }
}
