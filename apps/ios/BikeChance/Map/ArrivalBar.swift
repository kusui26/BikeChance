import BikeChanceCore
import SwiftUI

/// 到着時刻と「借りる／返す」の切替（開発プラン §9.2 のチップ、W4 の PR B）。
///
/// **時刻で書く。** 「約 30 分後」ではなく「14:35 到着」と出す。応答は CDN に最大 3 分
/// 留まりうるので、相対で書くと読んだときには別の時刻の話になっている（W4 §12 の 114）。
///
/// **借りる／返すは、到着を選んだときだけ出す。** 「いま」の地図は現在値の色で塗っており、
/// そこでは意図によって色が変わらない。効かない操作を置かない。
struct ArrivalBar: View {
    let arrival: ArrivalChoice
    @Binding var intent: RideIntent
    let now: Date
    let select: (ArrivalChoice) -> Void

    var body: some View {
        HStack(spacing: 8) {
            arrivalMenu
            if arrival.wantsForecast {
                intentPicker
            }
        }
        .padding(.horizontal, 12)
        .animation(.default, value: arrival)
    }

    private var arrivalMenu: some View {
        Menu {
            // **選んでいるものに印を付ける。** 閉じているときはラベルしか見えない
            Picker("到着", selection: selection) {
                ForEach(ArrivalChoice.choices) { choice in
                    Text(choice.pickerLabel).tag(choice)
                }
            }
        } label: {
            Label(arrival.label(now: now), systemImage: "clock")
                .font(.footnote.weight(.medium))
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.regularMaterial, in: Capsule())
        }
        .accessibilityLabel("到着時刻。いまは \(arrival.label(now: now))")
    }

    /// `Picker` に渡す口。**選び直しは `StationsModel` を通す**（取り直しが要るため）。
    private var selection: Binding<ArrivalChoice> {
        Binding(get: { arrival }, set: { select($0) })
    }

    private var intentPicker: some View {
        Picker("したいこと", selection: $intent) {
            ForEach(RideIntent.allCases) { intent in
                Text(intent.label).tag(intent)
            }
        }
        .pickerStyle(.segmented)
        .frame(maxWidth: 160)
        .background(.regularMaterial, in: Capsule())
    }
}
