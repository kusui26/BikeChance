import BikeChanceCore
import SwiftUI

/// ポートの詳細（W3 プラン §5.11）。地図のピンから push する。
///
/// **判断は `StationDetail` に置いてある。** ここは並べるだけで、
/// 「未観測は 0 と出さない」「鮮度切れの値を『現在値』として出さない」といった規則は
/// `BikeChanceCore` 側でテストしている。
///
/// **予測と履歴は出さない。** 予測は W4（`/v1` がまだ返さない）、履歴は W5
/// （`/v1/stations/{id}` と同じ PR）。
struct StationDetailScreen: View {
    let station: StationCurrent
    let feed: FeedStatus?
    let attribution: Attribution?
    let now: Date

    private var detail: StationDetail {
        StationDetail(station: station, feed: feed, attribution: attribution, now: now)
    }

    var body: some View {
        List {
            availabilitySection
            factsSection
            creditSection
        }
        .listStyle(.insetGrouped)
        .navigationTitle(detail.name)
        .navigationBarTitleDisplayMode(.inline)
    }

    /// 借りられる／返せる。**実測値なので、観測時刻を必ず添える**（CLAUDE.md §2 の 7）。
    private var availabilitySection: some View {
        Section {
            HStack(alignment: .top, spacing: 32) {
                ForEach(detail.availability) { counter($0) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 6)
        } header: {
            Text(detail.systemName)
        } footer: {
            Label(
                detail.freshness.label,
                systemImage: detail.freshness.isPresentable
                    ? "clock" : "clock.badge.exclamationmark"
            )
            .foregroundStyle(detail.freshness.isPresentable ? Color.secondary : Color.orange)
        }
    }

    private func counter(_ item: Availability) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(item.title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(item.value)
                .font(.system(.largeTitle, design: .rounded).weight(.semibold))
                .monospacedDigit()
            // 停止中は数を出さない。代わりに理由を出す（`StationDetail` の規則）
            Text(item.caution ?? " ")
                .font(.caption2)
                .foregroundStyle(.orange)
                .opacity(item.caution == nil ? 0 : 1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.title) \(item.value)\(item.caution.map { "、\($0)" } ?? "")")
    }

    private var factsSection: some View {
        Section("このポートについて") {
            ForEach(detail.facts) { fact in
                VStack(alignment: .leading, spacing: 2) {
                    LabeledContent(fact.label) {
                        Text(fact.value).monospacedDigit()
                    }
                    if let note = fact.note {
                        Text(note)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }

    /// **表示は義務**（CC BY 4.0 と ODPT ガイドライン 3.1）。文言はサーバーから貰う。
    @ViewBuilder
    private var creditSection: some View {
        if let credit = detail.credit {
            Section("データの出典") {
                VStack(alignment: .leading, spacing: 4) {
                    Text(credit).font(.caption)
                    Text("上記の著作物を改変して利用しています。")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}
