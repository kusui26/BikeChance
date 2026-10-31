import BikeChanceCore
import SwiftUI

/// まとめた印を押したときに出す、系統を選ぶ一覧（W8 プランの W8-11、所見 232）。
///
/// **判断は `MarkerChoice`（Core）にあり、ここは並べるだけ。** 点の色は地図のピンと同じ規則
/// （`tint`。呼ぶ側の 1 か所）で塗る——1 つずつ出していたら付いた色を、そのまま並べる。
/// 選ぶと閉じ、閉じきってから詳細へ進む（呼ぶ側の `onDismiss`）。
struct MarkerChooser: View {
    let choices: [MarkerChoice]
    let tint: (StationCurrent) -> Color
    let choose: (StationCurrent) -> Void

    var body: some View {
        NavigationStack {
            List(choices) { choice in
                Button {
                    choose(choice.station)
                } label: {
                    row(choice)
                }
                // **文字はふつうの色**（`DestinationPicker` と同じ）。リストのボタンは文字を強調色で
                // 塗り、緑は「ほぼ大丈夫」の帯と同じ色なので、どの行も良さそうに見えた。色で話すのは左の点だけ
                .buttonStyle(.plain)
                .accessibilityLabel(accessibilityText(choice))
            }
            .navigationTitle(choices.first?.name ?? "")
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium])
    }

    private func row(_ choice: MarkerChoice) -> some View {
        HStack(spacing: 12) {
            Circle()
                .fill(tint(choice.station))
                .frame(width: 12, height: 12)
            VStack(alignment: .leading, spacing: 2) {
                Text(choice.systemName).font(.headline)
                Text(choice.headline).font(.subheadline)
                if let detail = choice.detail {
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
    }

    /// 読み上げ。**系統を先に言う**（どちらの話かを数字より先に）。
    private func accessibilityText(_ choice: MarkerChoice) -> String {
        [choice.systemName, choice.headline, choice.detail].compactMap { $0 }.joined(separator: "、")
    }
}
