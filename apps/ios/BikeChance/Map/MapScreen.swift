import BikeChanceCore
import MapKit
import SwiftUI

/// ホーム。現在地周辺のポートを地図に出す。
///
/// **W2 の範囲は実測値だけ**（予測は W4）。値には必ず観測時刻を添え、フィードが滞って
/// いれば「現在値」として出さない（CLAUDE.md §2 の 7・8）。
///
/// 位置情報の許可が無くても使える。初回は東京駅を中心に置き、あとは地図の操作で完結する
/// （App Store 審査ガイドライン 5.1.2、開発プラン §9.2）。
struct MapScreen: View {
    @State var model: StationsModel
    @State private var camera: MapCameraPosition = .region(MapScreen.initialRegion)
    @State private var visible: [StationCurrent] = []
    @State private var feeds: [String: FeedStatus] = [:]
    @State private var selected: StationCurrent?
    @State private var showsCredits = false
    @State private var now = Date()

    /// 東京駅まわり。**約 0.02 度四方**で、件数の上限にも表示上限にも余裕がある。
    static let initialRegion = MKCoordinateRegion(
        center: CLLocationCoordinate2D(latitude: 35.681236, longitude: 139.767125),
        span: MKCoordinateSpan(latitudeDelta: 0.02, longitudeDelta: 0.02)
    )

    /// 「N 分前の観測」を進めるための刻み。**Combine を持ち込まない**ために
    /// `Task.sleep` で回す（`.task` は画面が消えれば自動で止まる）。
    private static let tickSeconds = 30

    var body: some View {
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
        .safeAreaInset(edge: .top) { StatusBanner(state: model.state, now: now) }
        .safeAreaInset(edge: .bottom) { CreditFooter(showsCredits: $showsCredits) }
        .sheet(item: $selected) { station in
            // 選んだあとに再取得が走ることがあるので、**表示は最新の行から引き直す**
            let latest = visible.first { $0.id == station.id } ?? station
            StationSheet(station: latest, feed: feeds[latest.systemID], now: now)
                .presentationDetents([.height(260)])
        }
        .sheet(isPresented: $showsCredits) { CreditsScreen() }
        .onChange(of: model.state) { _, state in apply(state) }
        .task {
            model.viewportChanged(to: Bbox(region: MapScreen.initialRegion))
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(Self.tickSeconds))
                now = Date()
            }
        }
    }

    /// 応答から画面の状態を作る。**表示上限を超えたら中心に近い順に絞る。**
    private func apply(_ state: StationsModel.State) {
        guard case .loaded(let response) = state else {
            if case .needsZoom = state { visible = [] }
            return
        }
        feeds = response.feedIndex()
        let center = response.bbox.bbox
        visible = MarkerSelection.nearest(
            response.stations,
            toLatitude: (center.north + center.south) / 2,
            longitude: (center.east + center.west) / 2
        )
    }

    /// マーカーの色。**予測の色ではない**（W4 まで確率は出さない）。
    ///
    /// 借りられる／返せるだけを区別する。分からないもの（未観測・鮮度切れ）は灰色にして、
    /// 「現在値」として読ませない。
    private func tint(for station: StationCurrent) -> Color {
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
