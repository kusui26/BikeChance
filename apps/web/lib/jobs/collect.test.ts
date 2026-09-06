import { afterEach, describe, expect, it, vi } from "vitest";
import { collect, isCollectSource, type CollectParams, type CollectSummary } from "./collect";
import type { BeginFetchResult, FetchLogEntry, IngestPort, IngestResult } from "./ingest-port";
import type { RawUploader } from "./storage";

/** `fetch` のモックに与える型。引数を型に含めないと mock.calls を読めない。 */
type FetchLike = (url: string | URL, init?: RequestInit) => Promise<Response>;

/** 実トークンに似せた値。テストの出力にこれが現れてはならない。 */
const TOKEN = "tok-abcdefghij0123456789";
const CONTACT_EMAIL = "dev@example.com";
const NOW = new Date("2026-09-06T06:52:00.000Z");
const LAST_UPDATED_S = 1_788_677_493; // 2026-09-06T06:51:33Z
const OBSERVED_AT = "2026-09-06T06:51:33.000Z";

const feedBody = (last_updated: number, stations = 2): string =>
  JSON.stringify({
    last_updated,
    ttl: 60,
    version: "2.3",
    data: {
      stations: Array.from({ length: stations }, (_, index) => ({
        station_id: String(index + 1),
        num_bikes_available: index,
        num_docks_available: index + 1,
        is_installed: true,
        is_renting: true,
        is_returning: true,
        last_reported: last_updated,
      })),
    },
  });

const okResponse = (body: string, headers: Record<string, string> = {}): Response =>
  new Response(body, { status: 200, headers: { etag: 'W/"new"', ...headers } });

const notModified = (): Response =>
  new Response(null, { status: 304, headers: { etag: 'W/"old"' } });

/** 呼び出しを記録する DB ポート。既定は「claim できて inserted」。 */
const recordingDb = (
  overrides: {
    claim?: Partial<BeginFetchResult>;
    ingest?: Partial<IngestResult>;
    beginThrows?: Error;
    ingestThrows?: Error;
    finishThrows?: Error;
  } = {},
): { db: IngestPort; logs: FetchLogEntry[]; ingested: unknown[] } => {
  const logs: FetchLogEntry[] = [];
  const ingested: unknown[] = [];
  const db: IngestPort = {
    beginFetch: async () => {
      if (overrides.beginThrows) throw overrides.beginThrows;
      return { claimed: true, last_etag: null, last_observed_at: null, ...overrides.claim };
    },
    ingestSnapshot: async (args) => {
      if (overrides.ingestThrows) throw overrides.ingestThrows;
      ingested.push(args);
      return {
        status: "inserted",
        n_stations: 2,
        n_new_stations: 2,
        n_changed: 2,
        array_length: 2,
        is_anomalous: false,
        ...overrides.ingest,
      };
    },
    finishFetch: async (_system, entry) => {
      if (overrides.finishThrows) throw overrides.finishThrows;
      logs.push(entry);
    },
  };
  return { db, logs, ingested };
};

const recordingUploader = (
  behaviour: { duplicate?: boolean; throws?: Error } = {},
): { upload: RawUploader; paths: string[] } => {
  const paths: string[] = [];
  const upload: RawUploader = async ({ path }) => {
    paths.push(path);
    if (behaviour.throws !== undefined) throw behaviour.throws;
    return { duplicate: behaviour.duplicate ?? false };
  };
  return { upload, paths };
};

const run = (overrides: Partial<CollectParams> & Pick<CollectParams, "db" | "upload">) =>
  collect({
    system_id: "hellocycling",
    feed: "station_status",
    source: "cron",
    token: TOKEN,
    contact_email: CONTACT_EMAIL,
    now: NOW,
    ...overrides,
  });

