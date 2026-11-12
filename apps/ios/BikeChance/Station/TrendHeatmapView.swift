import BikeChanceCore
import SwiftUI

/// ふだんの傾向のヒートマップ（iOS-5。W9-06）。**判断は `TrendHeatmap` に置いてある。** ここは並べるだけ。
///
/// 行が日（今日・明日・…）、列が JST の 0〜23 時。**色は地図と同じ 3 段の帯**（`ProbabilityBand.color`）で、
/// 読み方を 1 つにする。**出せない時間は灰色**（帯の色にしない）、**過ぎた時間は薄く**。
///
/// **押したマスの 1 文を下に出す**（開いたときは今日のいまの時間）。数字をマスに書かないのは、
/// 24 列では字が潰れるため。読み上げは行ごとの要約（168 個のマスを 1 つずつ読ませない）。
///
/// **字の大きさに合わせる。** 日付の欄の幅は字と一緒に広げ、アクセシビリティの大きな字では
/// 日付をマスの上に置く（左に置いたままだと 1 字ずつ折り返し、マスが細い線になる）。
struct TrendHeatmapView: View {
    let heatmap: TrendHeatmap

    @State private var selected: TrendHeatmap.CellID?
    @Environment(\.dynamicTypeSize) private var typeSize

    /// 行の左の日付の幅（「11/30」「火・祝」が収まる）。**字の大きさに合わせて広げる。**
    @ScaledMetric(relativeTo: .caption) private var labelWidth: CGFloat = 46
    private static let cellHeight: CGFloat = 22
    private static let cellSpacing: CGFloat = 1
    private static let cornerRadius: CGFloat = 2
    /// 過ぎた時間の濃さ。**消さない**——今日の形を残したまま、もう来ない時間だと分かるように。
    private static let pastOpacity = 0.3
    /// 出せない時間の色。**帯の色と見分けがつく灰色**（0% の赤と読ませない）。
    private static let collectingColor = Color.secondary.opacity(0.2)
    /// 日付の字を縮めてよい下限（切って「火…」にしない）。
    private static let labelMinimumScale = 0.7

    /// 日付をマスの上に置くか（アクセシビリティの大きな字）。
    private var stacksLabel: Bool { typeSize.isAccessibilitySize }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            hourAxis
            VStack(alignment: .leading, spacing: Self.cellSpacing * 2) {
                ForEach(heatmap.rows) { row in
                    rowView(row)
                }
            }
            selectedLine
        }
        .padding(.vertical, 4)
        .onAppear {
            if selected == nil { selected = heatmap.current }
        }
    }

    /// 横軸の時（0・6・12・18）。マスと同じ割り付けで、目盛りの時の左端に字を置く。
    private var hourAxis: some View {
        HStack(spacing: 0) {
            if !stacksLabel {
                Color.clear.frame(width: labelWidth, height: 1)
            }
            HStack(spacing: Self.cellSpacing) {
                ForEach(0..<24, id: \.self) { hour in
                    Text(TrendHeatmap.hourMarks.contains(hour) ? "\(hour)" : "")
                        .font(.caption2)
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                        .fixedSize()
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .accessibilityHidden(true)
    }

    private func rowView(_ row: TrendHeatmap.Row) -> some View {
        Group {
            if stacksLabel {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        dateLabel(row)
                        weekdayLabel(row)
                    }
                    cellsView(row)
                }
            } else {
                HStack(spacing: 0) {
                    VStack(alignment: .leading, spacing: 0) {
                        dateLabel(row)
                        weekdayLabel(row)
                    }
                    .lineLimit(1)
                    .minimumScaleFactor(Self.labelMinimumScale)
                    .frame(width: labelWidth, alignment: .leading)
                    cellsView(row)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(row.accessibilitySummary)
    }

    private func dateLabel(_ row: TrendHeatmap.Row) -> some View {
        Text(row.dateText)
            .font(.caption.weight(.medium))
    }

    private func weekdayLabel(_ row: TrendHeatmap.Row) -> some View {
        Text(row.weekdayText)
            .font(.caption2)
            .foregroundStyle(row.kind.textColor)
    }

    private func cellsView(_ row: TrendHeatmap.Row) -> some View {
        HStack(spacing: Self.cellSpacing) {
            ForEach(row.cells) { cell in
                cellView(cell, id: TrendHeatmap.CellID(date: row.date, hour: cell.hour))
            }
        }
    }

    private func cellView(_ cell: TrendHeatmap.Cell, id: TrendHeatmap.CellID) -> some View {
        RoundedRectangle(cornerRadius: Self.cornerRadius)
            .fill(cell.band?.color ?? Self.collectingColor)
            .opacity(cell.isPast ? Self.pastOpacity : 1)
            .overlay {
                if selected == id {
                    RoundedRectangle(cornerRadius: Self.cornerRadius)
                        .stroke(.primary, lineWidth: 1.5)
                }
            }
            .frame(maxWidth: .infinity)
            .frame(height: Self.cellHeight)
            .contentShape(Rectangle())
            .onTapGesture { selected = id }
    }

    /// 押したマスの 1 文。**数字はここにだけ出す**（「借りられる 90%（ほぼ大丈夫）」）。
    /// 版の日付は、すぐ下の注記がいつも出している。
    @ViewBuilder
    private var selectedLine: some View {
        if let selected, let cell = heatmap.cell(selected) {
            Text(cell.text)
                .font(.footnote.weight(.medium))
                .foregroundStyle(cell.band?.textColor ?? .secondary)
        } else {
            Text("マスを押すと、その時間のふだんの確率が出ます。")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }
}

extension TrendHeatmap.DayKind {
    /// 曜日の字の色。**土曜は青、日曜と祝日は赤**（暦の慣わし。帯の色とは別の話）。
    var textColor: Color {
        switch self {
        case .weekday: .secondary
        case .saturday: .blue
        case .sundayOrHoliday: .red
        }
    }
}
