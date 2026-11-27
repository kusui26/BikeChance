/**
 * 日次の枠を起こす（開発プランの D-50、W13 プランの W13-07・§4.6）。
 *
 * GitHub Actions の定時は 4〜11 時間遅れる（W12 プランの §10.2 の 6）。10:15・10:35 JST に、
 * **来ていない枠だけ**を `workflow_dispatch` で起こす。W12-07 で手で起こしていた判断と同じ：
 *
 *   1. 昨日（JST）の特徴量・プロファイル・傾向のどれかが無い → `build-features.yml` を日付を空で
 *      （空なら前日。傾向の段まで動く。**日付を渡すと傾向は写らない**——W12 プランの 11/24 の記録）
 *   2. 昨日の特徴量が済んでいて、昨日の日次評価が無い → `evaluate-daily.yml`
 *      （**特徴量の後**に起こす。どちらも Storage を重く読むので重ねない）
 *   3. どちらもそろっている → 何もしない
 *
 * **1 回に起こすのは 1 つだけ。** 10:15 の回で特徴量を起こし、20 分後の 10:35 の回が、済んだ特徴量を
 * 見て日次評価を起こす。
 *
 * **冪等**：起こしたことは `job_runs`（`dispatch_daily` の `detail.dispatched`）に残し、**同じ日に
 * 同じワークフローを 2 度起こさない**（起こした回が落ちたら、そのワークフローの失敗の警報で人が
 * 見る）。**同時に呼ばれたら**（Vercel Cron は二重に起動しうる）、走っている `dispatch_daily` の
 * うち行 id の小さい回だけが進み、ほかは `skipped`。GitHub で走っている回があれば起こさない
 * （定時の回が来ていれば、その回に任せる）。
 */
import { jstDate, shiftDate } from "@bikechance/shared";
import type { DispatchPort, JobRunRecord } from "./dispatch-port";
import { isJobError, toJobFailure, type JobFailure } from "./errors";
import type { DailyWorkflow, GitHubPort } from "./github-dispatch";

export const DAILY_DISPATCH_JOB_NAME = "dispatch_daily";

const FEATURES_JOB = "build_features";
const PROFILES_JOB = "build_profiles";
const TREND_JOB = "build_trend";
const EVALUATE_JOB = "evaluate_daily";

/** 判断に読む `job_runs` の名前。 */
export const WATCHED_JOBS = [
  FEATURES_JOB,
  PROFILES_JOB,
  TREND_JOB,
  EVALUATE_JOB,
  DAILY_DISPATCH_JOB_NAME,
] as const;

const FEATURES_WORKFLOW: DailyWorkflow = "build-features.yml";
const EVALUATE_WORKFLOW: DailyWorkflow = "evaluate-daily.yml";

/** 走っている `dispatch_daily` を「同時に呼ばれた回」とみなす時間。上限（60 秒）より十分長い。 */
const CONCURRENT_WINDOW_MS = 10 * 60 * 1000;

/** 昨日の枠がどこまでそろっているか（`job_runs` の `ok` の行から）。 */
export type DailyState = {
  /** 昨日（JST）。**この日の枠を見る**（日次の 4 ジョブはどれも前日ぶんを作る） */
  readonly target_date: string;
  readonly features: boolean;
  readonly profiles: boolean;
  readonly trend: boolean;
  readonly evaluate: boolean;
  /** 今日（JST）に、この仕事がすでに起こしたワークフロー */
  readonly dispatched_today: readonly DailyWorkflow[];
  /** 自分より前に始まって、まだ走っている `dispatch_daily` があるか */
  readonly concurrent: boolean;
};

export type NoneReason =
  "all_done" | "already_dispatched" | "already_running" | "waiting_for_features" | "concurrent";

export type Decision =
  | { readonly kind: "dispatch"; readonly workflow: DailyWorkflow }
  | { readonly kind: "none"; readonly reason: NoneReason; readonly workflow: DailyWorkflow | null };

/** その JST の暦日の 0 時。 */
export const jstMidnight = (iso: string): Date => new Date(`${iso}T00:00:00+09:00`);

/** `ok` の行のうち、`detail` の `key` が `value` のものがあるか。 */
const hasOk = (
  rows: readonly JobRunRecord[],
  job_name: string,
  key: string,
  value: string,
): boolean =>
  rows.some(
    (row) => row.job_name === job_name && row.status === "ok" && row.detail?.[key] === value,
  );

