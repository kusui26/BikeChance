/**
 * 天気予報の生アーカイブ（W2 プラン §5.3、PR A）。
 *
 * **保存だけを行い、解釈は一切しない。** W1 の PR 0 と同じ役割で、テーブルも特徴量も
 * 待たずに「失われるもの」を先に止める。
 *
 * 学習で使うのは「`t` 時点で入手できた**予報**」で、これは過去に遡って観測できない
 * 唯一の入力である（開発プラン §6.7、R16）。実測で、最新の予報と 1 日前の予報は
 * 降雨フラグが 15% 食い違う。過去 API のどの近似にも同規模の誤差があり、**ライブの
 * アーカイブだけが厳密に正しい。1 日遅れれば 1 日分が永久に失われる。**
 *
 * 収集（`collect.ts`）と同じ規律に従う。
 *   * 受信バイト列をそのまま保存する。パースは地点数の確認にしか使わない
 *   * どの経路を通っても最後に `job_runs` へ記録する
 *   * 分割の 1 つが失敗しても、成功した分割は保存済みのまま残す
 *
 * **取り直しは同じ回の中だけで行う**（W7-01、W6 プランの所見 223）。発行の「使える時刻」は
 * この回が終わった時刻（`v_weather_files.available_at`）なので、別の回で取り直すと 1 つの
 * 発行に 2 つの時刻が混ざり、特徴量は遅いほうを採る——配信で使った発行を、学習では
 * 使えなくなる。**時間の上限でも止められない**：分割を投げる前に、投げて保存して記録まで
 * 収まるかを確かめ（`fitsArchiveBudget`）、収まらない分割は投げずに `failed` として残す。
 */
import {
  WEATHER_BATCH_SIZE,
  WEATHER_LOCATIONS_PER_MINUTE,
  WEATHER_RATE_WINDOW_MS,
  WEATHER_RETRY_BACKOFF_MS,
  batchCells,
  fitsArchiveBudget,
  pacingDelaysMs,
  truncateToHour,
  weatherObjectPath,
  type EpochSeconds,
} from "@bikechance/shared";
import { JobError, isJobError, toJobFailure, type JobFailure } from "./errors";
import type { RawUploader } from "./storage";
import { fetchWeatherBatch, type WeatherCell } from "./weather-fetch";
import type { WeatherPort } from "./weather-port";

const MS_PER_S = 1000;

/** 5xx はサーバー側の一時的な失敗として取り直す。**4xx は取り直さない**（429 は速さの枠の印）。 */
const HTTP_SERVER_ERROR_MIN = 500;

/** 取り直してよい失敗の種類：時間切れと通信の失敗（W7-01）。 */
const RETRYABLE_ERROR_NAMES: ReadonlySet<string> = new Set([
  "TimeoutError",
  "TypeError",
  "AbortError",
]);

const FIRST_ATTEMPT = 1;
const RETRY_ATTEMPT = 2;

/** 速さを抑えるための待ち。**ここだけが時計に触る**（決め方は `pacingDelaysMs`）。 */
const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

export const ARCHIVE_WEATHER_JOB_NAME = "archive_weather";

/** 1 分割の結果。失敗しても他の分割は続ける。 */
export type BatchOutcome = {
  readonly batch: number;
  readonly n_cells: number;
  readonly ok: boolean;
  readonly n_locations: number | null;
  readonly bytes: number | null;
  readonly gzip_bytes: number | null;
  readonly path: string | null;
  /** 既に同じパスがあった（同じ時間内の再実行）。正常系。 */
  readonly duplicate: boolean;
  readonly error: string | null;
  /** 取り直してよい失敗か（W7-01）。成功と、投げなかった分割は false。 */
  readonly retryable: boolean;
  /** 投げた回数。0 は時間の予算が足りず投げなかった、2 は取り直した。 */
  readonly attempts: number;
};

export type ArchiveWeatherSummary = {
  readonly ok: boolean;
  readonly n_cells: number;
  readonly n_batches: number;
  readonly n_saved: number;
  readonly n_duplicate: number;
  readonly n_failed: number;
  /** 取り直した分割の数と、そのうち埋まった数（W7-01）。 */
  readonly n_retried: number;
  readonly n_recovered: number;
  /** 時間の予算が足りず、最初の回で投げなかった分割の数。 */
  readonly n_skipped: number;
  readonly bytes: number;
  readonly gzip_bytes: number;
  readonly hour_epoch_s: EpochSeconds;
  readonly duration_ms: number;
  /** 速さを抑えるために待った合計（ミリ秒）。**枠に近づいたらここが伸びる。** */
  readonly waited_ms: number;
  readonly batches: readonly BatchOutcome[];
  readonly error: JobFailure | null;
};

