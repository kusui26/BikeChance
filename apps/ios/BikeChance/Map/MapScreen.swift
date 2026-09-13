import BikeChanceCore
import MapKit
import SwiftUI

/// ホーム。現在地周辺のポートを地図に出す。
///
/// 値には必ず観測時刻を添え、フィードが滞っていれば「現在値」として出さない
/// （CLAUDE.md §2 の 7・8）。
///
/// **現在値の色と予測の色を混ぜない**（W4 の PR B）。到着時刻を選んでいなければ現在値の
/// 色（借りられる／返せる）で塗り、選べば**その時刻の確率の帯**で塗る。1 枚の地図に
/// 2 つの意味の色が同時に出ることはない。
///
/// 位置情報の許可が無くても使える。初回は東京駅を中心に置き、あとは地図の操作で完結する
/// （App Store 審査ガイドライン 5.1.2、開発プラン §9.2）。
struct MapScreen: View {
    @State var model: StationsModel
    /// **アプリに 1 つだけ**（`BikeChanceApp` が持つ）。詳細画面の ☆ と一覧が同じものを見る。
    var favorites: FavoritesModel
    /// 背面に回っているあいだは取りに行かない。
    @Environment(\.scenePhase) private var scenePhase
    @State private var camera: MapCameraPosition = .region(MapScreen.initialRegion)
    @State private var visible: [StationCurrent] = []
    @State private var feeds: [String: FeedStatus] = [:]
    @State private var attributions: [String: Attribution] = [:]
    @State private var selected: StationCurrent?
    @State private var showsCredits = false
    @State private var showsFavorites = false
    @State private var now = Date()
    /// **いま出している応答が指している到着時刻。** 端末の時計から計算し直さない
    /// （応答は CDN に留まりうる。W4 プラン §12 の 114）。
    @State private var forecastArrival: Date?

    /// 東京駅まわり。**約 0.02 度四方**で、件数の上限にも表示上限にも余裕がある。
    static let initialRegion = MKCoordinateRegion(
        center: CLLocationCoordinate2D(latitude: 35.681236, longitude: 139.767125),
        span: MKCoordinateSpan(latitudeDelta: 0.02, longitudeDelta: 0.02)
    )

    /// 「N 分前の観測」を進め、頃合いなら取り直すための刻み。**Combine を持ち込まない**
    /// ために `Task.sleep` で回す（`.task` は画面が消えれば自動で止まる）。
    ///
    /// 取り直す間隔は `StationsModel.refreshInterval`（60 秒）で、刻みはその半分にする。
    /// 刻みと間隔を同じにすると、判定の境目で 1 周ぶん（最大 60 秒）遅れることがある。
    private static let tickSeconds = 30

    var body: some View {
        NavigationStack {
            map
                // 地図は全面に出す。詳細画面のほうは自分でバーを持つ
                .toolbar(.hidden, for: .navigationBar)
                .navigationDestination(item: $selected) { station in
                    // 選んだあとに再取得が走ることがあるので、**表示は最新の行から引き直す**
                    let latest = visible.first { $0.id == station.id } ?? station
                    StationDetailScreen(
                        station: latest,
                        feed: feeds[latest.systemID],
                        attribution: attributions[latest.systemID],
                        intent: model.intent,
                        arrival: forecastArrival,
                        now: now,
                        favorites: favorites
                    )
                }
        }
    }

