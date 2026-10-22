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
| **矩形を 0.01 度の格子に丸めてから送る** | 細かい位置を送らない（CLAUDE.md §5）。近い要求が同じ URL になりキャッシュが効く。**丸めた範囲は「おおよその位置」としてプライバシーラベルに申告する**（Vercel のログに最長 30 日残るため。開発プランの D-43） | `Bbox.quantized()` |
| **位置の許可は、現在地のボタンを押したときにだけ求める** | 起動時には求めない（許可が無くても使える。開発プラン §9.2）。システムの `MapUserLocationButton` は自分では許可を求めず、未決定のまま回り続けた（W6 プランの所見 224）。断られていたら設定への案内を出し、許可の印（`UserAnnotation`）は許されているときだけ出す | `LocationButtonAction`（Core）・`LocationAuthorizer`・`MapScreen.locationButton` |
| **現在地へは、現在地が届いてから動かす** | 押したら（許したら）`requestLocation()` で 1 度だけ取り、届いた座標へいつもの縮尺で動かす。**`.userLocation(fallback:)` は使わない**——地図が現在地をまだ知らないうちに置くと代わりの位置に留まり、あとで分かっても動かない（許可した直後・起動直後。W7 プランの所見 226） | `LocationCentering`（Core）・`LocationAuthorizer`・`MapScreen.center(on:)` |
| **丸めた後の長辺が 0.08 度を超えたら問い合わせない** | サーバーはそれより広い矩形に、ポートではなく格子のセル（`aggregation: cell`）を返す。**同じ値をサーバーと iOS の両方の検査が書く**（W6 の契約 41）。**比べるのは格子の数**——度の引き算は端数でずれる（139.84 − 139.76 ＝ 0.0800000000000125。W6 プランの所見 205） | `Bbox.requestMaxSpanDegrees`・`isRequestable` |
| **応答の粒度を先に読む** | セル応答には `stations` が無い。そのまま読むと失敗の帯になっていた（W6 プランの所見 194）。**`aggregation` が無い（古い）応答はポート、知らない値はポートとして読まない** | `StationsAggregation`・`V1Error.aggregated` |
| **「多すぎる」「セルが返った」は失敗ではなく案内** | 地図を拡大すれば解決する。エラーとして出さない | `StationsModel.State.needsZoom` |
| **表示は 300 マーカーまで、中心に近い順** | 描画の負荷（開発プラン §9.1）。ID 順で捨てると再描画のたびに違うポートが消える | `MarkerSelection` |
| **未観測は `—`。0 と区別する** | `-1`（未観測）は 0 台とは違う（データ辞書 §2 の (3)） | `StationDetail.unknownValue`（`StationDetailScreen`） |
| **鮮度が切れた値は「現在値」として出さない** | ODPT ガイドライン 2.1、CLAUDE.md §2 の 8。灰色にして観測時刻を添える | `Freshness` |
| **クレジットを常時出す** | CC BY 4.0 と ODPT ガイドライン 3.1。文言は `/v1/meta` から取り、アプリに焼き込まない。**データを出す画面から見えること**で確かめる——お気に入りはシートで開き、地図の下の 1 行が隠れるので、シートにも行の応答の前置きと `attribution` を出し、「データについて」へ行けるようにする（W7-08、W6 プランの所見 219） | `CreditFooter` / `CreditsScreen` / `FavoritesCredits`（`FavoritesScreen`） |
| **取れなくてもクレジットを出す** | 表示義務は通信に依存しない。最後に取れた `/v1/meta` をバイトのまま取っておき、**サーバーが作った時刻つきで**出す（W6 の契約 42）。**起動時に 1 度取って取っておく**ので、「データについて」を 1 度も開かずに通信できなくなった端末でも空にならない（中身はアプリに 1 つ。W7-08）。「いつの内容か」はファイルの日時から作らない（読むとプライバシーマニフェストに理由の申告が要る） | `CreditsModel`・`FileMetaCache`・`BikeChanceApp` |
| **予測値には予測時刻、実測値には観測時刻を添える** | CLAUDE.md §2 の 7（W6 の契約 43）。文言は 1 か所で作る | `ForecastDisplay.basisText`・`StationCurrent.currentLine` |

## 契約テスト

`BikeChanceCore/Tests/BikeChanceCoreTests/Fixtures/` は **本番の `/v1` が実際に返した応答**
（書き換えない）。サーバー側のスキーマを変えたらここが落ちて気づける。

- `stations*.json`（5 つ。2026-09-09〜13）… `/v1/stations` のポート応答。**`aggregation` を足す前**の形
- `stations_aggregation.json`（2026-09-27）… ポート応答（`aggregation: station`）。**前置きを足す前**の形
- `stations_preface.json`（2026-10-14）… いまのポート応答（`139.76,35.68,139.77,35.69`、9 ポート）。**クレジットの前置き `attribution_preface` つき**（W6 の PR J）
- `stations_cells.json`（2026-09-27）… **セル応答**（`139.76,35.67,139.85,35.70`、9 格子。`stations` が無い）
- `station_detail*.json`（2026-09-13）… ポート詳細（予測の曲線あり・なし）
- `trip_check*.json`（2026-09-13）… 行程チェック
- `meta.json`（2026-09-09）… `/v1/meta`。**前置きと出典を足す前**の形（いまの iOS も読めることを確かめる）
- `meta_sources.json`（2026-10-14）… いまの `/v1/meta`（前置き `attribution_preface` と、ODPT 以外の出典 `sources` つき。W6 の PR J）
- `problem_*.json` … RFC 9457 のエラー

