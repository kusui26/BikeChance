import BikeChanceCore
import CoreLocation
import Observation

/// 位置情報の許可と、現在地を 1 度だけ取ること（W7 プランの W7-06、W6 プランの所見 224）。
///
/// **押したときにだけ許可を求める**——起動時には求めない（開発プラン §9.2）。押したら
/// 現在地を 1 度だけ取りに行き、**届いた座標を `target` に出す**。画面は `target` の変化を見て
/// 地図を動かす。何をいつするかの判断は `BikeChanceCore` の `LocationCentering` にあり、
/// ここは CoreLocation とのやりとりだけを持つ。
@MainActor
@Observable
final class LocationAuthorizer: NSObject, CLLocationManagerDelegate {
    private(set) var authorization: LocationAuthorization
    private var centering = LocationCentering()
    @ObservationIgnored private let manager: CLLocationManager

    override init() {
        let manager = CLLocationManager()
        // 地図の中心を決めるだけなので、100 m で足りる（そのぶん早く届く）
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        self.manager = manager
        self.authorization = Self.fold(manager.authorizationStatus)
        super.init()
        manager.delegate = self
    }

    /// 地図を動かす先。**押して（許して）、現在地が届いたときだけ**新しくなる
    var target: CenterTarget? { centering.target }

    /// 現在地を待っているか（ボタンに出す）
    var isLocating: Bool { centering.isLocating }

    /// 押した。許可を求めるか、現在地を取りに行く。することを返す（案内は画面が出す）。
    func tap() -> LocationButtonAction {
        let action = centering.tapped(authorization)
        switch action {
        case .requestPermission: manager.requestWhenInUseAuthorization()
        case .centerOnUser: manager.requestLocation()
        case .explainSettings, .explainRestricted: break
        }
        return action
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor in self.receive(Self.fold(status)) }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]
    ) {
        guard let coordinate = locations.last?.coordinate else { return }
        let (latitude, longitude) = (coordinate.latitude, coordinate.longitude)
        Task { @MainActor in self.centering.received(latitude: latitude, longitude: longitude) }
    }

    /// `requestLocation()` は、取れなければここに来る（実装しないと例外になる）。
    nonisolated func locationManager(
        _ manager: CLLocationManager, didFailWithError error: any Error
    ) {
        Task { @MainActor in self.centering.failed() }
    }

    /// 答えを受け取る。**押して許してもらえたときだけ**、現在地を取りに行く。
    private func receive(_ answer: LocationAuthorization) {
        authorization = answer
        if centering.answered(answer) {
            manager.requestLocation()
        }
    }

    /// `CLAuthorizationStatus` を、アプリが判断に使う 4 つに畳む。
    nonisolated static func fold(_ status: CLAuthorizationStatus) -> LocationAuthorization {
        switch status {
        case .notDetermined: .notDetermined
        case .authorizedWhenInUse, .authorizedAlways: .authorized
        case .denied: .denied
        case .restricted: .restricted
        @unknown default: .denied
        }
    }
}
