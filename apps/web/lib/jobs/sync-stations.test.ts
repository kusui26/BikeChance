import { afterEach, describe, expect, it, vi } from "vitest";
import type { AttributeRow, AttributesPort, UpsertAttributesResult } from "./attributes-port";
import type { RawUploader } from "./storage";
import { jobNameFor, syncStations, type SyncStationsParams } from "./sync-stations";

type FetchLike = (url: string | URL, init?: RequestInit) => Promise<Response>;

/** 実トークンに似せた値。テストの出力にこれが現れてはならない。 */
const TOKEN = "tok-abcdefghij0123456789";
const CONTACT_EMAIL = "dev@example.com";
const NOW = new Date("2026-09-08T19:00:00.000Z");
const LAST_UPDATED_S = 1_788_678_460;

const feedBody = (stations: readonly Record<string, unknown>[]): string =>
  JSON.stringify({ last_updated: LAST_UPDATED_S, ttl: 60, version: "2.3", data: { stations } });

const station = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  station_id: "1",
  name: "ポート",
  lat: 35.1,
  lon: 139.1,
  vehicle_capacity: "6",
  ...over,
});

const okResponse = (body: string): Response => new Response(body, { status: 200 });

type Recorded = {
  readonly db: AttributesPort;
  readonly rows: AttributeRow[][];
  readonly finished: { id: number; status: string; detail: Record<string, unknown> }[];
};

const recordingDb = (
  overrides: {
    upsert?: Partial<UpsertAttributesResult>;
    upsertThrows?: Error;
    startedThrows?: Error;
    finishedThrows?: Error;
  } = {},
): Recorded => {
  const rows: AttributeRow[][] = [];
  const finished: { id: number; status: string; detail: Record<string, unknown> }[] = [];
  const db: AttributesPort = {
    upsertAttributes: async ({ rows: input }) => {
      if (overrides.upsertThrows) throw overrides.upsertThrows;
      rows.push([...input]);
      return {
        status: "ok",
        n_input: input.length,
        n_new_stations: input.length,
        n_changed: 0,
        n_unchanged: 0,
        n_versions_added: input.length,
        n_geo_suspect: 0,
        n_skipped_older: 0,
        ...overrides.upsert,
      };
    },
    jobStarted: async () => {
      if (overrides.startedThrows) throw overrides.startedThrows;
      return 42;
    },
    jobFinished: async (id, status, detail) => {
      if (overrides.finishedThrows) throw overrides.finishedThrows;
      finished.push({ id, status, detail: { ...detail } });
    },
  };
  return { db, rows, finished };
};

const uploaderThat = (
  behaviour: { duplicate?: boolean; throws?: Error } = {},
): { upload: RawUploader; paths: string[] } => {
  const paths: string[] = [];
  const upload: RawUploader = async ({ path }) => {
    if (behaviour.throws) throw behaviour.throws;
    paths.push(path);
    return { duplicate: behaviour.duplicate ?? false };
  };
  return { upload, paths };
};