/** 応答とログを文字列にして、トークンが 1 バイトも混ざっていないことを確かめる。 */
const expectNoTokenAnywhere = (summary: CollectSummary, logs: readonly FetchLogEntry[]): void => {
  const serialized = JSON.stringify({ summary, logs });
  expect(serialized).not.toContain(TOKEN);
  expect(serialized).not.toContain("consumerKey=tok");
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("isCollectSource", () => {
  it("既定の 3 種類だけを受け入れる", () => {
    expect(isCollectSource("cron")).toBe(true);
    expect(isCollectSource("watchdog")).toBe(true);
    expect(isCollectSource("manual")).toBe(true);
    expect(isCollectSource("other")).toBe(false);
  });
});

describe("result の分岐（§11.6）", () => {
  it("inserted: 取得 → 保存 → 取り込み", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { db, logs, ingested } = recordingDb();
    const { upload, paths } = recordingUploader();

    const summary = await run({ db, upload });

    expect(summary.ok).toBe(true);
    expect(summary.result).toBe("inserted");
    expect(summary.observed_at).toBe(OBSERVED_AT);
    expect(summary.n_stations).toBe(2);
    expect(paths).toEqual([`hellocycling/2026/09/06/station_status_${LAST_UPDATED_S}.json.gz`]);
    expect(ingested).toHaveLength(1);
    expect(logs[0]?.result).toBe("inserted");
  });

  it("duplicate: RPC が duplicate を返しても正常系", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { db, logs } = recordingDb({ ingest: { status: "duplicate" } });
    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.ok).toBe(true);
    expect(summary.result).toBe("duplicate");
    expect(logs[0]?.ok).toBe(true);
  });

  it("locked: 同じシステムの取り込みが実行中でも正常系", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { db } = recordingDb({ ingest: { status: "locked" } });
    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.ok).toBe(true);
    expect(summary.result).toBe("locked");
  });

  it("unchanged（304）: 取得も保存もしない", async () => {
    const fetchMock = vi.fn<FetchLike>(async () => notModified());
    vi.stubGlobal("fetch", fetchMock);
    const { db, logs, ingested } = recordingDb({ claim: { last_etag: 'W/"old"' } });
    const { upload, paths } = recordingUploader();

    const summary = await run({ db, upload });

    expect(summary.result).toBe("unchanged");
    expect(summary.http_status).toBe(304);
    expect(paths).toEqual([]);
    expect(ingested).toEqual([]);
    expect(logs[0]?.ok).toBe(true);
  });

  it("unchanged（同一 last_updated）: 保存はするが取り込まない", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { db, ingested } = recordingDb({ claim: { last_observed_at: OBSERVED_AT } });
    const { upload, paths } = recordingUploader();

    const summary = await run({ db, upload });

    expect(summary.result).toBe("unchanged");
    expect(paths).toHaveLength(1);
    expect(ingested).toEqual([]);
  });

  it("unchanged（後退した last_updated）: 古いスナップショットは取り込まない", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S - 300))),
    );
    const { db, ingested } = recordingDb({ claim: { last_observed_at: OBSERVED_AT } });
    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.result).toBe("unchanged");
    expect(ingested).toEqual([]);
  });

  it("skipped_recent: claim できなければ ODPT を叩かない", async () => {
    const fetchMock = vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S)));
    vi.stubGlobal("fetch", fetchMock);
    const { db, logs } = recordingDb({ claim: { claimed: false } });

    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.result).toBe("skipped_recent");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(logs[0]?.result).toBe("skipped_recent");
  });

  it("error: ODPT が 4xx を返す", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => new Response("forbidden", { status: 403 })),
    );
    const { db, logs } = recordingDb();
    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.ok).toBe(false);
    expect(summary.result).toBe("error");
    expect(summary.http_status).toBe(403);
    expect(logs[0]?.ok).toBe(false);
  });

  it("error: 検証に失敗しても生 JSON は保存済み（W1-26）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () =>
        okResponse(
          JSON.stringify({
            last_updated: LAST_UPDATED_S,
            data: { stations: [{ station_id: "" }] },
          }),
        ),
      ),
    );
    const { db, logs, ingested } = recordingDb();
    const { upload, paths } = recordingUploader();

    const summary = await run({ db, upload });

    expect(summary.ok).toBe(false);
    expect(summary.error?.phase).toBe("parse");
    // ここが要：検証に落ちても保存は済んでいて、そのことが記録にも残る
    expect(paths).toHaveLength(1);
    expect(summary.stored_path).not.toBeNull();
    expect(ingested).toEqual([]);
    expect(logs[0]?.ok).toBe(false);
  });

  it("error: Storage が失敗したら取り込まない", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { db, ingested } = recordingDb();
    const summary = await run({
      db,
      upload: recordingUploader({ throws: new Error("bucket not found") }).upload,
    });

    expect(summary.ok).toBe(false);
    expect(summary.error?.phase).toBe("storage");
    expect(ingested).toEqual([]);
  });

  it("error: RPC が失敗しても取得と保存の事実は記録に残る", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { db, logs } = recordingDb({ ingestThrows: new Error("57014 statement timeout") });
    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.ok).toBe(false);
    expect(summary.stored_path).not.toBeNull();
    expect(summary.bytes).toBeGreaterThan(0);
    expect(logs[0]?.bytes).toBeGreaterThan(0);
  });

  it("error: begin_fetch が失敗したら ODPT を叩かない", async () => {
    const fetchMock = vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S)));
    vi.stubGlobal("fetch", fetchMock);
    const { db, logs } = recordingDb({ beginThrows: new Error("connection refused") });

    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.ok).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(logs[0]?.ok).toBe(false);
  });
});