const isWorkflow = (value: unknown): value is DailyWorkflow =>
  value === FEATURES_WORKFLOW || value === EVALUATE_WORKFLOW;

/** `since` 以後に、`dispatch_daily` が起こしたワークフロー（`ok` の行の `detail.dispatched`）。 */
const dispatchedSince = (rows: readonly JobRunRecord[], since: Date): readonly DailyWorkflow[] =>
  rows
    .filter((row) => row.job_name === DAILY_DISPATCH_JOB_NAME && row.status === "ok")
    .filter((row) => Date.parse(row.started_at) >= since.getTime())
    .map((row) => row.detail?.["dispatched"])
    .filter(isWorkflow);

/** 自分より行 id の小さい、走っている `dispatch_daily` があるか（同時に呼ばれた回）。 */
const isConcurrent = (rows: readonly JobRunRecord[], run_id: number, now: Date): boolean =>
  rows.some(
    (row) =>
      row.job_name === DAILY_DISPATCH_JOB_NAME &&
      row.status === "running" &&
      row.id < run_id &&
      now.getTime() - Date.parse(row.started_at) < CONCURRENT_WINDOW_MS,
  );

/** 読んだ行から、昨日の枠がどこまでそろっているかを決める。 */
export const dailyStateOf = (
  rows: readonly JobRunRecord[],
  params: { readonly now: Date; readonly run_id: number },
): DailyState => {
  const today = jstDate(params.now);
  const target_date = shiftDate(today, -1);
  return {
    target_date,
    features: hasOk(rows, FEATURES_JOB, "date", target_date),
    profiles: hasOk(rows, PROFILES_JOB, "date", target_date),
    trend: hasOk(rows, TREND_JOB, "date", target_date),
    evaluate: hasOk(rows, EVALUATE_JOB, "metric_date", target_date),
    dispatched_today: dispatchedSince(rows, jstMidnight(today)),
    concurrent: isConcurrent(rows, params.run_id, params.now),
  };
};

const none = (reason: NoneReason, workflow: DailyWorkflow | null = null): Decision => ({
  kind: "none",
  reason,
  workflow,
});

/** 日次評価の枠を起こすか。**特徴量の後**に、同じ日に 1 度だけ。 */
const decideEvaluate = (state: DailyState): Decision => {
  if (!state.features) {
    return none("waiting_for_features", EVALUATE_WORKFLOW);
  }
  if (state.dispatched_today.includes(EVALUATE_WORKFLOW)) {
    return none("already_dispatched", EVALUATE_WORKFLOW);
  }
  return { kind: "dispatch", workflow: EVALUATE_WORKFLOW };
};

/** 何を起こすか（GitHub で走っている回を見る前）。**特徴量の枠が先**、1 回に 1 つ。 */
export const decide = (state: DailyState): Decision => {
  if (state.concurrent) {
    return none("concurrent");
  }
  const features_pending = !(state.features && state.profiles && state.trend);
  if (features_pending && !state.dispatched_today.includes(FEATURES_WORKFLOW)) {
    return { kind: "dispatch", workflow: FEATURES_WORKFLOW };
  }
  if (!state.evaluate) {
    return decideEvaluate(state);
  }
  return features_pending ? none("already_dispatched", FEATURES_WORKFLOW) : none("all_done");
};

export type DispatchParams = {
  readonly db: DispatchPort;
  readonly github: GitHubPort;
  readonly now: Date;
  /** 記録に出す文字列から伏せるもの（GitHub のトークン） */
  readonly secrets: readonly string[];
};

export type DispatchAction = "dispatched" | "none" | "skipped";

export type DispatchSummary = {
  readonly ok: boolean;
  readonly action: DispatchAction | null;
  readonly target_date: string | null;
  readonly workflow: DailyWorkflow | null;
  readonly reason: NoneReason | null;
  readonly error: JobFailure | null;
};

/** 判断と結果（記録と応答の両方の素。**トークンは入らない**）。 */
type Outcome = {
  readonly state: DailyState;
  readonly decision: Decision;
  /** 起こす前に数えた、GitHub で走っている回（数えなかったら null） */
  readonly active_runs: number | null;
  readonly dispatched: boolean;
};

