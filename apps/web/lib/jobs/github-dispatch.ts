/**
 * GitHub Actions の API の入り口（日次の枠を起こすジョブ。開発プランの D-50）。
 *
 * 使うのは 2 つだけ：ワークフローの**走っている回の数**と、`workflow_dispatch`。上位のロジック
 * （`daily-dispatch.ts`）は `fetch` に依存せず、テストで分岐を全部通せる（`WeatherPort` と同じ考え）。
 *
 * **トークンは `Authorization` の見出しにだけ載せる。** URL にも記録にも例外にも出さない——
 * 失敗は `{ phase, http_status, error_name }` に詰め替え、文字列は `redact()` にトークンを渡して
 * 伏せる（`toJobFailure`）。`Response` はこのモジュールの外へ出さない（ODPT の取得と同じ規律）。
 */
import { GITHUB_API_TIMEOUT_MS } from "@bikechance/shared";
import { z } from "zod";
import { JobError, httpJobFailure, toJobFailure } from "./errors";

const GITHUB_API_BASE = "https://api.github.com";
const GITHUB_API_VERSION = "2022-11-28";
const USER_AGENT = "bikechance-daily-dispatch";

/** `workflow_dispatch` が成功したときの応答（本文なし）。 */
const HTTP_NO_CONTENT = 204;
const HTTP_OK = 200;

/** 一覧で見る回の数。**走っている回は新しい側に並ぶ**ので、最近の 20 回で足りる。 */
const RUNS_PER_PAGE = 20;

/** 終わっていない回の状態（GitHub の `status`。`completed` 以外）。 */
const ACTIVE_STATUSES: ReadonlySet<string> = new Set([
  "queued",
  "in_progress",
  "waiting",
  "requested",
  "pending",
]);

/** 起こしてよいワークフロー。**この 2 つのほかは起こさない**（D-50）。 */
export const DAILY_WORKFLOWS = ["build-features.yml", "evaluate-daily.yml"] as const;
export type DailyWorkflow = (typeof DAILY_WORKFLOWS)[number];

export type GitHubPort = {
  /** そのワークフローの、終わっていない回の数（定時の回も手で起こした回も数える）。 */
  readonly activeRuns: (workflow: DailyWorkflow) => Promise<number>;
  /** `main` で起こす。**入力は渡さない**——日付が空なら前日（両方のワークフローの既定）。 */
  readonly dispatch: (workflow: DailyWorkflow) => Promise<void>;
};

const runsSchema = z.looseObject({
  workflow_runs: z.array(z.looseObject({ status: z.string() })),
});

export type GitHubPortParams = {
  readonly token: string;
  /** `owner/name`。 */
  readonly repository: string;
  readonly fetch?: typeof fetch;
  readonly timeout_ms?: number;
};

const headersFor = (token: string): Record<string, string> => ({
  Accept: "application/vnd.github+json",
  Authorization: `Bearer ${token}`,
  "X-GitHub-Api-Version": GITHUB_API_VERSION,
  "User-Agent": USER_AGENT,
});

/** 要求を投げ、**状態と本文の文字列だけ**を返す（`Response` を外へ出さない）。 */
const send = async (
  params: GitHubPortParams,
  path: string,
  init: { readonly method: "GET" | "POST"; readonly body?: string },
): Promise<{ readonly status: number; readonly text: string }> => {
  const base = params.fetch ?? fetch;
  try {
    const response = await base(`${GITHUB_API_BASE}${path}`, {
      method: init.method,
      headers: headersFor(params.token),
      ...(init.body === undefined ? {} : { body: init.body }),
      signal: AbortSignal.timeout(params.timeout_ms ?? GITHUB_API_TIMEOUT_MS),
    });
    return { status: response.status, text: await response.text() };
  } catch (cause) {
    throw new JobError(toJobFailure({ phase: "fetch", cause, secrets: [params.token] }));
  }
};

/** 期待しない状態のときの失敗。**本文は載せない**（GitHub の本文には URL が入る）。 */
const unexpected = (what: string, status: number): JobError =>
  new JobError(httpJobFailure({ phase: "fetch", http_status: status, detail: what }));

const workflowPath = (repository: string, workflow: DailyWorkflow): string =>
  `/repos/${repository}/actions/workflows/${workflow}`;

/** 一覧の本文から、終わっていない回を数える。**形が違えば投げる**（数えたつもりで 0 にしない）。 */
export const countActiveRuns = (text: string): number => {
  const parsed = runsSchema.safeParse(JSON.parse(text));
  if (!parsed.success) {
    throw new JobError({
      phase: "parse",
      error_name: "UnexpectedRunsShape",
      http_status: null,
      message: "workflow runs の一覧の形が違う",
    });
  }
  return parsed.data.workflow_runs.filter((run) => ACTIVE_STATUSES.has(run.status)).length;
};

const parseRuns = (text: string): number => {
  try {
    return countActiveRuns(text);
  } catch (cause) {
    if (cause instanceof JobError) {
      throw cause;
    }
    throw new JobError(toJobFailure({ phase: "parse", cause }));
  }
};

export const createGitHubPort = (params: GitHubPortParams): GitHubPort => ({
  activeRuns: async (workflow) => {
    const path = `${workflowPath(params.repository, workflow)}/runs?per_page=${RUNS_PER_PAGE}`;
    const { status, text } = await send(params, path, { method: "GET" });
    if (status !== HTTP_OK) {
      throw unexpected(`GitHub の API（${workflow} の回の一覧）`, status);
    }
    return parseRuns(text);
  },

  dispatch: async (workflow) => {
    const path = `${workflowPath(params.repository, workflow)}/dispatches`;
    const body = JSON.stringify({ ref: "main", inputs: {} });
    const { status } = await send(params, path, { method: "POST", body });
    if (status !== HTTP_NO_CONTENT) {
      throw unexpected(`GitHub の API（${workflow} の workflow_dispatch）`, status);
    }
  },
});
