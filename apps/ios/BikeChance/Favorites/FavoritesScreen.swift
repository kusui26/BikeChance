import BikeChanceCore
import SwiftUI

/// お気に入り（W5 プラン §6.8、開発プラン §9.2）。
///
/// **判断は `FavoritesModel` と `FavoriteRow` に置いてある。** ここは並べるだけで、
/// 「取れなかった行は前の値を古さつきで出す」「未観測は 0 と区別する」といった規則は
/// `BikeChanceCore` 側でテストしている。
///
/// **0 件のときも画面がある**（完了条件 4）。空白を出すと、壊れているのか
/// まだ登録していないのかが分からない。
///
/// **データを出しているあいだはクレジットも出す**（W7-08、W6 プランの所見 219）。この画面は
/// シートで開くので、地図の下の 1 行のクレジットが隠れる。
struct FavoritesScreen: View {
    @State var model: FavoritesModel
    /// 「N 分前の観測」を進める刻み（`MapScreen` と同じ考え方）。
    private static let tickSeconds = 30

    @Environment(\.dismiss) private var dismiss
    @State private var now = Date()
    @State private var showsCredits = false

    var body: some View {
        NavigationStack {
            Group {
                if model.favorites.isEmpty {
                    emptyState
                } else {
                    list
                }
            }
            .navigationTitle("お気に入り")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("閉じる") { dismiss() }
                }
                if !model.favorites.isEmpty {
                    ToolbarItem(placement: .primaryAction) { EditButton() }
                }
            }
        }
        // 中身はアプリに 1 つ（地図の「データについて」と同じもの）
        .sheet(isPresented: $showsCredits) { CreditsScreen() }
        .task {
            model.reload()
            while !Task.isCancelled {
                now = Date()
                // **時計を進めるだけ。** 取りに行かない（1 件 1 要求なので、
                // 開いたまま置かれても叩き続けない）
                model.tick()
                try? await Task.sleep(for: .seconds(Self.tickSeconds))
            }
        }
    }

    /// **0 件のとき**（完了条件 4）。何をすれば増えるかまで書く。
    private var emptyState: some View {
        ContentUnavailableView {
            Label("お気に入りはまだありません", systemImage: "star")
        } description: {
            Text("地図でポートを開いて ☆ を押すと、ここに並びます。\nよく使うポートを \(Favorites.limit) 件まで登録できます。")
        }
    }

    private var list: some View {
        List {
            Section {
                Picker("したいこと", selection: $model.intent) {
                    ForEach(RideIntent.allCases) { intent in
                        Text(intent.label).tag(intent)
                    }
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
            } footer: {
                Text("切り替えても取得し直しません（同じ応答に両方の確率が入っています）。")
            }

            Section {
                ForEach(model.rows) { row in
                    FavoriteRowView(row: row) { minutes in
                        model.setUsual(minutes, for: row.id)
                    }
                }
                .onDelete { model.remove(atOffsets: $0) }
                .onMove { model.move(fromOffsets: $0, toOffset: $1) }
            } footer: {
                Text("\(model.favorites.count) / \(Favorites.limit) 件")
            }

            if let credits = model.credits {
                creditSection(credits)
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { model.reload() }
    }

    /// **表示は義務**（CC BY 4.0 と ODPT ガイドライン 3.1）。文言は応答のまま、**前置きを
    /// クレジットの前に置く**（開発プラン §3.7）。ポート詳細・行程チェックと同じ形で、
    /// 系統が 2 つなら 2 行。「データについて」へも行ける。
    private func creditSection(_ credits: FavoritesCredits) -> some View {
        Section("データの出典") {
            VStack(alignment: .leading, spacing: 4) {
                if let preface = credits.preface {
                    Text(preface)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                ForEach(credits.credits, id: \.self) { credit in
                    Text(credit).font(.caption)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button {
                showsCredits = true
            } label: {
                Label("データについて", systemImage: "info.circle").font(.caption)
            }
        }
    }
}

/// 1 行。**確率が主、台数は従**（CLAUDE.md §2 の 7）。
struct FavoriteRowView: View {
    let row: FavoriteRow
    let setUsual: (Int?) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            forecast
            counts
            if let note = row.note {
                // **取れなかったことを必ず言う**（開発プラン §9.4）。
                // 値そのものの古さは下の「○分前の観測」が持っている
                Label(note, systemImage: "wifi.exclamationmark")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
        .padding(.vertical, 4)
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(row.name).font(.body.weight(.medium))
            Spacer(minLength: 8)
            usualMenu
        }
    }

    /// 「いつもの時刻」。**選んでも取りに行かない**（曲線は取ってある）。
    private var usualMenu: some View {
        Menu {
            Picker("いつもの時刻", selection: selection) {
                ForEach(Favorites.usualChoices, id: \.self) { choice in
                    Text(Favorites.usualLabel(choice)).tag(choice)
                }
            }
        } label: {
            Label(Favorites.usualLabel(row.favorite.usualMinutes), systemImage: "clock")
                .font(.caption2)
                .labelStyle(.titleAndIcon)
        }
        .accessibilityLabel("いつもの時刻。いまは \(Favorites.usualLabel(row.favorite.usualMinutes))")
    }

    private var selection: Binding<Int?> {
        Binding(get: { row.favorite.usualMinutes }, set: { setUsual($0) })
    }

    @ViewBuilder
    private var forecast: some View {
        switch row.forecast {
        case .available(let display):
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(display.headline)
                        .font(.system(.title3, design: .rounded).weight(.semibold))
                        .foregroundStyle(display.band.textColor)
                    Text(display.arrival).font(.caption).foregroundStyle(.secondary)
                    if display.isReference {
                        Text("参考値")
                            .font(.caption2.weight(.medium))
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(.quaternary, in: Capsule())
                    }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(display.accessibilityLabel)
                // **予測値には予測時刻を添える**（W6-32、所見 196）
                Text(display.basis).font(.caption2).foregroundStyle(.secondary)
            }
        case .unavailable(let message):
            Text(message).font(.caption).foregroundStyle(.secondary)
        case .notRequested:
            // 「いつもの時刻」を決めていない。**現在値だけを見る**
            EmptyView()
        }
    }

    /// 現在値と観測時刻。**実測値には必ず観測時刻を添える**（CLAUDE.md §2 の 7）。
    private var counts: some View {
        HStack(spacing: 12) {
            ForEach(row.availability) { item in
                HStack(spacing: 3) {
                    Text(item.title).font(.caption2).foregroundStyle(.secondary)
                    Text(item.value).font(.caption.weight(.medium)).monospacedDigit()
                    if let caution = item.caution {
                        Text(caution).font(.caption2).foregroundStyle(.orange)
                    }
                }
            }
            Spacer(minLength: 0)
            Text(row.freshness.label)
                .font(.caption2)
                .foregroundStyle(row.freshness.isPresentable ? Color.secondary : Color.orange)
        }
    }
}