describe("条件付き取得と残量ヘッダー（W1-4、W1-21）", () => {
  it("前回の ETag を If-None-Match に載せる", async () => {
    const fetchMock = vi.fn<FetchLike>(async () => notModified());
    vi.stubGlobal("fetch", fetchMock);
    const { db } = recordingDb({ claim: { last_etag: 'W/"400ede-abc"' } });

    await run({ db, upload: recordingUploader().upload });

    expect(fetchMock.mock.calls[0]?.[1]?.headers).toMatchObject({
      "If-None-Match": 'W/"400ede-abc"',
    });
  });

  it('cache は "no-cache" にする（これ以外だと ODPT が 304 を返さない）', async () => {
    // Fetch 仕様により、If-None-Match があると cache モードは no-store に倒れ、
    // 要求に Cache-Control: no-cache が付く。ODPT はそれを見て条件付き取得を無視し 200 を返す。
    // "no-cache" だけが max-age=0 を送り 304 になる（実測）。
    // ここが戻ると 80% の転送削減が黙って失われるので、テストで固定する
    const fetchMock = vi.fn<FetchLike>(async () => notModified());
    vi.stubGlobal("fetch", fetchMock);
    const { db } = recordingDb({ claim: { last_etag: 'W/"x"' } });

    await run({ db, upload: recordingUploader().upload });

    expect(fetchMock.mock.calls[0]?.[1]?.cache).toBe("no-cache");
  });

  it("ETag が無ければヘッダーを付けない（初回取得）", async () => {
    const fetchMock = vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S)));
    vi.stubGlobal("fetch", fetchMock);

    await run({ db: recordingDb().db, upload: recordingUploader().upload });

    expect(fetchMock.mock.calls[0]?.[1]?.headers).not.toHaveProperty("If-None-Match");
  });

  it("認証付きの応答から残量を読み、記録に載せる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () =>
        okResponse(feedBody(LAST_UPDATED_S), { "x-ratelimit-remaining-day": "23998" }),
      ),
    );
    const { db, logs } = recordingDb();
    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.ratelimit_remaining_day).toBe(23998);
    expect(logs[0]?.ratelimit_remaining_day).toBe(23998);
  });

  it("公開へフォールバックしたときは残量が null", async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockResolvedValueOnce(new Response("down", { status: 503 }))
      .mockResolvedValueOnce(okResponse(feedBody(LAST_UPDATED_S)));
    vi.stubGlobal("fetch", fetchMock);

    const summary = await run({ db: recordingDb().db, upload: recordingUploader().upload });

    expect(summary.endpoint).toBe("public");
    expect(summary.ratelimit_remaining_day).toBeNull();
  });

  it("残量ヘッダーが数値でなければ null に倒す", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () =>
        okResponse(feedBody(LAST_UPDATED_S), { "x-ratelimit-remaining-day": "unlimited" }),
      ),
    );
    const summary = await run({ db: recordingDb().db, upload: recordingUploader().upload });

    expect(summary.ratelimit_remaining_day).toBeNull();
  });
});

