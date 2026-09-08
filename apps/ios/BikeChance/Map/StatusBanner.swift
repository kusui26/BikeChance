import BikeChanceCore
import SwiftUI

/// 画面上部の状態表示。
///
/// **「多すぎる」を失敗として出さない。** 地図を拡大すれば解決する操作の案内なので、
/// エラー色ではなく案内として見せる。
struct StatusBanner: View {
    let state: StationsModel.State
    let now: Date

    var body: some View {
        Group {
            switch state {
            case .idle:
                EmptyView()
            case .loading:
                banner(icon: "arrow.trianglehead.2.clockwise", text: "読み込み中…", tone: .secondary)
            case .loaded(let response):
                banner(
                    icon: response.isStale ? "exclamationmark.triangle" : "bicycle",
                    text: summary(response), tone: response.isStale ? .orange : .secondary)
            case .needsZoom(let message):
                banner(icon: "plus.magnifyingglass", text: message, tone: .secondary)
            case .failed(let message):
                banner(icon: "wifi.exclamationmark", text: message, tone: .red)
            }
        }
        .animation(.default, value: state)
    }

    /// 件数と鮮度。**「いま」とは書かない**（観測時刻を添える）。
    private func summary(_ response: StationsResponse) -> String {
        let shown = min(response.count, MarkerSelection.displayLimit)
        let base =
            response.count > MarkerSelection.displayLimit
            ? "\(response.count) 件のうち中心に近い \(shown) 件を表示"
            : "\(response.count) 件"
        guard let updated = response.feeds.compactMap(\.dataUpdatedAt).max() else { return base }
        let minutes = max(0, Int(now.timeIntervalSince(updated) / 60))
        let staleness = response.isStale ? "・更新が滞っています" : ""
        return "\(base)・\(minutes) 分前の観測\(staleness)"
    }

    private func banner(icon: String, text: String, tone: Color) -> some View {
        Label(text, systemImage: icon)
            .font(.footnote)
            .foregroundStyle(tone)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.regularMaterial, in: Capsule())
            .padding(.top, 8)
    }
}
