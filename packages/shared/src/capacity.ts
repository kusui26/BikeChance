/**
 * ポートの大きさについての判定（W7 プランの W7-05）。
 *
 * **判定はサーバーの 1 か所に置く**——iOS と Web（W10）で 2 つ持たない（W5-01 と同じ理由）。
 * `/v1` は数（`capacity`・`docks`・`capacity_est`）を消さずに返し、この判定の答えを
 * `capacity_unbounded` として添える。画面は真のとき、数の代わりに「上限なし」と出す。
 */
import { UNBOUNDED_CAPACITY_MIN } from "./constants";

/** 判定に使う 3 つの数。どれも「分からない」は null。 */
export type CapacityFigures = {
  /** 事業者が宣言したラック数（動的な系統では null）。 */
  readonly capacity: number | null;
  /** いまの空き枠。 */
  readonly docks: number | null;
  /** 前日までの 7 日の `max(bikes + docks)`。 */
  readonly capacity_est: number | null;
};

/**
 * **上限なしか**：宣言の容量・空き枠・推定容量のどれかが番兵の大きさ
 * （`UNBOUNDED_CAPACITY_MIN` 以上）。
 *
 * 3 つとも見るのは、**拾える数が系統で違う**ため：HELLO の 26 ポートは宣言の容量（空き枠は
 * 945〜999 で届かない）、ドコモの 4161 は空き枠、5753 は推定容量でしか拾えない（ドコモは
 * 動的な系統なので、`/v1` の `capacity` が null）。
 */
export const isCapacityUnbounded = (figures: CapacityFigures): boolean =>
  [figures.capacity, figures.docks, figures.capacity_est].some(
    (value) => value !== null && value >= UNBOUNDED_CAPACITY_MIN,
  );