const paramsWith = (db: AttributesPort, upload: RawUploader): SyncStationsParams => ({
  db,
  upload,
  system_id: "hellocycling",
  token: TOKEN,
  contact_email: CONTACT_EMAIL,
  now: NOW,
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const stubFetch = (response: Response | (() => Promise<Response>)): void => {
  const impl: FetchLike =
    typeof response === "function" ? async () => response() : async () => response;
  vi.stubGlobal("fetch", vi.fn(impl));
};

describe("syncStations", () => {
  it("取得 → 保存 → 取り込み の順に進み、要約を返す", async () => {
    stubFetch(okResponse(feedBody([station(), station({ station_id: "2" })])));
    const { db, rows, finished } = recordingDb();
    const { upload, paths } = uploaderThat();

    const summary = await syncStations(paramsWith(db, upload));

    expect(summary.ok).toBe(true);
    expect(summary.status).toBe("ok");
    expect(summary.counts?.n_input).toBe(2);
    expect(rows[0]).toHaveLength(2);
    expect(paths[0]).toBe(`hellocycling/2026/09/06/station_information_${LAST_UPDATED_S}.json.gz`);
    expect(finished[0]?.status).toBe("ok");
  });

  it("観測時刻はフィードの last_updated", async () => {
    stubFetch(okResponse(feedBody([station()])));
    const { db } = recordingDb();
    const { upload } = uploaderThat();
    const summary = await syncStations(paramsWith(db, upload));
    expect(summary.observed_at).toBe(new Date(LAST_UPDATED_S * 1000).toISOString());
  });

  it("HELLO の文字列 vehicle_capacity を数値にして送る", async () => {
    stubFetch(okResponse(feedBody([station({ vehicle_capacity: "8" })])));
    const { db, rows } = recordingDb();
    const { upload } = uploaderThat();
    await syncStations(paramsWith(db, upload));
    expect(rows[0]?.[0]?.capacity).toBe(8);
  });

  it("範囲外の座標に geo_suspect を立てて送る", async () => {
    stubFetch(okResponse(feedBody([station({ lat: 35.5, lon: 39.5 })])));
    const { db, rows } = recordingDb();
    const { upload } = uploaderThat();
    const summary = await syncStations(paramsWith(db, upload));
    expect(rows[0]?.[0]?.geo_suspect).toBe(true);
    expect(summary.warnings?.["outside_japan"]).toBe(1);
  });

  it("raw に未知フィールドを載せる", async () => {
    stubFetch(okResponse(feedBody([station({ address: "東京都…" })])));
    const { db, rows } = recordingDb();
    const { upload } = uploaderThat();
    await syncStations(paramsWith(db, upload));
    expect(rows[0]?.[0]?.raw).toMatchObject({ address: "東京都…" });
  });

  it("検証に失敗しても生 JSON は保存済み（W1-26）", async () => {
    stubFetch(
      okResponse(JSON.stringify({ last_updated: LAST_UPDATED_S, data: { stations: [{}] } })),
    );
    const { db, rows, finished } = recordingDb();
    const { upload, paths } = uploaderThat();

    const summary = await syncStations(paramsWith(db, upload));

    expect(summary.ok).toBe(false);
    expect(summary.error?.phase).toBe("parse");
    expect(paths).toHaveLength(1);
    expect(summary.stored_path).not.toBeNull();
    expect(rows).toHaveLength(0);
    expect(finished[0]?.status).toBe("failed");
  });

  it("ODPT が 500 を返したら取り込まず、記録に残す", async () => {
    stubFetch(new Response("boom", { status: 500 }));
    const { db, rows, finished } = recordingDb();
    const { upload } = uploaderThat();

    const summary = await syncStations(paramsWith(db, upload));

    expect(summary.ok).toBe(false);
    expect(summary.error?.phase).toBe("fetch");
    expect(rows).toHaveLength(0);
    expect(finished[0]?.status).toBe("failed");
  });

  it("多重起動は locked として正常終了する", async () => {
    stubFetch(okResponse(feedBody([station()])));
    const { db, finished } = recordingDb({ upsert: { status: "locked" } });
    const { upload } = uploaderThat();

    const summary = await syncStations(paramsWith(db, upload));

    expect(summary.ok).toBe(true);
    expect(summary.status).toBe("locked");
    expect(summary.counts).toBeNull();
    expect(finished[0]?.status).toBe("ok");
  });

  it("同じ last_updated の再実行では保存が重複でも成功する", async () => {
    stubFetch(okResponse(feedBody([station()])));
    const { db } = recordingDb();
    const { upload } = uploaderThat({ duplicate: true });
    const summary = await syncStations(paramsWith(db, upload));
    expect(summary.ok).toBe(true);
    expect(summary.stored_path).not.toBeNull();
  });

  it("job_started が失敗しても同期は続ける（記録の不調で属性を失わない）", async () => {
    stubFetch(okResponse(feedBody([station()])));
    const { db, rows } = recordingDb({ startedThrows: new Error("db down") });
    const { upload } = uploaderThat();
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});

    const summary = await syncStations(paramsWith(db, upload));

    expect(summary.ok).toBe(true);
    expect(rows).toHaveLength(1);
    spy.mockRestore();
  });

  it("job_finished が失敗しても元の結果を返す", async () => {
    stubFetch(okResponse(feedBody([station()])));
    const { db } = recordingDb({ finishedThrows: new Error("db down") });
    const { upload } = uploaderThat();
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});

    const summary = await syncStations(paramsWith(db, upload));

    expect(summary.ok).toBe(true);
    spy.mockRestore();
  });

  it("失敗の記録にトークンが現れない（W1-21）", async () => {
    stubFetch(async () => {
      throw new Error(`connect failed: https://api.odpt.org/x?acl:consumerKey=${TOKEN}`);
    });
    const { db, finished } = recordingDb();
    const { upload } = uploaderThat();

    const summary = await syncStations(paramsWith(db, upload));

    const serialized = JSON.stringify({ summary, finished });
    expect(serialized).not.toContain(TOKEN);
    expect(serialized).not.toContain("acl:consumerKey=tok");
  });

  it("ジョブ名にシステムが入る（job_runs で見分けられる）", () => {
    expect(jobNameFor("hellocycling")).toBe("sync_stations:hellocycling");
    expect(jobNameFor("docomo-cycle")).toBe("sync_stations:docomo-cycle");
  });
});
