import BikeChanceCore
import SwiftUI

/// ポートの詳細（W3 プラン §5.11）。地図のピンから push する。
///
/// **判断は `StationDetail` に置いてある。** ここは並べるだけで、
/// 「未観測は 0 と出さない」「鮮度切れの値を『現在値』として出さない」といった規則は
/// `BikeChanceCore` 側でテストしている。
///
/// **選んだ到着の予測は、地図で到着時刻を選んだときだけ出す**（W4 の PR B）。
/// **到着時刻ごとの予測（10 水平）と直近 24 時間は、開いたときに詳細の応答から出す**（iOS-3。
/// 判断は `ForecastTimeline` と `RecentHistory`）。台数と鮮度は地図の行のまま（ピンと同じ値）。
///
/// **予測と現在値を混ぜない。** 欄を分け、予測が出せなくても台数は出す。
struct StationDetailScreen: View {
    let station: StationCurrent
    let feed: FeedStatus?
    let attribution: Attribution?
    /// クレジットの前置き（W6-31）。**地図の応答の文言**を渡す。
    let attributionPreface: String?
    let intent: RideIntent
    /// **応答が指している到着時刻**（`StationsResponse.forecastArrival`）。nil なら予測を出さない。
    let arrival: Date?
    /// 地図の時計（30 秒ごと）。**詳細を開いているあいだも進む**（地図の `.task` はナビゲーションの
    /// 外側にある。所見 233）。「N 分前の観測」と、過ぎた予測の点を落とすのに使う。
    let now: Date
    /// **アプリに 1 つだけ**（`BikeChanceApp` が持つ）。☆ の状態と一覧が同じものを見る。
    var favorites: FavoritesModel

    @Environment(\.v1Client) private var client
    @Environment(\.scenePhase) private var scenePhase
    /// 詳細の応答（到着時刻ごとの予測と直近 24 時間）。
    @State private var loader = StationDetailModel()
    /// 増やすと取り直す（「もう一度読み込む」・前面に戻った）。
    @State private var attempt = 0

    private var detail: StationDetail {
        StationDetail(
            station: station, feed: feed, attribution: attribution,
            attributionPreface: attributionPreface, intent: intent, arrival: arrival, now: now)
    }

