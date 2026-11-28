import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { isJobError } from "./errors";
import { DAILY_WORKFLOWS, countActiveRuns, createGitHubPort } from "./github-dispatch";

const TOKEN = "test-dispatch-token-value-0123456789";
const REPOSITORY = "owner/repo";

type Call = { readonly url: string; readonly init: RequestInit | undefined };

/** 決まった応答を返し、呼ばれた URL と要求を覚える `fetch`。 */
const fakeFetch = (
  respond: () => Response | Promise<Response>,
): { readonly fetch: typeof fetch; readonly calls: Call[] } => {
  const calls: Call[] = [];
  const fetchLike = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    calls.push({ url: String(input), init });
    return respond();
  };
  return { fetch: fetchLike, calls };
};

const runsBody = (...statuses: string[]): string =>
  JSON.stringify({
    total_count: statuses.length,
    workflow_runs: statuses.map((status, index) => ({ id: index, status, event: "schedule" })),
  });

const headerOf = (call: Call | undefined, name: string): string | null =>
  new Headers(call?.init?.headers).get(name);

const failureOf = async (work: Promise<unknown>): Promise<unknown> => {
  try {
    await work;
  } catch (cause) {
    return cause;
  }
  throw new Error("投げなかった");
};

describe("走っている回を数える", () => {
  it("**終わっていない回だけ**（queued・in_progress・waiting・requested・pending）", () => {
    const body = runsBody("completed", "queued", "in_progress", "waiting", "requested", "pending");
    expect(countActiveRuns(body)).toBe(5);
    expect(countActiveRuns(runsBody("completed", "completed"))).toBe(0);
  });

  it("**形が違えば投げる**（数えたつもりで 0 にしない）", () => {
    expect(() => countActiveRuns(JSON.stringify({ runs: [] }))).toThrow();
  });
});

describe("GitHub の API の呼び方", () => {
  it("一覧は GET、トークンは Authorization の見出しにだけ載せる", async () => {
    const { fetch, calls } = fakeFetch(() => new Response(runsBody("queued"), { status: 200 }));
    const port = createGitHubPort({ token: TOKEN, repository: REPOSITORY, fetch });
    expect(await port.activeRuns("build-features.yml")).toBe(1);
    expect(calls[0]?.url).toBe(
      "https://api.github.com/repos/owner/repo/actions/workflows/build-features.yml/runs?per_page=20",
    );
    expect(calls[0]?.init?.method).toBe("GET");
    expect(headerOf(calls[0], "authorization")).toBe(`Bearer ${TOKEN}`);
    expect(headerOf(calls[0], "x-github-api-version")).toBe("2022-11-28");
    expect(calls[0]?.url).not.toContain(TOKEN);
    expect(calls[0]?.init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("**起こすのは main で、入力は渡さない**（日付が空なら前日）", async () => {
    const { fetch, calls } = fakeFetch(() => new Response(null, { status: 204 }));
    const port = createGitHubPort({ token: TOKEN, repository: REPOSITORY, fetch });
    await port.dispatch("evaluate-daily.yml");
    expect(calls[0]?.url).toBe(
      "https://api.github.com/repos/owner/repo/actions/workflows/evaluate-daily.yml/dispatches",
    );
    expect(calls[0]?.init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({ ref: "main", inputs: {} });
  });

  it("**204 でなければ投げる**（状態を残し、本文と URL は残さない）", async () => {
    const { fetch } = fakeFetch(
      () =>
        new Response(`{"message":"Bad credentials","url":"https://api.github.com/x"}`, {
          status: 401,
        }),
    );
    const port = createGitHubPort({ token: TOKEN, repository: REPOSITORY, fetch });
    const cause = await failureOf(port.dispatch("build-features.yml"));
    expect(isJobError(cause) && cause.failure).toMatchObject({ phase: "fetch", http_status: 401 });
    expect(JSON.stringify(isJobError(cause) ? cause.failure : null)).not.toContain("https://");
  });

  it("一覧が 200 でなければ投げる", async () => {
    const { fetch } = fakeFetch(() => new Response("{}", { status: 404 }));
    const port = createGitHubPort({ token: TOKEN, repository: REPOSITORY, fetch });
    const cause = await failureOf(port.activeRuns("build-features.yml"));
    expect(isJobError(cause) && cause.failure.http_status).toBe(404);
  });

  it("**届かなければ fetch の失敗にし、メッセージのトークンを伏せる**", async () => {
    const { fetch } = fakeFetch(() => {
      throw new TypeError(`fetch failed (Authorization: Bearer ${TOKEN})`);
    });
    const port = createGitHubPort({ token: TOKEN, repository: REPOSITORY, fetch });
    const cause = await failureOf(port.activeRuns("build-features.yml"));
    expect(isJobError(cause) && cause.failure.phase).toBe("fetch");
    expect(JSON.stringify(isJobError(cause) ? cause.failure : null)).not.toContain(TOKEN);
  });

  it("一覧の本文が JSON でなければ parse の失敗", async () => {
    const { fetch } = fakeFetch(() => new Response("<html>", { status: 200 }));
    const port = createGitHubPort({ token: TOKEN, repository: REPOSITORY, fetch });
    const cause = await failureOf(port.activeRuns("evaluate-daily.yml"));
    expect(isJobError(cause) && cause.failure.phase).toBe("parse");
  });
});

describe("起こすワークフローの前提（D-50）", () => {
  const workflowText = (file: string): string =>
    readFileSync(
      fileURLToPath(new URL(`../../../../.github/workflows/${file}`, import.meta.url)),
      "utf8",
    );

  it.each(DAILY_WORKFLOWS)("%s は workflow_dispatch で起こせ、日付が空なら前日", (file) => {
    const text = workflowText(file);
    expect(text).toMatch(/^\s{2}workflow_dispatch:\s*$/m);
    expect(text).toMatch(/^\s{6}date:\s*$/m);
    expect(text).toContain("空なら前日");
    expect(text).toMatch(/required: false/);
  });
});