    private var map: some View {
        Map(position: $camera, selection: $selected) {
            UserAnnotation()
            ForEach(visible) { station in
                Marker(
                    station.name ?? station.stationID,
                    systemImage: "bicycle",
                    coordinate: CLLocationCoordinate2D(
                        latitude: station.latitude, longitude: station.longitude)
                )
                .tint(tint(for: station))
                .tag(station)
            }
        }
        .mapControls {
            MapUserLocationButton()
            MapCompass()
        }
        // 動かしている最中ではなく、止まったときだけ問い合わせる（開発プラン §9.1）
        .onMapCameraChange(frequency: .onEnd) { context in
            model.viewportChanged(to: Bbox(region: context.region))
        }
        .safeAreaInset(edge: .top) {
            VStack(spacing: 8) {
                StatusBanner(state: model.state, now: now)
                HStack(spacing: 8) {
                    ArrivalBar(
                        arrival: model.arrival, intent: $model.intent, now: now,
                        select: { model.select(arrival: $0) })
                    Spacer(minLength: 0)
                    favoritesButton
                }
            }
        }
        .safeAreaInset(edge: .bottom) { CreditFooter(showsCredits: $showsCredits) }
        .sheet(isPresented: $showsCredits) { CreditsScreen() }
        .sheet(isPresented: $showsFavorites) { FavoritesScreen(model: favorites) }
        .onChange(of: model.state) { _, state in apply(state) }
        // 最初の 1 回。カメラの位置は以降 `onMapCameraChange` が持つ
        .task { model.viewportChanged(to: Bbox(region: MapScreen.initialRegion)) }
        // **`id:` を付けて、場面が変わるたびに作り直す。**
        // `.task` に閉じ込めた `@Environment` は**開始時の値のまま固まる**ので、
        // ループの中で `scenePhase` を見ても永遠に起動時の値になる（§12 の 108）。
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            while !Task.isCancelled {
                now = Date()
                // **時刻を進めるだけでは値は新しくならない**（§12 の 107）。
                // 地図を動かさない利用者には、データが古くなっていく様子だけが見えて、
                // やがて全部が灰色になったまま戻らなかった。
                // 前面に戻った直後にも 1 度試すよう、眠る前に呼ぶ
                model.refreshIfDue()
                try? await Task.sleep(for: .seconds(Self.tickSeconds))
            }
        }
    }

    /// お気に入りへの導線（開発プラン §9.2）。**件数を出す**——0 件でも開けるようにして、
    /// 「まだ登録していない」ことが分かる画面へ連れていく（完了条件 4）。
    private var favoritesButton: some View {
        Button {
            showsFavorites = true
        } label: {
            Label(
                favorites.favorites.isEmpty ? "お気に入り" : "\(favorites.favorites.count)",
                systemImage: "star.fill"
            )
            .font(.footnote.weight(.medium))
            .labelStyle(.titleAndIcon)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.regularMaterial, in: Capsule())
        }
        .padding(.trailing, 12)
        .accessibilityLabel("お気に入り \(favorites.favorites.count) 件")
    }

    /// 応答から画面の状態を作る。**表示上限を超えたら中心に近い順に絞る。**
    private func apply(_ state: StationsModel.State) {
        guard case .loaded(let response) = state else {
            if case .needsZoom = state { visible = [] }
            return
        }
        feeds = response.feedIndex()
        attributions = response.attributionIndex()
        // **応答が指している到着時刻を持つ。** 選択中の到着ではない（取り直しの最中は
        // まだ前の応答が出ており、ピンの色はそちらの時刻の話をしている）
        forecastArrival = response.forecastArrival
        let center = response.bbox.bbox
        visible = MarkerSelection.nearest(
            response.stations,
            toLatitude: (center.north + center.south) / 2,
            longitude: (center.east + center.west) / 2
        )
    }

    /// マーカーの色。**到着を選んでいれば確率の帯、選んでいなければ現在値。**
    ///
    /// 分からないもの（未観測・鮮度切れ・予測が出せない）はどちらの場合も灰色にして、
    /// 「現在値」としても「予測」としても読ませない。
    private func tint(for station: StationCurrent) -> Color {
        guard let arrival = forecastArrival else { return currentTint(for: station) }
        let state = ForecastState.make(
            station: station, feed: feeds[station.systemID], intent: model.intent,
            arrival: arrival)
        return state.band?.color ?? .gray
    }

    /// 現在値の色。借りられる／返せるだけを区別する。
    private func currentTint(for station: StationCurrent) -> Color {
        guard station.freshness(feed: feeds[station.systemID], now: now).isPresentable else {
            return .gray
        }
        switch (station.canRentNow, station.canReturnNow) {
        case (true?, true?): return .green
        case (true?, _): return .blue
        case (_, true?): return .orange
        default: return .red
        }
    }
}

extension ProbabilityBand {
    /// 地図のピンの色（開発プラン §9.3 の表）。**判断は `BikeChanceCore` にあり、ここは色だけ。**
    var color: Color {
        switch self {
        case .high: .green
        case .medium: .yellow
        case .low: .red
        }
    }

    /// 文字に使う色。**中を橙にする**のは、黄色の文字が白地の上で読めないため。
    /// ピンは色面なので黄でよく、文字は線なので同じ色では細くて消える。
    var textColor: Color {
        switch self {
        case .high: .green
        case .medium: .orange
        case .low: .red
        }
    }
}

extension Bbox {
    /// MapKit の表示領域から矩形を作る。
    init(region: MKCoordinateRegion) {
        self.init(
            west: region.center.longitude - region.span.longitudeDelta / 2,
            south: region.center.latitude - region.span.latitudeDelta / 2,
            east: region.center.longitude + region.span.longitudeDelta / 2,
            north: region.center.latitude + region.span.latitudeDelta / 2
        )
    }
}
