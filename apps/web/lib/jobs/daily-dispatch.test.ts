import { describe, expect, it } from "vitest";
import {
  DAILY_DISPATCH_JOB_NAME,
  WATCHED_JOBS,
  dailyStateOf,
  decide,
  dispatchDaily,
  jstMidnight,
  type DailyState,
  type DispatchParams,
} from "./daily-dispatch";
import type { DispatchPort, JobRunRecord } from "./dispatch-port";
import { JobError } from "./errors";
import type { DailyWorkflow, GitHubPort } from "./github-dispatch";

/** 2026-11-30（月）10:15:20 JST。昨日は 11-29 */
const NOW = new Date("2026-11-30T01:15:20.000Z");
const TARGET = "2026-11-29";
const RUN_ID = 900;
const TOKEN = "test-dispatch-token-value-0123456789";

const row = (
  id: number,
  job_name: string,
  status: string,
  started_at: string,
  detail: Record<string, unknown> | null = null,
): JobRunRecord => ({ id, job_name, status, started_at, detail });

/** 昨日の枠がそろった行（特徴量・プロファイル・傾向・日次評価）。 */
const finishedDay = (): JobRunRecord[] => [
  row(10, "build_profiles", "ok", "2026-11-30T00:01:00Z", { date: TARGET }),
  row(11, "build_trend", "ok", "2026-11-30T00:03:00Z", { date: TARGET }),
  row(12, "build_features", "ok", "2026-11-30T00:59:00Z", { date: TARGET }),
  row(13, "evaluate_daily", "ok", "2026-11-30T01:01:00Z", { metric_date: TARGET }),
];

const stateOf = (rows: readonly JobRunRecord[]): DailyState =>
  dailyStateOf(rows, { now: NOW, run_id: RUN_ID });

describe("JST の日と、読む行の範囲", () => {
  it("JST の 0 時", () => {
    expect(jstMidnight("2026-11-30").toISOString()).toBe("2026-11-29T15:00:00.000Z");
  });

  it("**昨日（JST）の枠を見る**（UTC の日付と 1 日ずれる時刻でも）", () => {
    // 2026-11-30 08:59 JST は UTC では 11-29 23:59。昨日は 11-29
    const early = dailyStateOf([], { now: new Date("2026-11-29T23:59:00Z"), run_id: RUN_ID });
    expect(early.target_date).toBe(TARGET);
    expect(stateOf([]).target_date).toBe(TARGET);
  });

  it("判断に読むのは日次の 4 つと、自分の記録", () => {
    expect(WATCHED_JOBS).toEqual([
      "build_features",
      "build_profiles",
      "build_trend",
      "evaluate_daily",
      DAILY_DISPATCH_JOB_NAME,
    ]);
  });
});

describe("昨日の枠がどこまでそろっているか", () => {
  it("そろっている", () => {
    expect(stateOf(finishedDay())).toMatchObject({
      features: true,
      profiles: true,
      trend: true,
      evaluate: true,
      dispatched_today: [],
      concurrent: false,
    });
  });

  it("**日付の違う回・ok でない回は数えない**（一昨日の特徴量や、落ちた日次評価）", () => {
    const state = stateOf([
      row(1, "build_features", "ok", "2026-11-29T01:00:00Z", { date: "2026-11-28" }),
      row(2, "build_profiles", "failed", "2026-11-30T00:01:00Z", { date: TARGET }),
      row(3, "build_trend", "running", "2026-11-30T00:03:00Z", { date: TARGET }),
      row(4, "evaluate_daily", "ok", "2026-11-29T05:00:00Z", { metric_date: "2026-11-28" }),
    ]);
    expect(state).toMatchObject({
      features: false,
      profiles: false,
      trend: false,
      evaluate: false,
    });
  });

  it("**日次評価は `metric_date` で見る**（`date` の欄は無い）", () => {
    const state = stateOf([
      row(1, "evaluate_daily", "ok", "2026-11-30T01:01:00Z", { date: TARGET }),
    ]);
    expect(state.evaluate).toBe(false);
  });

  it("**今日起こしたものは、今日の ok の行の `dispatched` だけ**（昨日の行・落ちた行は数えない）", () => {
    const state = stateOf([
      row(5, DAILY_DISPATCH_JOB_NAME, "ok", "2026-11-29T01:15:20Z", {
        dispatched: "build-features.yml",
      }),
      row(6, DAILY_DISPATCH_JOB_NAME, "failed", "2026-11-30T00:50:00Z", {
        dispatched: "evaluate-daily.yml",
      }),
      row(7, DAILY_DISPATCH_JOB_NAME, "ok", "2026-11-30T00:55:00Z", { action: "none" }),
      row(8, DAILY_DISPATCH_JOB_NAME, "ok", "2026-11-30T01:00:00Z", {
        dispatched: "build-features.yml",
      }),
    ]);
    expect(state.dispatched_today).toEqual(["build-features.yml"]);
  });

  it("**同時に呼ばれた回**：自分より id の小さい、10 分以内に始まって走っている回だけ", () => {
    const earlier = row(899, DAILY_DISPATCH_JOB_NAME, "running", "2026-11-30T01:15:19Z");
    const later = row(901, DAILY_DISPATCH_JOB_NAME, "running", "2026-11-30T01:15:21Z");
    const stale = row(500, DAILY_DISPATCH_JOB_NAME, "running", "2026-11-30T00:35:00Z");
    const mine = row(RUN_ID, DAILY_DISPATCH_JOB_NAME, "running", "2026-11-30T01:15:20Z");
    expect(stateOf([earlier, mine]).concurrent).toBe(true);
    expect(stateOf([mine, later]).concurrent).toBe(false);
    expect(stateOf([stale, mine]).concurrent).toBe(false);
  });
});

