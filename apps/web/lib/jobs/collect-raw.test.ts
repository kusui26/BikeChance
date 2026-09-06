import { afterEach, describe, expect, it, vi } from "vitest";
import {
  WARNING_LAST_UPDATED_UNREADABLE,
  WARNING_PUBLIC_FALLBACK,
  collectRawFeed,
  type CollectParams,
  type CollectSummary,
} from "./collect-raw";
import type { RawUploader } from "./storage";

/** `fetch` のモックに与える型。引数を型に含めないと mock.calls を読めない。 */
type FetchLike = (url: string | URL, init?: RequestInit) => Promise<Response>;

/** 実トークンに似せた値。テストの出力にこれが現れてはならない。 */
const TOKEN = "tok-abcdefghij0123456789";
const CONTACT_EMAIL = "dev@example.com";
const NOW = new Date("2026-09-06T05:00:30.000Z");
/** NOW を秒に切り捨てた値。`last_updated` が読めないときのパスに使われる。 */
const NOW_EPOCH_S = 1_788_670_830;
const LAST_UPDATED_S = 1_788_670_800; // 2026-09-06T05:00:00Z

const feedBody = (last_updated: number): string =>
  JSON.stringify({ last_updated, ttl: 60, version: "2.3", data: { stations: [] } });

/** 実フィードに近い形と大きさの station_status。圧縮率の検証に使う。 */
const largeFeedBody = (last_updated: number, station_count: number): string =>
  JSON.stringify({
    last_updated,
    ttl: 60,
    version: "2.3",
    data: {
      stations: Array.from({ length: station_count }, (_, index) => ({
        station_id: String(10_000 + index),
        num_bikes_available: index % 7,
        num_docks_available: index % 5,
        is_installed: true,
        is_renting: true,
        is_returning: true,
        last_reported: last_updated - (index % 42),
      })),
    },
  });

const okResponse = (body: string): Response => new Response(body, { status: 200 });

/** 呼び出しを記録するアップローダ。既定は新規保存として振る舞う。 */
const recordingUploader = (
  behaviour: { duplicate?: boolean; throws?: Error } = {},
): { upload: RawUploader; paths: string[] } => {
  const paths: string[] = [];
  const upload: RawUploader = async ({ path }) => {
    paths.push(path);
    if (behaviour.throws !== undefined) {
      throw behaviour.throws;
    }
    return { duplicate: behaviour.duplicate ?? false };
  };
  return { upload, paths };
};

const runCollect = (
  overrides: Partial<CollectParams> & Pick<CollectParams, "upload">,
): Promise<CollectSummary> =>
  collectRawFeed({
    system_id: "hellocycling",
    feed: "station_status",
    token: TOKEN,
    contact_email: CONTACT_EMAIL,
    now: NOW,
    ...overrides,
  });

/** 応答全体を文字列にして、トークンが 1 バイトも混ざっていないことを確かめる。 */
const expectNoTokenAnywhere = (summary: CollectSummary): void => {
  const serialized = JSON.stringify(summary);
  expect(serialized).not.toContain(TOKEN);
  expect(serialized).not.toContain("consumerKey=tok");
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("collectRawFeed 正常系", () => {
  it("取得して gzip し、last_updated のパスに保存する", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { upload, paths } = recordingUploader();

    const summary = await runCollect({ upload });

    expect(summary.ok).toBe(true);
    expect(summary.result).toBe("saved");
    expect(summary.endpoint).toBe("token");
    expect(summary.observed_at).toBe("2026-09-06T05:00:00.000Z");
    expect(paths).toEqual([`hellocycling/2026/09/06/station_status_${LAST_UPDATED_S}.json.gz`]);
    expect(summary.warnings).toEqual([]);
    expect(summary.error).toBeNull();
  });

  it("原文のバイト数と gzip 後のバイト数を記録する", async () => {
    // 実フィードに近い形（1,000 ポート）で測る。数十バイトの JSON では
    // gzip のヘッダ・トレーラ分だけ増えてしまい、圧縮の検証にならない。
    const body = largeFeedBody(LAST_UPDATED_S, 1_000);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse(body)),
    );
    const summary = await runCollect({ upload: recordingUploader().upload });

    expect(summary.bytes).toBe(new TextEncoder().encode(body).byteLength);
    expect(summary.gzip_bytes).toBeGreaterThan(0);
    // 実測では HELLO の 4.2 MB が gzip 約 100 KB（約 40 分の 1）まで縮む
    expect(summary.gzip_bytes).toBeLessThan((summary.bytes ?? 0) / 5);
  });

  it("同じパスが既にあれば duplicate を正常系として返す", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const summary = await runCollect({ upload: recordingUploader({ duplicate: true }).upload });

    expect(summary.ok).toBe(true);
    expect(summary.result).toBe("duplicate");
  });

  it("認証付きエンドポイントを最初に叩き、URL にトークンを載せる", async () => {
    const fetchMock = vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S)));
    vi.stubGlobal("fetch", fetchMock);
    await runCollect({ upload: recordingUploader().upload });

    const [url] = fetchMock.mock.calls[0] ?? [];
    expect(String(url)).toContain("https://api.odpt.org/");
    // URLSearchParams を使うとコロンが %3A になり ODPT が受け付けない
    expect(String(url)).toContain("?acl:consumerKey=");
    expect(String(url)).not.toContain("%3AconsumerKey");
  });

  it("User-Agent に連絡先を載せる（ODPT から見て誰の取得か分かるようにする）", async () => {
    const fetchMock = vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S)));
    vi.stubGlobal("fetch", fetchMock);
    await runCollect({ upload: recordingUploader().upload });

    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.headers).toMatchObject({ "User-Agent": `BikeChance/0.1 (+${CONTACT_EMAIL})` });
    expect(init?.redirect).toBe("error");
  });
});

