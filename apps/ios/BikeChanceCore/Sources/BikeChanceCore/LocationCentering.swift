/// 現在地へ動かすまでの段取り（W7 プランの W7-06、W6 プランの所見 224）。
///
/// **現在地が実際に届いてから動かす。** MapKit の `.userLocation(fallback:)` は、地図が現在地を
/// まだ知らないうちに置くと代わりの位置に留まり、あとで現在地が分かっても動かない
/// （10/17 のシミュレータ。許可した直後と起動直後がそれにあたる）。そこで、押したら
/// 現在地を 1 度だけ取りに行き、**届いた座標を動かす先にする**。
///
/// CoreLocation に依存しないので、順序の判断を検査で確かめられる（取りに行くのはアプリ側の
/// `LocationAuthorizer`）。
public struct LocationCentering: Sendable, Equatable {
    public enum Phase: Sendable, Equatable {
        case idle
        /// 許可を求めて、答えを待っている
        case awaitingAnswer
        /// 現在地を取りに行き、届くのを待っている
        case awaitingFix
    }

    public private(set) var phase: Phase = .idle
    /// 地図を動かす先。**届くたびに番号が進む**（同じ場所でも、押せば動かし直す）
    public private(set) var target: CenterTarget?

    public init() {}

    /// 現在地を待っているか（ボタンに「探している」ことを出す）
    public var isLocating: Bool { phase == .awaitingFix }

    /// 押した。すること（`LocationButtonAction.onTap`）を返し、何を待つかを覚える。
    public mutating func tapped(_ authorization: LocationAuthorization) -> LocationButtonAction {
        let action = LocationButtonAction.onTap(authorization)
        switch action {
        case .requestPermission: phase = .awaitingAnswer
        case .centerOnUser: phase = .awaitingFix
        case .explainSettings, .explainRestricted: phase = .idle
        }
        return action
    }

    /// 許可の答えが来た。**現在地を取りに行くべきとき** true。
    ///
    /// 未決定のままの知らせ（代理を付けた直後など）は、まだ答えではないので待ち続ける。
    /// 待っていないときの知らせ（設定から戻った・起動時の知らせ）では、何も変えない。
    public mutating func answered(_ answer: LocationAuthorization) -> Bool {
        guard phase == .awaitingAnswer, answer != .notDetermined else { return false }
        let locates = LocationButtonAction.centersAfterAnswer(awaiting: true, answer: answer)
        phase = locates ? .awaitingFix : .idle
        return locates
    }

    /// 現在地が届いた。**待っていたときだけ**、動かす先にする。
    public mutating func received(latitude: Double, longitude: Double) {
        guard phase == .awaitingFix else { return }
        phase = .idle
        let sequence = (target?.sequence ?? 0) + 1
        target = CenterTarget(sequence: sequence, latitude: latitude, longitude: longitude)
    }

    /// 取れなかった。**待つのをやめる**（回り続けない）。
    public mutating func failed() {
        if phase == .awaitingFix { phase = .idle }
    }
}

/// 地図を動かす先。`sequence` は届いた順の番号で、同じ座標でも別の合図として区別する。
public struct CenterTarget: Sendable, Equatable {
    public let sequence: Int
    public let latitude: Double
    public let longitude: Double
}
