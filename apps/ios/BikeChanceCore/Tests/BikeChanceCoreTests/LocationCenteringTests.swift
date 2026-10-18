import Testing

@testable import BikeChanceCore

/// 現在地へ動かすまでの段取り（`LocationCentering.swift`、W7 プランの W7-06）。
///
/// **主題は「現在地が届いてから動かす」こと。** 許可した直後に `.userLocation(fallback:)` を
/// 置くと、地図はまだ現在地を知らず、代わりの位置に留まった（10/17 のシミュレータ）。
/// 押して → 許して → **届いた座標へ** 1 度だけ動く、を固定する。
@Suite("現在地へ動かす段取り")
struct LocationCenteringTests {
    private static let yokohama = (latitude: 35.4660, longitude: 139.6223)

    @Test("**押して許したら、現在地が届いてから動かす**（届く前には動かさない）")
    func grantThenFix() {
        var centering = LocationCentering()
        let action = centering.tapped(.notDetermined)
        // 代理を付けた直後の「未決定」の知らせは、まだ答えではない
        let locatesOnUndecided = centering.answered(.notDetermined)
        let locatesOnGranted = centering.answered(.authorized)

        #expect(action == .requestPermission)
        #expect(!locatesOnUndecided)
        #expect(locatesOnGranted)
        #expect(centering.target == nil)
        #expect(centering.isLocating)

        centering.received(latitude: Self.yokohama.latitude, longitude: Self.yokohama.longitude)

        #expect(
            centering.target
                == CenterTarget(
                    sequence: 1, latitude: Self.yokohama.latitude,
                    longitude: Self.yokohama.longitude))
        #expect(!centering.isLocating)
    }

    @Test("許可済みで押せば、届いた現在地へ動かす")
    func authorizedTap() {
        var centering = LocationCentering()
        let action = centering.tapped(.authorized)

        #expect(action == .centerOnUser)
        #expect(centering.isLocating)

        centering.received(latitude: 35.0, longitude: 139.0)

        #expect(centering.target?.sequence == 1)
    }

    @Test("**押した直後の、起動時の許可の知らせでは待つのをやめない**（10/17 のシミュレータの順序）")
    func launchNoticeAfterTap() {
        var centering = LocationCentering()
        _ = centering.tapped(.authorized)
        // 待っているのは答えではなく現在地。知らせで取りに行き直さない
        let locates = centering.answered(.authorized)

        #expect(!locates)
        #expect(centering.isLocating)

        centering.received(latitude: 35.0, longitude: 139.0)

        #expect(centering.target?.sequence == 1)
    }

    @Test("1 回押したら 1 回だけ動かす（2 つ目の現在地では動かさない）")
    func oneTapOneMove() {
        var centering = LocationCentering()
        _ = centering.tapped(.authorized)
        centering.received(latitude: 35.0, longitude: 139.0)
        centering.received(latitude: 36.0, longitude: 140.0)

        #expect(centering.target == CenterTarget(sequence: 1, latitude: 35.0, longitude: 139.0))
    }

    @Test("もう 1 度押せば、同じ場所でも動かし直す（番号が進む）")
    func tapAgainMovesAgain() {
        var centering = LocationCentering()
        _ = centering.tapped(.authorized)
        centering.received(latitude: 35.0, longitude: 139.0)
        let first = centering.target
        _ = centering.tapped(.authorized)
        centering.received(latitude: 35.0, longitude: 139.0)

        #expect(centering.target?.sequence == 2)
        #expect(centering.target != first)
    }

    @Test("**取れなかったら待つのをやめる**（回り続けない。所見 224）")
    func failureStopsWaiting() {
        var centering = LocationCentering()
        _ = centering.tapped(.authorized)
        centering.failed()

        #expect(!centering.isLocating)
        centering.received(latitude: 35.0, longitude: 139.0)
        #expect(centering.target == nil)
    }

    @Test(
        "断られた・制限されたら、取りに行かず待たない",
        arguments: [LocationAuthorization.denied, .restricted]
    )
    func declined(answer: LocationAuthorization) {
        var centering = LocationCentering()
        _ = centering.tapped(.notDetermined)
        let locates = centering.answered(answer)

        #expect(!locates)
        #expect(centering.phase == .idle)
        centering.received(latitude: 35.0, longitude: 139.0)
        #expect(centering.target == nil)
    }

    @Test("押していないときは動かさない：設定で許可して戻った・現在地が届いた")
    func untouched() {
        var centering = LocationCentering()
        let locates = centering.answered(.authorized)
        centering.received(latitude: 35.0, longitude: 139.0)

        #expect(!locates)
        #expect(centering.target == nil)
        #expect(centering.phase == .idle)
    }

    @Test(
        "設定へ案内するときは、何も待たない",
        arguments: [LocationAuthorization.denied, .restricted]
    )
    func explainsWithoutWaiting(authorization: LocationAuthorization) {
        var centering = LocationCentering()
        _ = centering.tapped(authorization)

        #expect(centering.phase == .idle)
    }
}