describe("collectRawFeed 壊れたフィード", () => {
  it("last_updated が読めなくても取得時刻のパスに保存する", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse("{ broken json")),
    );
    const { upload, paths } = recordingUploader();

    const summary = await runCollect({ upload });

    expect(summary.ok).toBe(true);
    expect(summary.result).toBe("saved");
    expect(summary.warnings).toContain(WARNING_LAST_UPDATED_UNREADABLE);
    expect(paths).toEqual([`hellocycling/2026/09/06/station_status_${NOW_EPOCH_S}.json.gz`]);
  });

  it("空の応答でも保存を続ける（生データを落とさない）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse("")),
    );
    const summary = await runCollect({ upload: recordingUploader().upload });

    expect(summary.ok).toBe(true);
    expect(summary.bytes).toBe(0);
  });
});

describe("collectRawFeed フォールバック", () => {
  it("認証付きが 5xx なら公開エンドポイントに 1 回だけ切り替える", async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockResolvedValueOnce(new Response("upstream down", { status: 503 }))
      .mockResolvedValueOnce(okResponse(feedBody(LAST_UPDATED_S)));
    vi.stubGlobal("fetch", fetchMock);

    const summary = await runCollect({ upload: recordingUploader().upload });

    expect(summary.ok).toBe(true);
    expect(summary.endpoint).toBe("public");
    expect(summary.warnings.join()).toContain(WARNING_PUBLIC_FALLBACK);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("api-public.odpt.org");
    expect(String(fetchMock.mock.calls[1]?.[0])).not.toContain("consumerKey");
  });

  it("ネットワーク障害でも公開エンドポイントに切り替える", async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockResolvedValueOnce(okResponse(feedBody(LAST_UPDATED_S)));
    vi.stubGlobal("fetch", fetchMock);

    const summary = await runCollect({ upload: recordingUploader().upload });

    expect(summary.ok).toBe(true);
    expect(summary.endpoint).toBe("public");
  });

  it("4xx はフォールバックせず失敗にする（トークン失効に気づけるようにする）", async () => {
    const fetchMock = vi.fn<FetchLike>(async () => new Response("forbidden", { status: 403 }));
    vi.stubGlobal("fetch", fetchMock);

    const summary = await runCollect({ upload: recordingUploader().upload });

    expect(summary.ok).toBe(false);
    expect(summary.result).toBe("error");
    expect(summary.http_status).toBe(403);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("両方失敗したら error になる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );
    const summary = await runCollect({ upload: recordingUploader().upload });

    expect(summary.ok).toBe(false);
    expect(summary.error?.error_name).toBe("OdptUnreachable");
    expect(summary.error?.phase).toBe("fetch");
  });

  it("Storage の失敗も error になる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const summary = await runCollect({
      upload: recordingUploader({ throws: new Error("bucket not found") }).upload,
    });

    expect(summary.ok).toBe(false);
    expect(summary.result).toBe("error");
  });
});

describe("collectRawFeed はトークンを漏らさない（W1-21）", () => {
  it("undici 風の cause 付き例外を注入してもトークンが出ない", async () => {
    const leakyUrl = `https://api.odpt.org/api/v4/gbfs/hellocycling/station_status.json?acl:consumerKey=${TOKEN}`;
    const cause = new Error(`connect ECONNREFUSED for ${leakyUrl}`);
    const failure = new TypeError("fetch failed");
    Object.defineProperty(failure, "cause", { value: cause });

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw failure;
      }),
    );
    const summary = await runCollect({ upload: recordingUploader().upload });

    expect(summary.ok).toBe(false);
    expectNoTokenAnywhere(summary);
  });

  it("例外メッセージそのものに URL が入っていても伏字になる", async () => {
    const message = `request to https://api.odpt.org/x.json?acl:consumerKey=${TOKEN} failed`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error(message);
      }),
    );
    const summary = await runCollect({ upload: recordingUploader().upload });

    expectNoTokenAnywhere(summary);
    expect(JSON.stringify(summary)).toContain("acl:consumerKey=***");
  });

  it("Storage のエラーに URL が混ざっていても伏字になる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const summary = await runCollect({
      upload: recordingUploader({ throws: new Error(`upload failed, token=${TOKEN}`) }).upload,
    });

    expect(summary.ok).toBe(false);
    expectNoTokenAnywhere(summary);
  });

  it("正常系の応答にもトークンは含まれない", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const summary = await runCollect({ upload: recordingUploader().upload });

    expectNoTokenAnywhere(summary);
  });
});
