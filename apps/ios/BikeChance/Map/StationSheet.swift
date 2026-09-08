import BikeChanceCore
import SwiftUI

/// ポートを選んだときの簡易シート。**W2 は現在値だけ**（詳細画面と予測は W4 以降）。
///
/// 数字には必ず観測時刻を添える。未観測は「—」にして、**0 と区別する**。
struct StationSheet: View {
    let station: StationCurrent
    let feed: FeedStatus?
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text(station.name ?? "（名称未取得）")
                    .font(.headline)
                Text(feed?.displayName ?? station.systemID)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 24) {
                counter("借りられる", value: station.bikes, enabled: station.isRenting)
                counter("返せる", value: station.docks, enabled: station.isReturning)
            }

            // 実測値には観測時刻を必ず添える（CLAUDE.md §2 の 7）
            Label(
                freshness.label,
                systemImage: freshness.isPresentable ? "clock" : "clock.badge.exclamationmark"
            )
            .font(.caption)
            .foregroundStyle(freshness.isPresentable ? Color.secondary : Color.orange)

            if station.capacity != nil, feed?.capacityIsDynamic == true {
                Text("このシステムの「容量」は固定の台数ではなく、その時点の台数と空き枠の合計です。")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            Spacer(minLength: 0)
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var freshness: Freshness {
        station.freshness(feed: feed, now: now)
    }

    /// 台数の表示。**未観測は「—」**。0 台と混同させない。
    private func counter(_ title: String, value: Int?, enabled: Bool?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(display(value: value, enabled: enabled))
                .font(.system(.title, design: .rounded).weight(.semibold))
                .monospacedDigit()
            if enabled == false {
                Text("停止中").font(.caption2).foregroundStyle(.orange)
            }
        }
    }

    private func display(value: Int?, enabled: Bool?) -> String {
        guard freshness.isPresentable, let value else { return "—" }
        return enabled == false ? "—" : "\(value)"
    }
}