export type ArchiveWeatherParams = {
  readonly db: WeatherPort;
  readonly upload: RawUploader;
  readonly gzip: (body: Uint8Array) => Promise<Uint8Array>;
  readonly contact_email: string;
  readonly now: Date;
  /**
   * 速さを抑えるための待ち。**省くと本物の時計で待つ。**
   *
   * 差し替えられるのは試験のためである——待ちは 1 分以上あり、**本当に待つ試験は
   * 書かれなくなる**。差し替えた側は「何ミリ秒で呼ばれたか」を見て、待ちが実際に
   * 挟まっていることを確かめる（`archive-weather.test.ts`）。
   */
  readonly sleep?: (ms: number) => Promise<void>;
  /**
   * 経った時間を測る時計（ミリ秒）。**省くと本物の時計。** 時間の予算（W7-01）を、
   * 試験で決まった値にして確かめるため。
   */
  readonly clock?: () => number;
};

/** 1 回の実行が持ち回るもの。**経った時間はここからしか測らない。** */
type Run = {
  readonly job: ArchiveWeatherParams;
  readonly epoch_s: EpochSeconds;
  readonly elapsed: () => number;
};

/** 投げる予定の 1 分割。 */
type Planned = {
  readonly batch: number;
  readonly cells: readonly WeatherCell[];
  readonly delay_ms: number;
};

const toEpochSeconds = (date: Date): EpochSeconds => Math.floor(date.getTime() / MS_PER_S);

/** 取り直してよい失敗か（W7-01）：取得の時間切れ・通信の失敗・5xx。 */
export const isRetryable = (failure: JobFailure): boolean =>
  failure.phase === "fetch" &&
  (RETRYABLE_ERROR_NAMES.has(failure.error_name) ||
    (failure.http_status !== null && failure.http_status >= HTTP_SERVER_ERROR_MIN));

const failedBatch = (params: {
  readonly batch: number;
  readonly n_cells: number;
  readonly error: string;
  readonly retryable: boolean;
  readonly attempts: number;
}): BatchOutcome => ({
  ...params,
  ok: false,
  n_locations: null,
  bytes: null,
  gzip_bytes: null,
  path: null,
  duplicate: false,
});

/** 時間の予算が足りず、投げなかった分割。**黙って欠けさせず、記録に残す。** */
const skippedBatch = (batch: number, n_cells: number): BatchOutcome =>
  failedBatch({
    batch,
    n_cells,
    error: "budget/TimeBudget: 上限の内側で投げ終えられないので投げなかった",
    retryable: false,
    attempts: 0,
  });

/** 受け取った応答を保存する。**地点の数が合わなければ保存しない。** */
const saveBatch = async (params: {
  readonly job: ArchiveWeatherParams;
  readonly cells: readonly WeatherCell[];
  readonly batch: number;
  readonly epoch_s: EpochSeconds;
  readonly attempt: number;
}): Promise<BatchOutcome> => {
  const { job, cells, batch, epoch_s, attempt } = params;
  const received = await fetchWeatherBatch({ cells, contact_email: job.contact_email });
  if (received.n_locations !== cells.length) {
    throw new JobError({
      phase: "parse",
      error_name: "LocationCountMismatch",
      http_status: null,
      message: `要求 ${cells.length} 地点に対し応答は ${received.n_locations} 地点だった`,
    });
  }
  const path = weatherObjectPath({ epoch_s, batch });
  const gzipped = await job.gzip(received.body);
  const { duplicate } = await job.upload({ path, gzipped });
  return {
    batch,
    n_cells: cells.length,
    ok: true,
    n_locations: received.n_locations,
    bytes: received.bytes,
    gzip_bytes: gzipped.byteLength,
    path,
    duplicate,
    error: null,
    retryable: false,
    attempts: attempt,
  };
};

/**
 * 1 分割を取得して保存する。**同じ時間内の再実行は同じパスに写像される**ので、
 * 既にあれば 409 が返り、それを正常系として扱う（§9.1）。
 */
const archiveBatch = async (params: Parameters<typeof saveBatch>[0]): Promise<BatchOutcome> => {
  try {
    return await saveBatch(params);
  } catch (cause) {
    const failure = isJobError(cause) ? cause.failure : toJobFailure({ phase: "unknown", cause });
    // 種別（error_name）を落とさない。message だけだと後から grep できない
    return failedBatch({
      batch: params.batch,
      n_cells: params.cells.length,
      error: `${failure.phase}/${failure.error_name}: ${failure.message}`,
      retryable: isRetryable(failure),
      attempts: params.attempt,
    });
  }
};

/** 予定の待ちを置いてから、1 分割を投げる。 */
const sendOne = async (run: Run, one: Planned, attempt: number): Promise<BatchOutcome> => {
  if (one.delay_ms > 0) {
    await (run.job.sleep ?? sleep)(one.delay_ms);
  }
  const { job, epoch_s } = run;
  return archiveBatch({ job, cells: one.cells, batch: one.batch, epoch_s, attempt });
};