    var body: some View {
        List {
            // **確率が主、台数は従**（CLAUDE.md §2 の 7）。予測を先に、実測を後に置く
            forecastSection
            timelineSection
            availabilitySection
            recentSection
            tripSection
            factsSection
            creditSection
        }
        .listStyle(.insetGrouped)
        .navigationTitle(detail.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .primaryAction) { favoriteButton } }
        // **開くたびに 1 度取る。開いたまま叩き続けない**（お気に入りと同じ）。画面が消えれば取り消す
        .task(id: attempt) {
            await loader.load(
                client: client, system: station.systemID, stationID: station.stationID)
        }
        // 前面に戻ったら取り直す。開いたまま置かれた画面の予測を、古い点のままにしない
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { attempt += 1 }
        }
    }

    /// ☆。**上限に達していたら押せなくする**（押せるのに何も起きない、を作らない）。
    private var favoriteButton: some View {
        let saved = favorites.contains(station)
        return Button {
            if saved {
                favorites.remove(station.id)
            } else {
                favorites.add(station)
            }
        } label: {
            Image(systemName: saved ? "star.fill" : "star")
        }
        .disabled(!saved && favorites.isFull)
        .accessibilityLabel(saved ? "お気に入りから外す" : "お気に入りに登録")
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

    // MARK: - 到着時刻ごとの予測（iOS-3）

    /// 到着時刻ごとの予測。**取れるまでは読み込み中、取れなければ理由ともう一度のボタン。**
    @ViewBuilder
    private var timelineSection: some View {
        switch loader.state {
        case .loading:
            Section(ForecastTimeline.header(for: intent)) { loadingRow }
        case .failed(let message):
            Section(ForecastTimeline.header(for: intent)) {
                Label(message, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.secondary)
                    .font(.subheadline)
                Button("もう一度読み込む") { attempt += 1 }
            }
        case .loaded(let response):
            timelineSection(
                ForecastTimelineState.make(
                    detail: response, intent: intent, selectedArrival: arrival, now: now))
        }
    }

    /// **出せないときも欄は出す**（`forecastSection` と同じ。黙って消すと、無いのか選んでいないのか
    /// 分からない）。出せるときは、**基づく観測の時刻と、点をつながない理由を必ず添える。**
    @ViewBuilder
    private func timelineSection(_ state: ForecastTimelineState) -> some View {
        switch state {
        case .unavailable(let message):
            Section(ForecastTimeline.header(for: intent)) {
                Label(message, systemImage: "questionmark.circle")
                    .foregroundStyle(.secondary)
                    .font(.subheadline)
            }
        case .available(let timeline):
            Section {
                if timeline.isReference {
                    Label(ForecastTimeline.referenceNote, systemImage: "info.circle")
                        .font(.caption)
                }
                ForecastTimelineChart(timeline: timeline)
                timelineNumbers(timeline)
            } header: {
                Text(ForecastTimeline.header(for: intent))
            } footer: {
                VStack(alignment: .leading, spacing: 4) {
                    Label(timeline.basis, systemImage: "chart.dots.scatter")
                    Text(ForecastTimeline.pointsNote)
                }
            }
        }
    }

    /// 点の数字。**グラフからは読み取れない値を、畳んで置く**（読み上げでも 1 点ずつ読める）。
    private func timelineNumbers(_ timeline: ForecastTimeline) -> some View {
        DisclosureGroup("数字で見る") {
            ForEach(timeline.points) { point in
                LabeledContent(point.display.arrival) {
                    Text("\(point.display.percent)%")
                        .foregroundStyle(point.display.band.textColor)
                        .monospacedDigit()
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(point.accessibilityLabel)
            }
        }
    }

    private var loadingRow: some View {
        ProgressView()
            .frame(maxWidth: .infinity)
            .padding(.vertical, 8)
    }

    // MARK: - 直近 24 時間（iOS-3）

    /// 直近 24 時間。**実測値なので観測の時刻を添える**（契約 43）。取れていなければ出さない
    /// （読み込み中と失敗は上の予測の欄が言う）。
    @ViewBuilder
    private var recentSection: some View {
        if case .loaded(let response) = loader.state {
            recentSection(RecentHistoryState.make(detail: response, intent: intent))
        }
    }

    @ViewBuilder
    private func recentSection(_ state: RecentHistoryState) -> some View {
        switch state {
        case .empty(let message):
            Section(RecentHistory.header(for: intent)) {
                Label(message, systemImage: "clock.badge.questionmark")
                    .foregroundStyle(.secondary)
                    .font(.subheadline)
            }
        case .unbounded(let note):
            // **上限なしのポートの空き枠は数を出さない**（W7-05。番兵の 32767 を棒にしない）
            Section(RecentHistory.header(for: intent)) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(StationDetail.unboundedValue)
                        .font(.system(.title3, design: .rounded).weight(.semibold))
                    Text(note)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 4)
            }
        case .available(let history):
            Section {
                RecentHistoryChart(history: history)
            } header: {
                Text(RecentHistory.header(for: intent))
            } footer: {
                Label(history.footnote, systemImage: "clock")
            }
        }
    }

    // MARK: - 現在値

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

    /// **表示は義務**（CC BY 4.0 と ODPT ガイドライン 3.1）。文言はサーバーから貰い、
    /// **前置きを一覧の前に置く**（開発プラン §3.7。W6-31）。
    @ViewBuilder
    private var creditSection: some View {
        if let credit = detail.credit {
            Section("データの出典") {
                VStack(alignment: .leading, spacing: 4) {
                    if let preface = detail.creditPreface {
                        Text(preface)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Text(credit).font(.caption)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}