const base: DailyState = {
  target_date: TARGET,
  features: true,
  profiles: true,
  trend: true,
  evaluate: true,
  dispatched_today: [],
  concurrent: false,
};

describe("何を起こすか", () => {
  it("そろっていれば何もしない", () => {
    expect(decide(base)).toEqual({ kind: "none", reason: "all_done", workflow: null });
  });

  it.each([
    ["特徴量", { features: false }],
    ["プロファイル（00:40 の枠）", { profiles: false }],
    ["傾向", { trend: false }],
  ])("%sが無ければ、特徴量の枠（日付を空で）を起こす", (_name, missing) => {
    expect(decide({ ...base, ...missing, evaluate: false })).toEqual({
      kind: "dispatch",
      workflow: "build-features.yml",
    });
  });

  it("**特徴量が済んでいれば、日次評価を起こす**（1 回に 1 つ）", () => {
    expect(decide({ ...base, evaluate: false })).toEqual({
      kind: "dispatch",
      workflow: "evaluate-daily.yml",
    });
  });

  it("**同じ日に同じワークフローを 2 度は起こさない**", () => {
    const dispatched: DailyWorkflow[] = ["build-features.yml"];
    expect(
      decide({ ...base, features: false, evaluate: false, dispatched_today: dispatched }),
    ).toEqual({
      kind: "none",
      reason: "waiting_for_features",
      workflow: "evaluate-daily.yml",
    });
    expect(decide({ ...base, evaluate: false, dispatched_today: ["evaluate-daily.yml"] })).toEqual({
      kind: "none",
      reason: "already_dispatched",
      workflow: "evaluate-daily.yml",
    });
    expect(decide({ ...base, trend: false, dispatched_today: dispatched })).toEqual({
      kind: "none",
      reason: "already_dispatched",
      workflow: "build-features.yml",
    });
  });

  it("**特徴量は済み・傾向だけ無い日も、日次評価は起こせる**（日次評価は特徴量だけを待つ）", () => {
    const state = { ...base, trend: false, evaluate: false };
    expect(decide({ ...state, dispatched_today: ["build-features.yml"] })).toEqual({
      kind: "dispatch",
      workflow: "evaluate-daily.yml",
    });
  });

  it("同時に呼ばれた回は何もしない", () => {
    expect(decide({ ...base, features: false, concurrent: true })).toEqual({
      kind: "none",
      reason: "concurrent",
      workflow: null,
    });
  });
});

// ── 本体（DB と GitHub は代役）──────────────────────────────────
type Fake = {
  readonly params: DispatchParams;
  readonly finished: { status: string; detail: Record<string, unknown> }[];
  readonly dispatched: DailyWorkflow[];
  readonly asked: DailyWorkflow[];
};

const fake = (
  rows: readonly JobRunRecord[],
  overrides: {
    readonly active?: number;
    readonly dispatchThrows?: Error;
    readonly startThrows?: Error;
    readonly finishThrows?: Error;
  } = {},
): Fake => {
  const finished: { status: string; detail: Record<string, unknown> }[] = [];
  const dispatched: DailyWorkflow[] = [];
  const asked: DailyWorkflow[] = [];
  const db: DispatchPort = {
    jobStarted: async () => {
      if (overrides.startThrows) throw overrides.startThrows;
      return RUN_ID;
    },
    jobFinished: async (_id, status, detail) => {
      if (overrides.finishThrows) throw overrides.finishThrows;
      finished.push({ status, detail: { ...detail } });
    },
    runsSince: async () => rows,
  };
  const github: GitHubPort = {
    activeRuns: async (workflow) => {
      asked.push(workflow);
      return overrides.active ?? 0;
    },
    dispatch: async (workflow) => {
      if (overrides.dispatchThrows) throw overrides.dispatchThrows;
      dispatched.push(workflow);
    },
  };
  return { params: { db, github, now: NOW, secrets: [TOKEN] }, finished, dispatched, asked };
};