/**
 * 予定の順に投げる。**投げる前に時間の予算を確かめ**、足りなければそこから先は投げない
 * （null）。**途中を飛ばして後ろを投げない**——飛ばした分割の待ちを前提に、後ろの分割の
 * 待ちが決まっているので、飛ばして投げると速さの枠を越える。
 *
 * 分割は 1 つずつ順に投げる（並べて投げると速さの枠を越える）ので、`await` の並びになる。
 */
const sendInOrder = async (
  run: Run,
  planned: readonly Planned[],
  attempt: number,
): Promise<readonly (BatchOutcome | null)[]> => {
  const outcomes: (BatchOutcome | null)[] = [];
  for (const one of planned) {
    const fits = !outcomes.includes(null) && fitsArchiveBudget(run.elapsed(), one.delay_ms);
    outcomes.push(fits ? await sendOne(run, one, attempt) : null);
  }
  return outcomes;
};

/** 最初の回の予定。待ちは分割の大きさの並びだけで決まる（`pacingDelaysMs`）。 */
const firstPlan = (batches: readonly (readonly WeatherCell[])[]): readonly Planned[] => {
  const delays = pacingDelaysMs(
    batches.map((cells) => cells.length),
    WEATHER_LOCATIONS_PER_MINUTE,
    WEATHER_RATE_WINDOW_MS,
  );
  return batches.map((cells, batch) => ({ batch, cells, delay_ms: delays[batch] ?? 0 }));
};

/**
 * 取り直しの予定（W7-01）。**速さの予算は最初の回と 1 列に並べて数える**（取り直しも同じ
 * 1 分の枠に入る）。最初の取り直しは、一時的な遅れをやり過ごすために少し待つ。
 */
const retryPlan = (
  batches: readonly (readonly WeatherCell[])[],
  first: readonly BatchOutcome[],
): readonly Planned[] => {
  const failed = first.filter((outcome) => outcome.retryable);
  const sizes = [...batches.map((cells) => cells.length), ...failed.map((one) => one.n_cells)];
  const delays = pacingDelaysMs(sizes, WEATHER_LOCATIONS_PER_MINUTE, WEATHER_RATE_WINDOW_MS).slice(
    batches.length,
  );
  return failed.map((one, index) => ({
    batch: one.batch,
    cells: batches[one.batch] ?? [],
    delay_ms:
      index === 0 ? Math.max(delays[index] ?? 0, WEATHER_RETRY_BACKOFF_MS) : (delays[index] ?? 0),
  }));
};

/** 取り直した分割は、その結果で置き換える。**投げなかった取り直しは、最初の失敗のまま。** */
const mergeRetries = (
  first: readonly BatchOutcome[],
  planned: readonly Planned[],
  retried: readonly (BatchOutcome | null)[],
): readonly BatchOutcome[] => {
  const byBatch = planned.reduce((found, one, index) => {
    const outcome = retried[index];
    return outcome ? new Map(found).set(one.batch, outcome) : found;
  }, new Map<number, BatchOutcome>());
  return first.map((outcome) => byBatch.get(outcome.batch) ?? outcome);
};

/** 実際に投げた分割の前の待ちだけを足す。 */
const waitedOf = (planned: readonly Planned[], sent: readonly (BatchOutcome | null)[]): number =>
  planned.reduce((total, one, index) => (sent[index] ? total + one.delay_ms : total), 0);

type Counts = Pick<ArchiveWeatherSummary, "n_retried" | "n_recovered" | "n_skipped" | "waited_ms">;

const NO_COUNTS: Counts = { n_retried: 0, n_recovered: 0, n_skipped: 0, waited_ms: 0 };

const summarize = (params: {
  readonly hour_epoch_s: EpochSeconds;
  readonly n_cells: number;
  readonly batches: readonly BatchOutcome[];
  readonly duration_ms: number;
  readonly counts: Counts;
  readonly error: JobFailure | null;
}): ArchiveWeatherSummary => {
  const { batches } = params;
  const n_failed = batches.filter((outcome) => !outcome.ok).length;
  return {
    // 分割が 1 つでも落ちたら失敗として扱う。次の時間に取り直せるが、
    // 「一部だけ欠けた時間」を成功と記録すると後から気づけない
    ok: params.error === null && n_failed === 0 && batches.length > 0,
    n_cells: params.n_cells,
    n_batches: batches.length,
    n_saved: batches.filter((outcome) => outcome.ok && !outcome.duplicate).length,
    n_duplicate: batches.filter((outcome) => outcome.duplicate).length,
    n_failed,
    ...params.counts,
    bytes: batches.reduce((total, outcome) => total + (outcome.bytes ?? 0), 0),
    gzip_bytes: batches.reduce((total, outcome) => total + (outcome.gzip_bytes ?? 0), 0),
    hour_epoch_s: params.hour_epoch_s,
    duration_ms: params.duration_ms,
    batches,
    error: params.error,
  };
};