const actionOf = (outcome: Outcome): DispatchAction => {
  if (outcome.dispatched) {
    return "dispatched";
  }
  return outcome.decision.kind === "none" && outcome.decision.reason === "concurrent"
    ? "skipped"
    : "none";
};

/** 走っている回があれば起こさない。無ければ起こす。 */
const act = async (params: DispatchParams, state: DailyState): Promise<Outcome> => {
  const decision = decide(state);
  if (decision.kind === "none") {
    return { state, decision, active_runs: null, dispatched: false };
  }
  const active_runs = await params.github.activeRuns(decision.workflow);
  if (active_runs > 0) {
    const running = none("already_running", decision.workflow);
    return { state, decision: running, active_runs, dispatched: false };
  }
  await params.github.dispatch(decision.workflow);
  return { state, decision, active_runs, dispatched: true };
};

/** 記録の `detail`。**起こしたときだけ `dispatched` を書く**（次の回が「今日起こした」を読む鍵）。 */
export const detailOf = (outcome: Outcome): Record<string, unknown> => ({
  action: actionOf(outcome),
  target_date: outcome.state.target_date,
  workflow: outcome.decision.workflow,
  reason: outcome.decision.kind === "none" ? outcome.decision.reason : null,
  ...(outcome.dispatched ? { dispatched: outcome.decision.workflow } : {}),
  active_runs: outcome.active_runs,
  state: outcome.state,
});

const summaryOf = (outcome: Outcome, error: JobFailure | null): DispatchSummary => ({
  ok: error === null,
  action: actionOf(outcome),
  target_date: outcome.state.target_date,
  workflow: outcome.decision.workflow,
  reason: outcome.decision.kind === "none" ? outcome.decision.reason : null,
  error,
});

const failedSummary = (error: JobFailure): DispatchSummary => ({
  ok: false,
  action: null,
  target_date: null,
  workflow: null,
  reason: null,
  error,
});

const failureOf = (params: DispatchParams, cause: unknown): JobFailure =>
  isJobError(cause)
    ? cause.failure
    : toJobFailure({ phase: "unknown", cause, secrets: params.secrets });

/** 読んで、決めて、（要れば）起こす。 */
const decideAndAct = async (params: DispatchParams, run_id: number): Promise<Outcome> => {
  const since = jstMidnight(shiftDate(jstDate(params.now), -1));
  const rows = await params.db.runsSince(WATCHED_JOBS, since);
  return act(params, dailyStateOf(rows, { now: params.now, run_id }));
};

/**
 * 記録する。**失敗は返す**——起こした後に記録できなければ、次の回が「今日起こした」を読めず
 * 二重に起こしうるので、ルートは 500 を返す（Vercel Observability の失敗の率に出す）。
 */
const record = async (
  params: DispatchParams,
  run_id: number,
  status: "ok" | "failed" | "skipped",
  detail: Record<string, unknown>,
): Promise<JobFailure | null> =>
  params.db.jobFinished(run_id, status, detail).then(
    () => null,
    (cause: unknown) => failureOf(params, cause),
  );

const finishOk = async (
  params: DispatchParams,
  run_id: number,
  outcome: Outcome,
): Promise<DispatchSummary> => {
  const status = actionOf(outcome) === "skipped" ? "skipped" : "ok";
  return summaryOf(outcome, await record(params, run_id, status, detailOf(outcome)));
};

const finishFailed = async (
  params: DispatchParams,
  run_id: number,
  error: JobFailure,
): Promise<DispatchSummary> => {
  await record(params, run_id, "failed", { action: null, error });
  return failedSummary(error);
};

const startRun = async (params: DispatchParams): Promise<number | JobFailure> =>
  params.db.jobStarted(DAILY_DISPATCH_JOB_NAME).catch((cause: unknown) => failureOf(params, cause));

/**
 * 日次の枠を起こす本体。**記録を始められなければ起こさない**——`job_runs` が冪等の鍵なので、
 * 記録の無いまま起こすと、次の回が同じワークフローをもう 1 度起こしうる。
 */
export const dispatchDaily = async (params: DispatchParams): Promise<DispatchSummary> => {
  const started = await startRun(params);
  if (typeof started !== "number") {
    return failedSummary(started);
  }
  return decideAndAct(params, started).then(
    async (outcome) => finishOk(params, started, outcome),
    async (cause: unknown) => finishFailed(params, started, failureOf(params, cause)),
  );
};
