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
 */
import {
  WEATHER_BATCH_SIZE,
  batchCells,
  truncateToHour,
  weatherObjectPath,
  type EpochSeconds,
} from "@bikechance/shared";
import { JobError, isJobError, toJobFailure, type JobFailure } from "./errors";
import type { RawUploader } from "./storage";
import { fetchWeatherBatch, type WeatherCell } from "./weather-fetch";
import type { WeatherPort } from "./weather-port";

const MS_PER_S = 1000;

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
};

export type ArchiveWeatherSummary = {
  readonly ok: boolean;
  readonly n_cells: number;
  readonly n_batches: number;
  readonly n_saved: number;
  readonly n_duplicate: number;
  readonly n_failed: number;
  readonly bytes: number;
  readonly gzip_bytes: number;
  readonly hour_epoch_s: EpochSeconds;
  readonly duration_ms: number;
  readonly batches: readonly BatchOutcome[];
  readonly error: JobFailure | null;
};

export type ArchiveWeatherParams = {
  readonly db: WeatherPort;
  readonly upload: RawUploader;
  readonly gzip: (body: Uint8Array) => Promise<Uint8Array>;
  readonly contact_email: string;
  readonly now: Date;
};

const toEpochSeconds = (date: Date): EpochSeconds => Math.floor(date.getTime() / MS_PER_S);

const failedBatch = (batch: number, n_cells: number, error: string): BatchOutcome => ({
  batch,
  n_cells,
  ok: false,
  n_locations: null,
  bytes: null,
  gzip_bytes: null,
  path: null,
  duplicate: false,
  error,
});

/**
 * 1 分割を取得して保存する。**同じ時間内の再実行は同じパスに写像される**ので、
 * 既にあれば 409 が返り、それを正常系として扱う（§9.1）。
 */
const archiveBatch = async (params: {
  readonly job: ArchiveWeatherParams;
  readonly cells: readonly WeatherCell[];
  readonly batch: number;
  readonly epoch_s: EpochSeconds;
}): Promise<BatchOutcome> => {
  const { job, cells, batch, epoch_s } = params;
  try {
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
    };
  } catch (cause) {
    const failure = isJobError(cause) ? cause.failure : toJobFailure({ phase: "unknown", cause });
    // 種別（error_name）を落とさない。message だけだと後から grep できない
    return failedBatch(
      batch,
      cells.length,
      `${failure.phase}/${failure.error_name}: ${failure.message}`,
    );
  }
};

const summarize = (params: {
  readonly hour_epoch_s: EpochSeconds;
  readonly n_cells: number;
  readonly batches: readonly BatchOutcome[];
  readonly duration_ms: number;
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
  bytes: summary.bytes,
  gzip_bytes: summary.gzip_bytes,
  hour_epoch_s: summary.hour_epoch_s,
  duration_ms: summary.duration_ms,
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

export const archiveWeather = async (
  params: ArchiveWeatherParams,
): Promise<ArchiveWeatherSummary> => {
  const started_ms = Date.now();
  const epoch_s = toEpochSeconds(params.now);
  let run_id: number | null = null;

  try {
    run_id = await params.db.jobStarted(ARCHIVE_WEATHER_JOB_NAME);
  } catch (cause) {
    // 記録を始められなくてもアーカイブは行う。記録の不調で予報を失わない
    const failure = toJobFailure({ phase: "ingest", cause });
    console.error(`job_started に失敗した: ${failure.error_name}: ${failure.message}`);
  }

  const summary = await (async (): Promise<ArchiveWeatherSummary> => {
    const cells = await params.db.listGridCells();
    if (cells.length === 0) {
      // 属性が 1 件も無いのは異常。黙って「成功・0 件」にしない
      throw new JobError({
        phase: "ingest",
        error_name: "NoWeatherGridCells",
        http_status: null,
        message: "気象格子が 0 件だった（station_attributes に有効行が無い）",
      });
    }
    const batches = batchCells(cells, WEATHER_BATCH_SIZE);
    const outcomes: BatchOutcome[] = [];
    for (const [index, batch] of batches.entries()) {
      outcomes.push(await archiveBatch({ job: params, cells: batch, batch: index, epoch_s }));
    }
    return summarize({
      hour_epoch_s: truncateToHour(epoch_s),
      n_cells: cells.length,
      batches: outcomes,
      duration_ms: Date.now() - started_ms,
      error: null,
    });
  })().catch((cause: unknown) =>
    summarize({
      hour_epoch_s: truncateToHour(epoch_s),
      n_cells: 0,
      batches: [],
      duration_ms: Date.now() - started_ms,
      error: isJobError(cause) ? cause.failure : toJobFailure({ phase: "unknown", cause }),
    }),
  );

  await recordQuietly(params, run_id, summary);
  return summary;
};
