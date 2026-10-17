/// 位置情報の許可の状態（W7 プランの W7-06、W6 プランの所見 224）。
///
/// `CLAuthorizationStatus` を、**アプリが判断に使う 4 つ**に畳んだもの。CoreLocation に
/// 依存しないので、判断を検査で確かめられる（畳むのはアプリ側の `LocationAuthorizer`）。
public enum LocationAuthorization: Sendable, Equatable {
    /// まだ聞いていない
    case notDetermined
    /// 使用中は許可（常に許可も含む。アプリは常時の利用を求めない）
    case authorized
    /// 断られた。**設定で変えられる**
    case denied
    /// 制限されている（ペアレンタルコントロールなど）。**利用者も設定で変えられない**
    case restricted

    /// 地図に現在地を出してよいか。**許されているときだけ**（出せないときに印だけ出さない）
    public var showsUserLocation: Bool { self == .authorized }
}

/// 現在地のボタンを押したときにすること。
///
/// **許可は押したときにだけ求める**——起動時には求めない。位置情報が無くても、地図を
/// 動かしたり目的地を探したりして使えるので（開発プラン §9.2、App Store 審査ガイドライン
/// 5.1.2）、要るときに求める（Apple の HIG）。システムの `MapUserLocationButton` は
/// 自分では許可を求めず、未決定のまま回り続けた（所見 224）。
public enum LocationButtonAction: Sendable, Equatable {
    /// 許可を求める（未決定のとき）
    case requestPermission
    /// 地図を現在地へ動かす。**現在地を 1 度取ってから動かす**（`LocationCentering`）
    case centerOnUser
    /// 設定で許可できることを案内する（断られている）
    case explainSettings
    /// 位置情報を使えないことを伝える（制限されていて、設定でも変えられない）
    case explainRestricted

    /// 押したときにすること。
    public static func onTap(_ authorization: LocationAuthorization) -> LocationButtonAction {
        switch authorization {
        case .notDetermined: .requestPermission
        case .authorized: .centerOnUser
        case .denied: .explainSettings
        case .restricted: .explainRestricted
        }
    }

    /// 許可の答えが来たとき、**続けて現在地へ動かすか**（現在地を取りに行くか）。
    ///
    /// 利用者は「現在地へ行きたい」と押して許可の画面に答えたので、許されたら続けて
    /// 動かす（もう 1 度押させない）。押していないのに許可が変わったとき（設定から戻った
    /// など）は、勝手に地図を動かさない。順序の全体は `LocationCentering` が持つ。
    public static func centersAfterAnswer(awaiting: Bool, answer: LocationAuthorization) -> Bool {
        awaiting && answer == .authorized
    }
}