describe("記録（finish_fetch）", () => {
  it("source を記録に載せる（ウォッチドッグの到達を見分ける）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { db, logs } = recordingDb();
    await run({ db, upload: recordingUploader().upload, source: "watchdog" });

    expect(logs[0]?.source).toBe("watchdog");
  });

  it("正規化の警告をそのまま渡す（§11.6）", async () => {
    // 同じ station_id を 2 つ並べ、完全重複として数えられることを確かめる
    const body = JSON.stringify({
      last_updated: LAST_UPDATED_S,
      data: {
        stations: [1, 1].map(() => ({
          station_id: "dup",
          num_bikes_available: 1,
          num_docks_available: 1,
          is_installed: true,
          is_renting: true,
          is_returning: true,
          last_reported: LAST_UPDATED_S,
        })),
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(body)),
    );
    const { db, logs } = recordingDb();
    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.warnings?.["exact_duplicates"]).toBe(1);
    expect(logs[0]?.warnings?.["exact_duplicates"]).toBe(1);
  });

  it("記録に失敗しても元の結果を返す（失敗をすり替えない）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { db } = recordingDb({ finishThrows: new Error("log table missing") });

    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.ok).toBe(true);
    expect(summary.result).toBe("inserted");
  });
});

describe("トークンを漏らさない（W1-21）", () => {
  it("undici 風の cause 付き例外でも応答と記録に出ない", async () => {
    const leaky = `https://api.odpt.org/x.json?acl:consumerKey=${TOKEN}`;
    const failure = new TypeError("fetch failed");
    Object.defineProperty(failure, "cause", { value: new Error(`connect ECONNREFUSED ${leaky}`) });
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => {
        throw failure;
      }),
    );
    const { db, logs } = recordingDb();

    const summary = await run({ db, upload: recordingUploader().upload });

    expect(summary.ok).toBe(false);
    expectNoTokenAnywhere(summary, logs);
  });

  it("例外メッセージに URL が入っていても伏字になる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => {
        throw new Error(`request to https://api.odpt.org/x.json?acl:consumerKey=${TOKEN} failed`);
      }),
    );
    const { db, logs } = recordingDb();
    const summary = await run({ db, upload: recordingUploader().upload });

    expectNoTokenAnywhere(summary, logs);
    expect(JSON.stringify(summary)).toContain("acl:consumerKey=***");
  });

  it("RPC のエラーに URL が混ざっていても伏字になる", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { db, logs } = recordingDb({
      ingestThrows: new Error(`rpc failed for ?acl:consumerKey=${TOKEN}`),
    });
    const summary = await run({ db, upload: recordingUploader().upload });

    expectNoTokenAnywhere(summary, logs);
  });

  it("正常系の応答と記録にもトークンは含まれない", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchLike>(async () => okResponse(feedBody(LAST_UPDATED_S))),
    );
    const { db, logs } = recordingDb();
    const summary = await run({ db, upload: recordingUploader().upload });

    expectNoTokenAnywhere(summary, logs);
  });
});
