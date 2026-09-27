# apps/ios — BikeChance（iPhone）

地図（ポートの現在値と、到着時刻の確率）・ポート詳細（予測の曲線）・行程チェック・
お気に入り・データについて（クレジット）の 5 つの画面。**`/v1` だけを叩く**（CLAUDE.md §2 の 3）。
実測値には観測時刻を、予測値には予測時刻を添える（CLAUDE.md §2 の 7。まだ欠けている画面は
W6 プランの所見 196 と PR J）。

## 構成

| 場所 | 中身 | どう検証するか |
|---|---|---|
| `BikeChanceCore/` | **Swift Package。純粋ロジックと `/v1` の型。** UIKit / SwiftUI / MapKit に依存しない | `swift test`（シミュレータ不要） |
| `BikeChance/` | SwiftUI の画面（`Map`・シート・クレジット） | `xcodebuild build` とシミュレータ |
| `BikeChance.xcodeproj` | アプリのターゲット 1 つだけ。**同期グループ**なので、ファイルを足しても `.pbxproj` を触らない | — |

**判断の入るものはすべて `BikeChanceCore` に置く。** 画面側は表示だけを持つ。状態機械
（`StationsModel`）もパッケージ側にあり、`swift test` で全分岐を通してある。

## 動かす

```bash
# 純粋ロジックのテスト（いちばん速い。ここが本体）
cd apps/ios/BikeChanceCore && swift test

# 書式（Xcode 同梱の swift-format。追加のツールは要らない）
cd apps/ios
xcrun swift-format lint --parallel --recursive --configuration .swift-format \
  BikeChanceCore/Sources BikeChanceCore/Tests BikeChance

# アプリのビルド
xcodebuild -project BikeChance.xcodeproj -scheme BikeChance \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build

# シミュレータで動かす
xcrun simctl boot "iPhone 17 Pro"
xcrun simctl install booted "$(xcodebuild -project BikeChance.xcodeproj -scheme BikeChance \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -showBuildSettings 2>/dev/null \
  | awk -F' = ' '/ BUILT_PRODUCTS_DIR/{d=$2} / FULL_PRODUCT_NAME/{n=$2} END{print d"/"n}')"
xcrun simctl launch booted app.bikechance.BikeChance
```

接続先は既定で本番の `/v1`。開発中はスキームの環境変数 `BIKECHANCE_API_BASE_URL` で
差し替えられる（**Info.plist には置かない**。配布物に開発用の設定を混ぜないため）。

## `/v1` の使い方で守っていること

| 約束 | なぜ | どこ |
|---|---|---|
| **`Authorization` を付けない** | 付けると CDN のキャッシュが効かない（開発プラン §8.3） | `V1Client` |
| **矩形を 0.01 度の格子に丸めてから送る** | 細かい位置を送らない（CLAUDE.md §5）。近い要求が同じ URL になりキャッシュが効く | `Bbox.quantized()` |
| **丸めた後の長辺が 0.08 度を超えたら問い合わせない** | サーバーはそれより広い矩形に、ポートではなく格子のセル（`aggregation: cell`）を返す。**同じ値をサーバーと iOS の両方の検査が書く**（W6 の契約 41）。**比べるのは格子の数**——度の引き算は端数でずれる（139.84 − 139.76 ＝ 0.0800000000000125。W6 プランの所見 205） | `Bbox.requestMaxSpanDegrees`・`isRequestable` |
| **応答の粒度を先に読む** | セル応答には `stations` が無い。そのまま読むと失敗の帯になっていた（W6 プランの所見 194）。**`aggregation` が無い（古い）応答はポート、知らない値はポートとして読まない** | `StationsAggregation`・`V1Error.aggregated` |
| **「多すぎる」「セルが返った」は失敗ではなく案内** | 地図を拡大すれば解決する。エラーとして出さない | `StationsModel.State.needsZoom` |
| **表示は 300 マーカーまで、中心に近い順** | 描画の負荷（開発プラン §9.1）。ID 順で捨てると再描画のたびに違うポートが消える | `MarkerSelection` |
| **未観測は `—`。0 と区別する** | `-1`（未観測）は 0 台とは違う（データ辞書 §2 の (3)） | `StationDetail.unknownValue`（`StationDetailScreen`） |
| **鮮度が切れた値は「現在値」として出さない** | ODPT ガイドライン 2.1、CLAUDE.md §2 の 8。灰色にして観測時刻を添える | `Freshness` |
| **クレジットを常時出す** | CC BY 4.0 と ODPT ガイドライン 3.1。文言は `/v1/meta` から取り、アプリに焼き込まない | `CreditFooter` / `CreditsScreen` |

## 契約テスト

`BikeChanceCore/Tests/BikeChanceCoreTests/Fixtures/` は **本番の `/v1` が実際に返した応答**
（書き換えない）。サーバー側のスキーマを変えたらここが落ちて気づける。

- `stations*.json`（5 つ。2026-09-09〜13）… `/v1/stations` のポート応答。**`aggregation` を足す前**の形
- `stations_aggregation.json`（2026-09-27）… いまのポート応答（`aggregation: station`）
- `stations_cells.json`（2026-09-27）… **セル応答**（`139.76,35.67,139.85,35.70`、9 格子。`stations` が無い）
- `station_detail*.json`（2026-09-13）… ポート詳細（予測の曲線あり・なし）
- `trip_check*.json`（2026-09-13）… 行程チェック
- `meta.json`（2026-09-09）… `/v1/meta`
- `problem_*.json` … RFC 9457 のエラー

`Bbox` の丸めがサーバーと一致していることも、この応答の `bbox`（サーバーが返す実効矩形）と
突き合わせて確かめている。

## まだ無いもの

Widget、**低ズームのセル表示**（いまはセルが返ったら拡大の案内を出すだけ。W6-30）、
位置情報の常時利用。申請に要るもの（`PrivacyInfo.xcprivacy`・アイコンの画像・署名のチーム）は
W6 の PR K（W6 プラン §2.7）。