const withoutFeatures = (): JobRunRecord[] =>
  finishedDay().filter(
    (one) => one.job_name !== "build_features" && one.job_name !== "evaluate_daily",
  );

describe("日次の枠を起こす本体", () => {
  it("**そろっていれば GitHub に聞きもしない**（ok で記録する）", async () => {
    const run = fake(finishedDay());
    const summary = await dispatchDaily(run.params);
    expect(summary).toMatchObject({ ok: true, action: "none", reason: "all_done" });
    expect(run.asked).toEqual([]);
    expect(run.finished).toHaveLength(1);
    expect(run.finished[0]?.status).toBe("ok");
    expect(run.finished[0]?.detail).not.toHaveProperty("dispatched");
  });

  it("**特徴量が無ければ起こし、記録に `dispatched` を残す**（次の回が読む鍵）", async () => {
    const run = fake(withoutFeatures());
    const summary = await dispatchDaily(run.params);
    expect(summary).toMatchObject({
      ok: true,
      action: "dispatched",
      workflow: "build-features.yml",
      target_date: TARGET,
    });
    expect(run.dispatched).toEqual(["build-features.yml"]);
    expect(run.finished[0]?.detail).toMatchObject({
      dispatched: "build-features.yml",
      active_runs: 0,
    });
  });

  it("**GitHub で走っている回があれば起こさない**（定時の回に任せる）", async () => {
    const run = fake(withoutFeatures(), { active: 1 });
    const summary = await dispatchDaily(run.params);
    expect(summary).toMatchObject({ ok: true, action: "none", reason: "already_running" });
    expect(run.dispatched).toEqual([]);
    expect(run.finished[0]?.detail).not.toHaveProperty("dispatched");
  });

  it("**同時に呼ばれた回は skipped**（GitHub に聞かず、起こさない）", async () => {
    const earlier = row(RUN_ID - 1, DAILY_DISPATCH_JOB_NAME, "running", "2026-11-30T01:15:19Z");
    const run = fake([...withoutFeatures(), earlier]);
    const summary = await dispatchDaily(run.params);
    expect(summary).toMatchObject({ ok: true, action: "skipped", reason: "concurrent" });
    expect(run.asked).toEqual([]);
    expect(run.finished[0]?.status).toBe("skipped");
  });

  it("**記録を始められなければ起こさない**（記録が冪等の鍵）", async () => {
    const run = fake(withoutFeatures(), { startThrows: new Error("db down") });
    const summary = await dispatchDaily(run.params);
    expect(summary.ok).toBe(false);
    expect(run.asked).toEqual([]);
    expect(run.dispatched).toEqual([]);
  });

  it("**起こせなければ failed で記録して 500 の形**（トークンは記録にも応答にも出ない）", async () => {
    const failure = new JobError({
      phase: "fetch",
      error_name: "UnexpectedHttpStatus",
      http_status: 403,
      message: "GitHub の API（build-features.yml の workflow_dispatch）",
    });
    const run = fake(withoutFeatures(), { dispatchThrows: failure });
    const summary = await dispatchDaily(run.params);
    expect(summary).toMatchObject({ ok: false, error: { http_status: 403 } });
    expect(run.finished[0]?.status).toBe("failed");
    expect(JSON.stringify([summary, run.finished])).not.toContain(TOKEN);
  });

  it("**思わぬ例外のトークンも伏せる**", async () => {
    const run = fake(withoutFeatures(), { dispatchThrows: new Error(`boom ${TOKEN}`) });
    const summary = await dispatchDaily(run.params);
    expect(summary.ok).toBe(false);
    expect(JSON.stringify([summary, run.finished])).not.toContain(TOKEN);
  });

  it("**起こした後に記録できなければ ok にしない**（次の回が二重に起こしうるので 500）", async () => {
    const run = fake(withoutFeatures(), { finishThrows: new Error("db down") });
    const summary = await dispatchDaily(run.params);
    expect(run.dispatched).toEqual(["build-features.yml"]);
    expect(summary).toMatchObject({ ok: false, action: "dispatched" });
  });
});
