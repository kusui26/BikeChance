/**
 * GBFS の検証・正規化・取り込み引数への変換（W1 プラン §6.4）。
 *
 * **I/O を持たない。** 収集器（PR D）とバックアップ収集器（W3、Deno）が同じ実装を
 * 共有し、フィクスチャで完全にテストできる状態を保つ。
 *
 * `rebuild-check` だけは非同期の関数を持つが、**読む口を引数で受け取る**（`SnapshotSource`）
 * ので、このパッケージ自身は `fetch` も `fs` も知らない。生 JSON からの再構築が本当に
 * 同じ行を作るかを、フィクスチャで端から端まで確かめられる（W4 プラン §6.8 の PR M）。
 */
export * from "./schemas";
export * from "./normalize";
export * from "./station-attributes";
export * from "./build-args";
export * from "./rebuild-check";
