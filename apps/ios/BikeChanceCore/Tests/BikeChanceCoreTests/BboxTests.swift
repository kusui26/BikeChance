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

    @Test("上限を超えた矩形は中心を保って縮める")
    func clampsAroundTheCentre() {
        let wide = Bbox(west: 139.0, south: 35.0, east: 140.0, north: 36.0)
        let clamped = wide.clampedToRequestable()
        #expect(abs(clamped.latSpan - Bbox.requestMaxSpanDegrees) < 1e-9)
        #expect(abs(clamped.lonSpan - Bbox.requestMaxSpanDegrees) < 1e-9)
        // 中心は動かさない
        #expect(abs((clamped.north + clamped.south) / 2 - 35.5) < 1e-9)
        #expect(abs((clamped.east + clamped.west) / 2 - 139.5) < 1e-9)
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

    @Test("境界を含めて判定する")
    func containsIncludesTheEdges() {
        #expect(tokyo.contains(latitude: 35.68, longitude: 139.77))
        #expect(tokyo.contains(latitude: 35.67, longitude: 139.76))
        #expect(!tokyo.contains(latitude: 35.66, longitude: 139.77))
    }
}
