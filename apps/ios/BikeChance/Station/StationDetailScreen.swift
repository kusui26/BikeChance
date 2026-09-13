import BikeChanceCore
import SwiftUI

/// ポートの詳細（W3 プラン §5.11）。地図のピンから push する。
///
/// **判断は `StationDetail` に置いてある。** ここは並べるだけで、
/// 「未観測は 0 と出さない」「鮮度切れの値を『現在値』として出さない」といった規則は
/// `BikeChanceCore` 側でテストしている。
///
/// **予測は到着時刻を選んだときだけ出す**（W4 の PR B）。10 水平の曲線と履歴は W5
/// （`/v1/stations/{system}/{id}` と同じ PR）。
///
/// **予測と現在値を混ぜない。** 欄を分け、予測が出せなくても台数は出す。
struct StationDetailScreen: View {
    let station: StationCurrent
    let feed: FeedStatus?
    let attribution: Attribution?
    let intent: RideIntent
    /// **応答が指している到着時刻**（`StationsResponse.forecastArrival`）。nil なら予測を出さない。
    let arrival: Date?
    let now: Date

    @Environment(\.v1Client) private var client

    private var detail: StationDetail {
        StationDetail(
            station: station, feed: feed, attribution: attribution, intent: intent,
            arrival: arrival, now: now)
    }

    var body: some View {
        List {
            // **確率が主、台数は従**（CLAUDE.md §2 の 7）。出せるときは先に置く
            forecastSection
            availabilitySection
            tripSection
            factsSection
            creditSection
        }
        .listStyle(.insetGrouped)
        .navigationTitle(detail.name)
        .navigationBarTitleDisplayMode(.inline)
    }

    /// ここから行程を調べる（W5 の PR G）。
    ///
    /// **出発ポートはこのポート。** 到着は次の画面で探す——利用者が知っているのは
    /// 「行き先」であってポート名ではない（`DestinationPicker`）。
    ///
    /// **座標の無いポートからは始めない。** 目的地を探す中心が決まらず、`/v1/trip-check`
    /// も座標の無い端点を断る（`station_location_missing`）。
    private var tripSection: some View {
        Section {
            NavigationLink {
                TripCheckScreen(origin: station, client: client)
            } label: {
                Label("ここから行程をチェック", systemImage: "arrow.triangle.turn.up.right.diamond")
            }
        } footer: {
            Text("目的地を選ぶと、**借りられる確率**と**返せる確率**の両方が出ます。")
        }
    }

    /// 到着時刻の確率。**断定しない**（「借りられます」ではなく「借りられる可能性 80%」）。
    ///
    /// 出せないときも欄は出す。**「出せない」ことを伝えるのも情報**で、黙って消すと
    /// 「予測が無い」のか「そもそも選んでいない」のかが分からない。
    @ViewBuilder
    private var forecastSection: some View {
        switch detail.forecast {
        case .notRequested:
            EmptyView()
        case .available(let display):
            Section {
                forecastBody(display)
            } header: {
                Text(display.arrival)
            } footer: {
                Label(display.basis, systemImage: "chart.line.uptrend.xyaxis")
            }
        case .unavailable(let message):
            Section {
                Label(message, systemImage: "questionmark.circle")
                    .foregroundStyle(.secondary)
                    .font(.subheadline)
            } footer: {
                Text("下の台数は予測ではなく、いまの観測値です。")
            }
        }
    }

    private func forecastBody(_ display: ForecastDisplay) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(display.headline)
                .font(.system(.title2, design: .rounded).weight(.semibold))
                .foregroundStyle(display.band.textColor)
            HStack(spacing: 8) {
                Text(display.band.label)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                if display.isReference {
                    // 確度が低い（新設ポート・鮮度不良・再配置直後）。数字を鵜呑みにさせない
                    Text("参考値")
                        .font(.caption2.weight(.medium))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(.quaternary, in: Capsule())
                }
            }
        }
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(display.accessibilityLabel)
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
