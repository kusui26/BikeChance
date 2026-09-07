/**
 * 地図の矩形（bbox）の解釈（W2 プラン §5.7、開発プラン §8.3）。
 *
 * **純粋な関数だけを置く。** iOS が同じ規則で bbox を組み立てられるよう、ここが唯一の定義になる。
 *
 * 3 つのことをする。
 *   1. `west,south,east,north` の文字列を読む（壊れた入力は例外にせず結果で返す）
 *   2. 大きすぎる範囲を弾く。1 回の応答に 2 万件を載せない
 *   3. **格子に外側へ丸める**。理由は 2 つあって、どちらも重要：
 *      * プライバシー：サーバーは利用者の細かい位置を受け取っても**使わない**。
 *        丸めた矩形しか問い合わせに使わないので、細かさは記録にも残らない（CLAUDE.md §5）
 *      * CDN：同じ矩形は同じ URL になり、キャッシュが効く（開発プラン §8.3）
 */

export type Bbox = {
  readonly west: number;
  readonly south: number;
  readonly east: number;
  readonly north: number;
};

/**
 * 丸めの刻み（度）。東京では緯度 0.01° ≒ 1.11 km、経度 0.01° ≒ 0.90 km。
 * 「利用者は 1 km 四方のどこかに居る」までしかサーバーに伝わらない。
 */
export const BBOX_QUANTUM_DEG = 0.01;

/** 1 辺の上限（度）。緯度 0.5° ≒ 55 km。東京 23 区がひととおり入る大きさ。 */
export const BBOX_MAX_SPAN_DEG = 0.5;

/**
 * 1 回の応答に載せるポート数の上限。
 *
 * 実測（2026-09-08、本番）で 1 ポートあたりの JSON は平均 348 バイト。1,000 件で約 340 KB、
 * gzip 後は数十 KB に収まる。**上限を超えたら切り捨てずに 400 を返す**。地図に穴の開いた
 * 結果を黙って返すより、「範囲を狭めてください」と言うほうが正しい。
 * （低ズーム向けの格子集約は開発プラン §8.3 にあるが W2 では作らない）
 */
export const STATIONS_MAX_RESULTS = 1000;

/** 度を丸めるときの小数桁。浮動小数の端数を落として、同じ入力が同じ URL になるようにする。 */
const BBOX_DECIMALS = 6;

const LAT_MIN = -90;
const LAT_MAX = 90;
const LON_MIN = -180;
const LON_MAX = 180;

/** 想定どおりでない入力の種類。応答の Problem Details の型に対応する。 */
export const BBOX_PROBLEMS = [
  "missing",
  "malformed",
  "out_of_range",
  "inverted",
  "too_large",
] as const;
export type BboxProblem = (typeof BBOX_PROBLEMS)[number];

export type BboxParseResult =
  | { readonly ok: true; readonly bbox: Bbox }
  | { readonly ok: false; readonly problem: BboxProblem; readonly detail: string };

const failure = (problem: BboxProblem, detail: string): BboxParseResult => ({
  ok: false,
  problem,
  detail,
});

/** 分解代入は `undefined` も返し得る（`noUncheckedIndexedAccess`）。まとめて落とす型ガード。 */
const isNumber = (value: number | null | undefined): value is number =>
  value !== null && value !== undefined;

/** 有限の数だけを受け取る。`Number("")` は 0 になるので空文字は先に弾く。 */
const toFiniteNumber = (text: string): number | null => {
  const trimmed = text.trim();
  if (trimmed === "") {
    return null;
  }
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
};

export const bboxSpans = (bbox: Bbox): { readonly lat: number; readonly lon: number } => ({
  lat: bbox.north - bbox.south,
  lon: bbox.east - bbox.west,
});

/**
 * `west,south,east,north` を読む。**例外を投げない**（想定内の入力誤りだから）。
 *
 * 日付変更線をまたぐ矩形（west > east）は受け取らない。日本の範囲では起きず、
 * 受け取れるようにすると問い合わせが 2 本に割れて、上限の判定も曖昧になる。
 */
export const parseBbox = (text: string | null | undefined): BboxParseResult => {
  if (text === null || text === undefined || text.trim() === "") {
    return failure("missing", "bbox は必須です（west,south,east,north の順に度で指定）。");
  }
  const parts = text.split(",");
  if (parts.length !== 4) {
    return failure("malformed", `bbox は 4 つの数です（受け取った要素数 ${parts.length}）。`);
  }
  const [west, south, east, north] = parts.map(toFiniteNumber);
  if (!isNumber(west) || !isNumber(south) || !isNumber(east) || !isNumber(north)) {
    return failure("malformed", "bbox の要素は有限の数でなければなりません。");
  }

  if (south < LAT_MIN || north > LAT_MAX || west < LON_MIN || east > LON_MAX) {
    return failure("out_of_range", "緯度は -90〜90、経度は -180〜180 の範囲です。");
  }
  if (west >= east || south >= north) {
    return failure("inverted", "west < east かつ south < north でなければなりません。");
  }

  const bbox: Bbox = { west, south, east, north };
  const spans = bboxSpans(bbox);
  if (spans.lat > BBOX_MAX_SPAN_DEG || spans.lon > BBOX_MAX_SPAN_DEG) {
    return failure(
      "too_large",
      `1 辺は ${BBOX_MAX_SPAN_DEG} 度までです（緯度 ${spans.lat.toFixed(3)} / 経度 ${spans.lon.toFixed(3)} 度）。`,
    );
  }
  return { ok: true, bbox };
};

/** 量子の整数倍にそろえる。端数を先に落としてから丸め、同じ入力で同じ値になるようにする。 */
const snap = (value: number, quantum: number, toIndex: (n: number) => number): number => {
  const scaled = Number((value / quantum).toFixed(9));
  return Number((toIndex(scaled) * quantum).toFixed(BBOX_DECIMALS));
};

/**
 * 格子に**外側へ**丸める。要求された範囲は必ず結果の中に入る。
 *
 * 外側に広げるので、応答には要求範囲の外のポートも混じる。利用者側で絞り込む前提で、
 * 実効的な矩形は応答の `bbox` に入れて返す。
 */
export const quantizeBbox = (bbox: Bbox, quantum: number = BBOX_QUANTUM_DEG): Bbox => ({
  west: snap(bbox.west, quantum, Math.floor),
  south: snap(bbox.south, quantum, Math.floor),
  east: snap(bbox.east, quantum, Math.ceil),
  north: snap(bbox.north, quantum, Math.ceil),
});

/** 点が矩形の中にあるか（境界を含む）。 */
export const isInsideBbox = (bbox: Bbox, lat: number, lon: number): boolean =>
  lat >= bbox.south && lat <= bbox.north && lon >= bbox.west && lon <= bbox.east;

/** 応答やログに載せるときの表記。`parseBbox` が読み戻せる形にする。 */
export const formatBbox = (bbox: Bbox): string =>
  [bbox.west, bbox.south, bbox.east, bbox.north].join(",");
