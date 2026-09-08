import BikeChanceCore
import SwiftUI

/// クレジットと通知文。**表示は義務**（CC BY 4.0 と ODPT ガイドライン 3.1）。
///
/// 文言はアプリに焼き込まず、`/v1/meta` から取る。事業者やライセンス表記が変わっても
/// アプリを出し直さずに追随できる。**取れなかったときは何も出さないのではなく、
/// 取れなかったことを出す**（黙って表示義務を落とさない）。
struct CreditsScreen: View {
    @State private var meta: MetaResponse?
    @State private var message: String?

    var body: some View {
        NavigationStack {
            List {
                if let meta {
                    Section("このアプリが利用している著作物") {
                        ForEach(meta.attribution) { attribution in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(attribution.provider).font(.subheadline)
                                Text(attribution.credit)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            .padding(.vertical, 2)
                        }
                        Text("上記の著作物を改変して利用しています。")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Section("提供元からのお知らせ") {
                        Text(meta.notice).font(.caption)
                    }
                    Section("予測について") {
                        Text(meta.disclaimer).font(.caption)
                    }
                    Section("データの鮮度") {
                        ForEach(meta.feeds) { feed in
                            LabeledContent(feed.displayName) {
                                Text(feed.isStale ? "更新が滞っています" : "更新中")
                                    .foregroundStyle(feed.isStale ? .orange : .secondary)
                            }
                            .font(.caption)
                        }
                    }
                } else if let message {
                    ContentUnavailableView(
                        "クレジットを取得できませんでした", systemImage: "wifi.exclamationmark",
                        description: Text(message))
                } else {
                    ProgressView()
                }
            }
            .navigationTitle("データについて")
            .navigationBarTitleDisplayMode(.inline)
        }
        .task {
            do {
                meta = try await AppEnvironment.makeClient().meta()
            } catch let error as V1Error {
                message = error.message
            } catch {
                message = "通信に失敗しました。"
            }
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
