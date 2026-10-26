import BikeChanceCore
import Charts
import SwiftUI

/// 到着時刻ごとの予測の点（iOS-3）。**判断は `ForecastTimeline` に置いてある。** ここは描くだけ。
///
/// **点だけを描き、線でつながない**（W7 プラン §10.3）。水平の境目では森と B3 が切り替わって
/// 段が立つので、線にすると段が「動き」に見える。**`LineMark` を足さない。**
///
/// **縦軸は 0〜100% に固定する。** 自動の目盛りにすると、5% の差が画面いっぱいの崖に見える。
/// 目盛りは帯の境（60・85）に置き、点の色は帯の色にする（開発プラン §9.3 の表）。
struct ForecastTimelineChart: View {
    let timeline: ForecastTimeline

    /// 点の直径（pt）。**指で数えられる大きさ**にする（最初の 4 点は 5 分おきで寄り合う）。
    private static let pointDiameter: CGFloat = 9
    private static let chartHeight: CGFloat = 180
    /// 端の点が欠けないよう、描く範囲の左右に空ける幅（pt）。
    private static let edgePadding: CGFloat = 12

    var body: some View {
        Chart {
            if let arrival = timeline.selectedArrival {
                selectedArrivalRule(arrival)
            }
            ForEach(timeline.points) { point in
                PointMark(
                    x: .value("到着", point.arrival),
                    y: .value(timeline.title, point.display.percent)
                )
                .symbol { symbol(point.display.band) }
                .accessibilityLabel(point.accessibilityLabel)
            }
        }
        .chartYScale(domain: 0...100)
        .chartYAxis {
            AxisMarks(position: .leading, values: ForecastTimeline.axisPercents) { value in
                AxisGridLine()
                AxisValueLabel {
                    if let percent = value.as(Int.self) { Text("\(percent)%") }
                }
            }
        }
        .chartXAxis {
            AxisMarks(values: .stride(by: .hour)) { _ in
                AxisGridLine()
                AxisTick()
                AxisValueLabel(format: .dateTime.hour().minute())
            }
        }
        .chartXScale(range: .plotDimension(padding: Self.edgePadding))
        .frame(height: Self.chartHeight)
        .padding(.vertical, 8)
    }

    /// 地図で選んだ到着。**点ではなく線で示す**——そこに予測の点があるとは限らない
    /// （地図の 1 点はサーバーが補間した値、ここは補間していない生の点）。
    private func selectedArrivalRule(_ arrival: Date) -> some ChartContent {
        RuleMark(x: .value("選んだ到着", arrival))
            // 灰色を名指しする（Charts の中の `.secondary` は系列の色の薄い版になる）
            .foregroundStyle(Color.secondary)
            .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 3]))
            .annotation(
                position: .top, spacing: 2,
                overflowResolution: .init(x: .fit(to: .chart), y: .disabled)
            ) {
                Text("到着").font(.caption2).foregroundStyle(.secondary)
            }
            // 選んだ到着は上の欄が大きく言っている。読み上げで 2 度言わない
            .accessibilityHidden(true)
    }

    /// 帯の色の丸。**縁を付ける**——黄は白地の上で、小さな丸だと消えかける。
    private func symbol(_ band: ProbabilityBand) -> some View {
        Circle()
            .fill(band.color)
            .overlay(Circle().strokeBorder(.primary.opacity(0.35), lineWidth: 0.75))
            .frame(width: Self.pointDiameter, height: Self.pointDiameter)
    }
}
