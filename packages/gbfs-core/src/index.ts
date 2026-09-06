/**
 * GBFS の検証・正規化・取り込み引数への変換（W1 プラン §6.4）。
 *
 * すべて純粋関数で、I/O を持たない。収集器（PR D）とバックアップ収集器（W3、Deno）が
 * 同じ実装を共有し、フィクスチャで完全にテストできる状態を保つ。
 */
export * from "./schemas";
export * from "./normalize";
export * from "./build-args";