/** `job_runs.detail` に入れる要約。分割ごとの失敗理由まで残す。 */
const toDetail = (summary: ArchiveWeatherSummary): Readonly<Record<string, unknown>> => ({
  n_cells: summary.n_cells,
  n_batches: summary.n_batches,
  n_saved: summary.n_saved,
  n_duplicate: summary.n_duplicate,
  n_failed: summary.n_failed,
  n_retried: summary.n_retried,
  n_recovered: summary.n_recovered,
  n_skipped: summary.n_skipped,
  bytes: summary.bytes,
  gzip_bytes: summary.gzip_bytes,
  hour_epoch_s: summary.hour_epoch_s,
  duration_ms: summary.duration_ms,
  waited_ms: summary.waited_ms,
  ...(summary.n_failed === 0
    ? {}
    : { failures: summary.batches.filter((b) => !b.ok).map((b) => `${b.batch}: ${b.error}`) }),
  ...(summary.error === null ? {} : { error: `${summary.error.phase}: ${summary.error.message}` }),
});

const recordQuietly = async (
  params: ArchiveWeatherParams,
  run_id: number | null,
  summary: ArchiveWeatherSummary,
): Promise<void> => {
  if (run_id === null) {
    return;
  }
  try {
    await params.db.jobFinished(run_id, summary.ok ? "ok" : "failed", toDetail(summary));
  } catch (cause) {
    const failure = toJobFailure({ phase: "ingest", cause });
    console.error(`job_finished に失敗した: ${failure.error_name}: ${failure.message}`);
  }
};

/** 格子を引く。**1 件も無いのは異常**で、黙って「成功・0 件」にしない。 */
const listCells = async (db: WeatherPort): Promise<readonly WeatherCell[]> => {
  const cells = await db.listGridCells();
  if (cells.length === 0) {
    throw new JobError({
      phase: "ingest",
      error_name: "NoWeatherGridCells",
      http_status: null,
      message: "気象格子が 0 件だった（station_attributes に有効行が無い）",
    });
  }
  return cells;
};

/** 最初の回を投げ、取り直せる失敗を取り直して、要約にする。 */
const archiveAll = async (run: Run): Promise<ArchiveWeatherSummary> => {
  const cells = await listCells(run.job.db);
  const batches = batchCells(cells, WEATHER_BATCH_SIZE);
  const planned = firstPlan(batches);
  const sent = await sendInOrder(run, planned, FIRST_ATTEMPT);
  const first = sent.map(
    (outcome, index) => outcome ?? skippedBatch(index, batches[index]?.length ?? 0),
  );
  const retries = retryPlan(batches, first);
  const retried = await sendInOrder(run, retries, RETRY_ATTEMPT);
  return summarize({
    hour_epoch_s: truncateToHour(run.epoch_s),
    n_cells: cells.length,
    batches: mergeRetries(first, retries, retried),
    duration_ms: run.elapsed(),
    counts: {
      n_retried: retried.filter((outcome) => outcome !== null).length,
      n_recovered: retried.filter((outcome) => outcome?.ok === true).length,
      n_skipped: sent.filter((outcome) => outcome === null).length,
      waited_ms: waitedOf(planned, sent) + waitedOf(retries, retried),
    },
    error: null,
  });
};

/** 記録を始める。**始められなくてもアーカイブは行う**（記録の不調で予報を失わない）。 */
const startQuietly = async (db: WeatherPort): Promise<number | null> => {
  try {
    return await db.jobStarted(ARCHIVE_WEATHER_JOB_NAME);
  } catch (cause) {
    const failure = toJobFailure({ phase: "ingest", cause });
    console.error(`job_started に失敗した: ${failure.error_name}: ${failure.message}`);
    return null;
  }
};

export const archiveWeather = async (
  params: ArchiveWeatherParams,
): Promise<ArchiveWeatherSummary> => {
  const clock = params.clock ?? Date.now;
  const started_ms = clock();
  const run: Run = {
    job: params,
    epoch_s: toEpochSeconds(params.now),
    elapsed: () => clock() - started_ms,
  };
  const run_id = await startQuietly(params.db);
  const summary = await archiveAll(run).catch((cause: unknown) =>
    summarize({
      hour_epoch_s: truncateToHour(run.epoch_s),
      n_cells: 0,
      batches: [],
      duration_ms: run.elapsed(),
      counts: NO_COUNTS,
      error: isJobError(cause) ? cause.failure : toJobFailure({ phase: "unknown", cause }),
    }),
  );
  await recordQuietly(params, run_id, summary);
  return summary;
};
