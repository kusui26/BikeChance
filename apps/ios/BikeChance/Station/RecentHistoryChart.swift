import BikeChanceCore
import Charts
import SwiftUI

/// 直近 24 時間の 1 時間ごとの平均（iOS-3）。**判断は `RecentHistory` に置いてある。** ここは描くだけ。
///
/// **棒で描き、線でつながない。** 観測の無い時間は空いたまま（穴を穴として描く。0046）。
/// 横軸の幅は `RecentHistory` が決める——棒のある範囲に縮めると、観測の途切れが見えなくなる。
///
/// **観測のあった時間の下に線を引く。** 平均 0 台の棒は高さが無く、そのままでは穴と同じに
/// 見える（実測：1 日じゅう 0 台のポートで、グラフが空になった）。0 台は「線だけ」、
/// 観測の無い時間は「線も無い」。
///
/// **色は付けない**（灰色）。帯の色は確率のためのもので、台数に塗ると「予測」に見える。
struct RecentHistoryChart: View {
    let history: RecentHistory

    private static let chartHeight: CGFloat = 140
    /// 横軸の目盛りの間隔（時間）。24 時間を 4 つに切る。
    private static let hourStride = 6
    /// 観測のあった時間の線の太さ（pt）。目盛りの線（細い）と見分けられる太さにする。
    private static let observedLineWidth: CGFloat = 3
    /// 棒と線の色。**灰色を名指しする**——Charts の中の `.secondary` は系列の色（アプリの
    /// 色合い）の薄い版になり、色の付いた棒に見えた。
    private static let observedColor = Color.secondary

    var body: some View {
        Chart(history.bars) { bar in
            BarMark(
                x: .value("時間", bar.hourStart, unit: .hour),
                y: .value(history.title, bar.value)
            )
            .foregroundStyle(Self.observedColor)
            .accessibilityLabel(bar.hourText)
            .accessibilityValue(bar.valueText)
            RuleMark(
                xStart: .value("観測の始まり", bar.hourStart),
                xEnd: .value("観測の終わり", bar.hourEnd),
                y: .value(history.title, 0)
            )
            .foregroundStyle(Self.observedColor)
            .lineStyle(StrokeStyle(lineWidth: Self.observedLineWidth))
            // 値は棒が読み上げる。線は「観測があった」印なので 2 度言わない
            .accessibilityHidden(true)
        }
        .chartXScale(domain: history.start...history.end)
        .chartYScale(domain: 0...Double(history.ticks.last ?? 1))
        .chartYAxis {
            AxisMarks(position: .leading, values: history.ticks.map(Double.init)) { value in
                AxisGridLine()
                AxisValueLabel {
                    if let count = value.as(Double.self) { Text("\(Int(count))") }
                }
            }
        }
        .chartXAxis {
            AxisMarks(values: .stride(by: .hour, count: Self.hourStride)) { _ in
                AxisGridLine()
                AxisTick()
                AxisValueLabel(format: .dateTime.hour())
            }
        }
        .frame(height: Self.chartHeight)
        .padding(.vertical, 8)
    }
}