`Bbox` の丸めがサーバーと一致していることも、この応答の `bbox`（サーバーが返す実効矩形）と
突き合わせて確かめている。

## TestFlight に上げる（W6 の PR K）

配布に要る 2 つは、リポジトリに入っている。CI（`ios.yml`）が、ビルドした `.app` に
両方が入っていることを毎回確かめる。

| もの | どこ | 中身 |
|---|---|---|
| プライバシーマニフェスト | `BikeChance/PrivacyInfo.xcprivacy` | **追跡しない・集めるのは「おおよその位置」だけ（ユーザーに連結しない・目的はアプリの機能）・理由の要る API を使わない**（W6-33、W7-07、開発プランの D-43）。CI（`ios.yml`）が `.app` の中身を確かめる。同期グループなので、置くだけで `.app` の直下に入る。**UserDefaults を使い始めたら、理由 `CA92.1` を足す** |
| 暗号化の申告 | `project.pbxproj` の `INFOPLIST_KEY_ITSAppUsesNonExemptEncryption = NO` | 使う暗号は `URLSession` の HTTPS だけで、輸出規制の対象外。**これがあると、アップロードのたびに輸出コンプライアンスの質問票が出ない** |

### 最初に 1 回だけ（利用者の作業。W6 プラン §7.4）

1. **アイコン**：1024 × 1024 の PNG（透過なし）を Xcode の Assets の AppIcon に入れる
   （`Assets.xcassets/AppIcon.appiconset/` に置き、`Contents.json` の項目に `"filename"` を足すのと同じ）。
   **無いと、アップロードの検証で止まる**
2. **署名のチーム**：Xcode でターゲット BikeChance → Signing & Capabilities → Team を選ぶ
   （Automatically manage signing は入れたまま）。`project.pbxproj` に `DEVELOPMENT_TEAM` が
   書き込まれる。チーム ID は秘密ではない（配った `.app` の署名に載る）ので、コミットしてよい
3. **App Store Connect のアプリ**：Apps → ＋ → New App で、iOS・Bundle ID
   `app.bikechance.BikeChance` のアプリを作る。作っていなければ、最初のアップロードのときに
   Xcode が必要な情報を聞いて作る

### 毎回

1. **Archive**：実行先を **Any iOS Device (arm64)** にして Product → Archive。
   終わると Organizer（Window → Organizer → Archives）に並ぶ。**Product メニューの「Archive」のすぐ下の
   「Xcode Cloud」を選ばない**——Xcode Cloud（Apple のクラウドのビルド）を始める道で、
   「Grant Access to Your Source Code」（GitHub への権限）の画面に進む（2026-10-22 の M5 で一度入った。使わない）
2. **確かめる**（任意）：Archive を右クリック（Control クリック）→ **Generate Privacy Report** で、
   集めるデータが**おおよその位置（Coarse Location）だけ**（ユーザーに連結しない・トラッキングに使わない・
   目的はアプリの機能）であることを見る。App Store Connect のプライバシーの回答と同じ中身になる
   （W7-07、開発プランの D-43）。**Validate App** で、上げる前の自動の検証だけを先に通せる
3. **アップロード**：**Distribute App** → **App Store Connect** → Distribute。いまの Xcode では、以前の
   「TestFlight & App Store」がこの名前になっている（内部・外部のテストにも申請にも使えるビルド。2026-10-22 の M5）。
   この選択肢は**ビルド番号を Xcode が付け直す**ので、リポジトリの `CURRENT_PROJECT_VERSION`（1）を
   毎回上げなくてよい（同じバージョン `MARKETING_VERSION`（0.1）の中で、アップロードごとに違う番号が要る）。
   チームの中だけで試し、App Store へは出さないと決めた回は **TestFlight Internal Only** を選ぶ
   （その回のビルドは申請に使えない）
4. **入れる**：処理が終わると App Store Connect の TestFlight に出る。Internal Testing の
   グループ（**自動配布に印を付けてある**。2026-10-21）に届くので、iPhone の TestFlight アプリから入れる

手順の元は Apple の「Distributing your app for beta testing and releases」。メニューや
選択肢の名前は、Xcode の版で変わることがある。

## まだ無いもの

Widget、**低ズームのセル表示**（いまはセルが返ったら拡大の案内を出すだけ。W6-30）、
位置情報の常時利用。申請に要るもののうち、**アイコンの画像・署名のチーム・App Store Connect の
アプリ**は利用者の作業（上の「TestFlight に上げる」。W6 プラン §7.4）。
