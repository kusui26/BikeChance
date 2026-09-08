# apps/ios — BikeChance（iPhone）

W2 の段 8 で作った雛形。**`/v1/stations` を叩いて地図にポートを出すところまで。**
予測はまだ無いので、出すのは実測値だけで、必ず観測時刻を添える（CLAUDE.md §2 の 7・8）。

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
| **1 辺 0.1 度を超えたら問い合わせない** | 実測で 0.4 度四方の東京は 6,649 件で、件数の上限 1,000 件を超える。**400 を貰いに行かない** | `Bbox.requestMaxSpanDegrees` |
| **「多すぎる」は失敗ではなく案内** | 地図を拡大すれば解決する。エラーとして出さない | `StationsModel.State.needsZoom` |
| **表示は 300 マーカーまで、中心に近い順** | 描画の負荷（開発プラン §9.1）。ID 順で捨てると再描画のたびに違うポートが消える | `MarkerSelection` |
| **未観測は `—`。0 と区別する** | `-1`（未観測）は 0 台とは違う（データ辞書 §2 の (3)） | `StationSheet` |
| **鮮度が切れた値は「現在値」として出さない** | ODPT ガイドライン 2.1、CLAUDE.md §2 の 8。灰色にして観測時刻を添える | `Freshness` |
| **クレジットを常時出す** | CC BY 4.0 と ODPT ガイドライン 3.1。文言は `/v1/meta` から取り、アプリに焼き込まない | `CreditFooter` / `CreditsScreen` |

## 契約テスト

`BikeChanceCore/Tests/BikeChanceCoreTests/Fixtures/` は **2026-09-08 に本番の `/v1` が実際に
返した応答**。サーバー側のスキーマを変えたらここが落ちて気づける。

- `stations.json` … `/v1/stations` の正常系
- `meta.json` … `/v1/meta`
- `problem_bbox_missing.json` / `problem_too_many.json` … RFC 9457 のエラー

`Bbox` の丸めがサーバーと一致していることも、この応答の `bbox`（サーバーが返す実効矩形）と
突き合わせて確かめている。

## まだ無いもの

予測（`p_bike` / `p_dock`）、ポート詳細、行程チェック、お気に入り、Widget、検索、
低ズームのグリッド集約、位置情報の常時利用。いずれも W4 以降（開発プラン §9.2）。
