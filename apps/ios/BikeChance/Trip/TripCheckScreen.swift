import BikeChanceCore
import SwiftUI

/// 行程チェック（W5 プラン §6.7、開発プラン §9.2）。
///
/// **判断は `TripPlan` に置いてある。** ここは並べるだけで、「両端がそろわなければ
/// 掛け算を出さない」「注記を数と同じ欄に置く」「時刻は絶対で書く」といった規則は
/// `BikeChanceCore` 側でテストしている。
///
/// **画面のいちばん大きい字は「両方うまくいく可能性」**にする。利用者が知りたいのは
/// 片側ずつの確率ではなく、行って帰れるかどうかである。ただし**その数は独立仮定の
/// 掛け算**なので、注記を同じ欄から離さない（W4-24）。
struct TripCheckScreen: View {
    /// 出発ポート。地図の詳細画面から渡ってくる。
    let origin: StationCurrent
    let client: V1Client

    @State private var model: TripModel
    @State private var destination: StationCurrent?
    @State private var showsPicker = false
    @State private var now = Date()

    /// - Parameter destination: 到着ポートが**もう分かっている**ときに渡す
    ///   （お気に入りから「ここへ行く」を選んだ場合など）。渡せば開いた時点で調べる。
    init(origin: StationCurrent, destination: StationCurrent? = nil, client: V1Client) {
        self.origin = origin
        self.client = client
        _destination = State(initialValue: destination)
        _model = State(
            initialValue: TripModel(client: client, estimator: MapKitRideEstimator()))
    }

    /// 「N 分前の観測」を進める刻み（`MapScreen` と同じ考え方）。
    private static let tickSeconds = 30

    var body: some View {
        List {
            routeSection
            departureSection
            resultSection
        }
        .listStyle(.insetGrouped)
        .navigationTitle("行程をチェック")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showsPicker) {
            DestinationPicker(origin: origin, client: client, now: now) { station in
                destination = station
                check()
            }
        }
        .task {
            // 到着が分かって開いたなら、**待たせずに調べる**（押させる意味が無い）
            if destination != nil, model.state == .idle { check() }
            while !Task.isCancelled {
                now = Date()
                try? await Task.sleep(for: .seconds(Self.tickSeconds))
            }
        }
    }

    // MARK: - 入力

    private var routeSection: some View {
        Section("経路") {
            LabeledContent("出発") { Text(origin.name ?? "（名称未取得）") }
            LabeledContent("到着") {
                Button(destination?.name ?? "選ぶ") { showsPicker = true }
            }
        }
    }

    /// 出発時刻。**「いま」は無い**——`/v1/trip-check` は 5 分以上先しか受けない。
    private var departureSection: some View {
        Section {
            Picker("出発", selection: $model.departureMinutes) {
                ForEach(TripModel.departureChoices, id: \.self) { minutes in
                    Text(Arrival.relativeText(minutes: minutes)).tag(minutes)
                }
            }
            .pickerStyle(.segmented)
            if destination != nil {
                Button("この行程を調べる") { check() }
                    .frame(maxWidth: .infinity)
            }
        } header: {
            Text("いつ出発しますか")
        } footer: {
            Text("いまから 5 分以上先で、到着が 3 時間先までの行程を調べられます。")
        }
    }

    private func check() {
        guard let destination else { return }
        model.check(from: origin, to: destination)
    }

    // MARK: - 結果

    @ViewBuilder
    private var resultSection: some View {
        switch model.state {
        case .idle:
            EmptyView()
        case .loading:
            Section { ProgressView().frame(maxWidth: .infinity) }
        case .failed(let message):
            // **断られた理由をそのまま出す**（完了条件 4）。出発時刻を変えれば通る
            // 断られ方があるので、「通信に失敗しました」に畳まない
            Section {
                Label(message, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                    .font(.subheadline)
            }
        case .loaded:
            if let plan = model.plan(now: now) {
                outcomeSection(plan)
                endpointSection(plan.from, ride: plan.ride)
                endpointSection(plan.to, ride: nil)
                creditSection(plan)
            }
        }
    }

    /// 「両方うまくいく可能性」。**注記を同じ欄に置く**（W4-24）。
    @ViewBuilder
    private func outcomeSection(_ plan: TripPlan) -> some View {
        switch plan.outcome {
        case .available(let display):
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    Text(display.headline)
                        .font(.system(.title2, design: .rounded).weight(.semibold))
                        .foregroundStyle(display.band.textColor)
                    HStack(spacing: 8) {
                        Text(display.band.label)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        if display.isReference {
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
            } header: {
                Text(display.window)
            } footer: {
                // **数と離さない。** 別の画面や別の節に送ると、注記を落として数だけ
                // 読まれる（W4-24）
                Text(display.notice)
            }
        case .unavailable(let message):
            Section {
                Label(message, systemImage: "questionmark.circle")
                    .foregroundStyle(.secondary)
                    .font(.subheadline)
            } footer: {
                Text("下の 2 つは、それぞれの時刻の見通しです。")
            }
        }
    }

    /// 片側ぶん。**確率が主、台数は従**（CLAUDE.md §2 の 7）。
    private func endpointSection(_ endpoint: TripEndpoint, ride: RideSummary?) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                forecastRow(endpoint)
                countRow(endpoint)
            }
            .padding(.vertical, 2)
            ForEach(endpoint.alternatives) { alternative in
                alternativeRow(alternative)
            }
        } header: {
            Text("\(endpoint.clock)　\(endpoint.name)")
        } footer: {
            VStack(alignment: .leading, spacing: 4) {
                Text(endpoint.freshness.label)
                if let ride {
                    Label(ride.label, systemImage: "bicycle")
                    if let note = ride.note {
                        Text(note)
                    }
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func forecastRow(_ endpoint: TripEndpoint) -> some View {
        switch endpoint.forecast {
        case .available(let display):
            HStack(spacing: 8) {
                Text(display.headline)
                    .font(.system(.title3, design: .rounded).weight(.semibold))
                    .foregroundStyle(display.band.textColor)
                Text(display.band.label).font(.caption).foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(display.accessibilityLabel)
        case .unavailable(let message):
            Text(message).font(.subheadline).foregroundStyle(.secondary)
        case .notRequested:
            // 行程チェックは必ず時刻を指定するので、ここには来ない
            EmptyView()
        }
    }

    private func countRow(_ endpoint: TripEndpoint) -> some View {
        HStack(spacing: 6) {
            Text("いま \(endpoint.count.title) \(endpoint.count.value)")
                .font(.caption)
                .foregroundStyle(.secondary)
                .monospacedDigit()
            if let caution = endpoint.count.caution {
                Text(caution).font(.caption2).foregroundStyle(.orange)
            }
        }
    }

    /// 代替候補。**並べ替えない**（順序はサーバーが確率の高い順に決めている）。
    private func alternativeRow(_ alternative: TripAlternativeRow) -> some View {
        LabeledContent {
            Text(alternative.forecast.display?.headline ?? "—")
                .font(.caption)
                .foregroundStyle(alternative.forecast.band?.textColor ?? .secondary)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(alternative.name).font(.subheadline)
                Text("徒歩 \(alternative.distance)").font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    /// **表示は義務**（CC BY 4.0）。
    @ViewBuilder
    private func creditSection(_ plan: TripPlan) -> some View {
        if let credit = plan.credit {
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
