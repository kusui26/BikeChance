import BikeChanceCore
import SwiftUI

/// 「データについて」（W6-31・32、契約 42）。**表示は義務**（CC BY 4.0、ODPT ガイドライン 3.1、
/// Open-Meteo・公共データ利用規約の出典）。
///
/// **文言はサーバーが持つ**（`/v1/meta`）。ここは並べ方だけを決める：開発プラン §3.7 の
/// 前置きを**クレジットの前**に置き、ODPT 以外の出典、通知文、予測についての免責と
/// モデル版、鮮度の順に出す。
///
/// **取れなくても表示義務を落とさない。** 最後に取れた内容を、それをサーバーが作った
/// 時刻つきで出す（`CreditsModel`）。1 度も取れていない端末では、取れなかったと言う。
///
/// **中身はアプリに 1 つ**（`BikeChanceApp` が持ち、起動時に 1 度取る。W7-08）。地図からも
/// お気に入りのシートからも、同じものを開く。
struct CreditsScreen: View {
    @Environment(CreditsModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("データについて")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("閉じる") { dismiss() }
                    }
                }
        }
        // **開くたびに取り直す**（鮮度の欄を「いま」にする）。取れなければ、取っておいた内容を出す
        .task { await model.load() }
    }

    @ViewBuilder
    private var content: some View {
        if let display = CreditsDisplay(state: model.state) {
            list(display)
        } else if case .failed(let message) = model.state {
            ContentUnavailableView(
                "クレジットを取得できませんでした", systemImage: "wifi.exclamationmark",
                description: Text(message))
        } else {
            ProgressView()
        }
    }

    private func list(_ display: CreditsDisplay) -> some View {
        List {
            if display.isOffline {
                // **古い内容であることを先に言う**（取っておいた内容を「いま」のものに見せない）
                Section {
                    Label(display.asOf, systemImage: "wifi.exclamationmark")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
            creditSection(display.meta)
            sourcesSection(display.meta)
            Section("提供元からのお知らせ") {
                Text(display.meta.notice).font(.caption)
            }
            Section("予測について") {
                Text(display.meta.disclaimer).font(.caption)
                Text(display.modelVersion).font(.caption).foregroundStyle(.secondary)
            }
            if !display.isOffline {
                feedsSection(display)
            }
        }
    }

    /// **前置きを一覧の前に置く**（開発プラン §3.7。ODPT の FAQ の「改変して利用する場合」）。
    private func creditSection(_ meta: MetaResponse) -> some View {
        Section("クレジット") {
            if let preface = meta.attributionPreface {
                Text(preface).font(.subheadline)
            }
            ForEach(meta.attribution) { attribution in
                VStack(alignment: .leading, spacing: 4) {
                    Text(attribution.provider).font(.subheadline)
                    Text(attribution.credit)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 2)
            }
        }
    }

    /// 天気・祝日・行政区域の出典（W6-31）。**リンクの文言もサーバーのまま**
    /// （Open-Meteo は「Weather data by Open-Meteo.com」を求める）。
    @ViewBuilder
    private func sourcesSection(_ meta: MetaResponse) -> some View {
        if !meta.sources.isEmpty {
            Section("そのほかの出典") {
                ForEach(meta.sources) { source in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(source.text).font(.caption)
                        Link(source.linkLabel, destination: source.url).font(.caption)
                    }
                    .padding(.vertical, 2)
                }
            }
        }
    }

    /// いまの鮮度。**取っておいた内容のときは出さない**（何日も前の「更新中」を見せない）。
    private func feedsSection(_ display: CreditsDisplay) -> some View {
        Section {
            ForEach(display.meta.feeds) { feed in
                LabeledContent(feed.displayName) {
                    Text(feed.isStale ? "更新が滞っています" : "更新中")
                        .foregroundStyle(feed.isStale ? .orange : .secondary)
                }
                .font(.caption)
            }
        } header: {
            Text("データの鮮度")
        } footer: {
            Text(display.asOf)
        }
    }
}

/// 地図の下に常時出す 1 行のクレジット。**地図を見ているあいだも表示義務を満たす。**
struct CreditFooter: View {
    @Binding var showsCredits: Bool

    var body: some View {
        Button {
            showsCredits = true
        } label: {
            Label("データについて（公共交通オープンデータセンター / CC BY 4.0）", systemImage: "info.circle")
                .font(.caption2)
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(.regularMaterial, in: Capsule())
        }
        .buttonStyle(.plain)
        .padding(.bottom, 8)
    }
}
