# GBFS のテスト用フィクスチャ

`packages/gbfs-core` と、W2 以降の Python 側が共有するテストデータ。

## 出どころ

**PR 0 が本番の Storage（`gbfs-raw`）に保存した生 JSON** から作っている。ODPT から
取り直すのではなく、実際に収集したものを使う。同じバイト列がテストにも本番にも流れる。

`2026-09-06/` は 2026-09-06 06:51〜06:57 UTC に収集したスナップショット。

## 縮約の方針

| フィード | 扱い | 理由 |
|---|---|---|
| `station_status` | **縮約しない** | 性能テスト（HELLO 14,835 ポートで 150 ms 未満）とゴールデンテストは実物の規模でないと意味がない。gzip 後 HELLO 91 KB・ドコモ 31 KB |
| `station_information` | 約 1,000 件に縮約 | HELLO は gzip 851 KB と大きい。ただし**データの癖を持つポートは必ず残す** |

## 残してある癖（縮約後も存在することをテストが確認する）

- ドコモ `station_status`：**完全重複の `station_id` が 11 件**（先頭を残す規則の検証に使う）
- ドコモ `station_information`：**日本の BBox 外の座標**（`station_id` 4826、経度 39.55）、
  **`capacity=0` が 230 件**、`capacity` の最大 9999
- HELLO `station_information`：**文字列の `vehicle_capacity`**（GBFS 2.3 では非標準）、
  `rental_uris`、`address`、充電ステーション

## 作り直し方

```bash
# 1. 本番 Storage から生 JSON を取る（{system}.{feed}.json という名前で置く）
# 2. 縮約する
node scripts/build-gbfs-fixtures.mjs <生JSONのディレクトリ> fixtures/gbfs/<取得日>
```

**作り直したらゴールデンテストのハッシュとポート数の期待値が変わる。** 実データは日々
変わるので、期待値はフィクスチャから導出せず、測り直してテストに書く。
