/**
 * 日次の枠を起こすジョブ（開発プランの D-50）が使う DB の入り口。
 *
 * `WeatherPort` と同じ考え方で、DB とのやりとりを 3 つの関数に絞る：記録の始まりと終わり
 * （`job_started`・`job_finished`）と、**判断に使う `job_runs` の行を読む**ことだけ。
 * 判断そのもの（`daily-dispatch.ts`）は supabase-js に依存しない。
 */
import type { SupabaseClient } from "@supabase/supabase-js";
import { z } from "zod";
import { JobError, toJobFailure } from "./errors";

/** 判断に使う `job_runs` の 1 行。`detail` は形を決めずに受ける（読む側が欄を確かめる）。 */
const jobRunSchema = z.object({
  id: z.number(),
  job_name: z.string(),
  status: z.string(),
  started_at: z.string(),
  detail: z.record(z.string(), z.unknown()).nullable(),
});

export type JobRunRecord = z.infer<typeof jobRunSchema>;

/** 1 回に読む行の上限。日次の 4 ジョブと起こした回を 1 日半ぶん読んでも、数十行で収まる。 */
const RUNS_LIMIT = 500;

export type DispatchPort = {
  readonly jobStarted: (job_name: string) => Promise<number>;
  readonly jobFinished: (
    id: number,
    status: "ok" | "failed" | "skipped",
    detail: Readonly<Record<string, unknown>>,
  ) => Promise<void>;
  /** `since` 以後に始まった、名前が `job_names` のどれかの行（古い順）。 */
  readonly runsSince: (
    job_names: readonly string[],
    since: Date,
  ) => Promise<readonly JobRunRecord[]>;
};

const raiseDbError = (what: string, error: unknown): never => {
  const failure = toJobFailure({ phase: "ingest", cause: error });
  throw new JobError({ ...failure, message: `${what}: ${failure.message}` });
};

/** 読んだ行を確かめる。**1 行でも形が違えば投げる**（黙って落とすと、済んだ枠を起こし直しうる）。 */
export const toJobRunRecords = (data: unknown): readonly JobRunRecord[] => {
  const parsed = z.array(jobRunSchema).safeParse(data);
  if (!parsed.success) {
    throw new JobError({
      phase: "ingest",
      error_name: "UnexpectedJobRunsShape",
      http_status: null,
      message: "job_runs の行の形が違う",
    });
  }
  return parsed.data;
};

export const createSupabaseDispatchPort = (client: SupabaseClient): DispatchPort => ({
  jobStarted: async (job_name) => {
    const { data, error } = await client.rpc("job_started", { p_job_name: job_name });
    if (error !== null) {
      raiseDbError("job_started", error);
    }
    if (typeof data !== "number") {
      throw new JobError({
        phase: "ingest",
        error_name: "UnexpectedJobId",
        http_status: null,
        message: "job_started が行 id を返さなかった",
      });
    }
    return data;
  },

  jobFinished: async (id, status, detail) => {
    const { error } = await client.rpc("job_finished", {
      p_id: id,
      p_status: status,
      p_detail: detail,
    });
    if (error !== null) {
      raiseDbError("job_finished", error);
    }
  },

  runsSince: async (job_names, since) => {
    const { data, error } = await client
      .from("job_runs")
      .select("id, job_name, status, started_at, detail")
      .in("job_name", [...job_names])
      .gte("started_at", since.toISOString())
      .order("id", { ascending: true })
      .limit(RUNS_LIMIT);
    if (error !== null) {
      raiseDbError("job_runs", error);
    }
    return toJobRunRecords(data);
  },
});
