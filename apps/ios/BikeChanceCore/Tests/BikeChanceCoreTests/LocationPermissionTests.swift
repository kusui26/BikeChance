import Testing

@testable import BikeChanceCore

/// 位置情報の許可（`LocationPermission.swift`、W7 プランの W7-06、W6 プランの所見 224）。
///
/// **主題は「押したときにだけ許可を求め、答えに沿って動く」こと。** 端末での確認（10/16）で、
/// 現在地のボタンを押しても許可を求める画面が出ず、回り続けた——アプリに許可を求める
/// 処理が無かった。どの状態で押しても、何かが起きる（回り続けない）ことを固定する。
@Suite("位置情報の許可")
struct LocationPermissionTests {
    @Test(
        "押したときにすることは、許可の状態で 1 つに決まる",
        arguments: [
            (LocationAuthorization.notDetermined, LocationButtonAction.requestPermission),
            (.authorized, .centerOnUser),
            (.denied, .explainSettings),
            (.restricted, .explainRestricted),
        ]
    )
    func tapDecides(authorization: LocationAuthorization, expected: LocationButtonAction) {
        #expect(LocationButtonAction.onTap(authorization) == expected)
    }

    @Test("**未決定で押せば許可を求める**（所見 224：求めずに回り続けた）")
    func undecidedAsks() {
        #expect(LocationButtonAction.onTap(.notDetermined) == .requestPermission)
    }

    @Test("現在地の印は、許されているときだけ出す")
    func showsUserLocationOnlyWhenAuthorized() {
        #expect(LocationAuthorization.authorized.showsUserLocation)
        #expect(!LocationAuthorization.notDetermined.showsUserLocation)
        #expect(!LocationAuthorization.denied.showsUserLocation)
        #expect(!LocationAuthorization.restricted.showsUserLocation)
    }

    @Test("押して許してもらえたら、続けて現在地へ動く（もう 1 度押させない）")
    func centersAfterGrantedAnswer() {
        #expect(LocationButtonAction.centersAfterAnswer(awaiting: true, answer: .authorized))
    }

    @Test(
        "動かないとき：断られた・制限されている・押していないのに許可が変わった",
        arguments: [
            (true, LocationAuthorization.denied),
            (true, .restricted),
            (true, .notDetermined),
            (false, .authorized),
        ]
    )
    func staysPut(awaiting: Bool, answer: LocationAuthorization) {
        #expect(!LocationButtonAction.centersAfterAnswer(awaiting: awaiting, answer: answer))
    }
}
