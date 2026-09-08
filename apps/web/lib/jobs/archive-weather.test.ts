import { afterEach, describe, expect, it, vi } from "vitest";
import { archiveWeather, type ArchiveWeatherParams } from "./archive-weather";
import type { RawUploader } from "./storage";
import type { WeatherCell } from "./weather-fetch";
import type { WeatherPort } from "./weather-port";

type FetchLike = (url: string | URL, init?: RequestInit) => Promise<Response>;

const CONTACT_EMAIL = "dev@example.com";
/** 2026-09-08T05:17:33Z。時に丸めると 05:00 = 1788843600 */
const NOW = new Date("2026-09-08T05:17:33.000Z");
const HOUR_EPOCH = 1_788_843_600;

const cellsOf = (count: number): WeatherCell[] =>
  Array.from({ length: count }, (_, index) => ({
    lat: 35 + index * 0.05,
    lon: 139 + index * 0.0625,
  }));

/** Open-Meteo の応答（複数地点は配列で返る）。 */
const forecastBody = (count: number): string =>
  JSON.stringify(
    Array.from({ length: count }, (_, index) => ({
      latitude: 35 + index * 0.05,
      longitude: 139 + index * 0.0625,
      hourly: { time: ["2026-09-08T00:00"], precipitation: [0.0] },
    })),
  );

const okResponse = (body: string): Response => new Response(body, { status: 200 });

type Recorded = {
  readonly db: WeatherPort;
  readonly finished: { status: string; detail: Record<string, unknown> }[];
};

const recordingDb = (
  overrides: { cells?: WeatherCell[]; cellsThrow?: Error; startedThrows?: Error } = {},
): Recorded => {
  const finished: { status: string; detail: Record<string, unknown> }[] = [];
  const db: WeatherPort = {
    listGridCells: async () => {
      if (overrides.cellsThrow) throw overrides.cellsThrow;
      return overrides.cells ?? cellsOf(3);
    },
    jobStarted: async () => {
      if (overrides.startedThrows) throw overrides.startedThrows;
      return 7;
    },
    jobFinished: async (_id, status, detail) => {
      finished.push({ status, detail: { ...detail } });
    },
  };
  return { db, finished };
};

const uploaderThat = (
  behaviour: { duplicate?: boolean; throwsOnBatch?: number } = {},
): { upload: RawUploader; paths: string[] } => {
  const paths: string[] = [];
  let calls = 0;
  const upload: RawUploader = async ({ path }) => {
    // 「何回目の呼び出しで落とすか」で数える。paths.length だと落ちた回が数に入らず、
    // 以降ずっと落ち続けてしまう
    const current = calls;
    calls += 1;
    if (behaviour.throwsOnBatch !== undefined && current === behaviour.throwsOnBatch) {
      throw new Error("storage down");
    }
    paths.push(path);
    return { duplicate: behaviour.duplicate ?? false };
  };
  return { upload, paths };
};

const paramsWith = (db: WeatherPort, upload: RawUploader): ArchiveWeatherParams => ({
  db,
  upload,
  gzip: async (body) => body.slice(0, Math.max(1, Math.floor(body.byteLength / 4))),
  contact_email: CONTACT_EMAIL,
  now: NOW,
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const stubFetch = (impl: FetchLike): void => {
  vi.stubGlobal("fetch", vi.fn(impl));
};

describe("archiveWeather", () => {
  it("格子を取得し、分割ごとに保存して要約を返す", async () => {
    stubFetch(async () => okResponse(forecastBody(3)));
    const { db, finished } = recordingDb();
    const { upload, paths } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.ok).toBe(true);
    expect(summary.n_cells).toBe(3);
    expect(summary.n_batches).toBe(1);
    expect(summary.n_saved).toBe(1);
    expect(paths).toEqual(["2026/09/08/jma_msm_1788843600_00.json.gz"]);
    expect(finished[0]?.status).toBe("ok");
  });

  it("時に丸めたパスに保存する（同じ時間の再実行が同じ場所に写像される）", async () => {
    stubFetch(async () => okResponse(forecastBody(3)));
    const { db } = recordingDb();
    const { upload } = uploaderThat();
    const summary = await archiveWeather(paramsWith(db, upload));
    expect(summary.hour_epoch_s).toBe(HOUR_EPOCH);
  });

  it("595 格子は 6 分割になる", async () => {
    stubFetch(async (_url) => {
      // 要求した地点数と同じ数を返す
      const url = String(_url);
      const count = (new URL(url).searchParams.get("latitude") ?? "").split(",").length;
      return okResponse(forecastBody(count));
    });
    const { db } = recordingDb({ cells: cellsOf(595) });
    const { upload, paths } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.n_batches).toBe(6);
    expect(paths).toHaveLength(6);
    expect(paths.at(-1)).toContain("_05.json.gz");
    expect(summary.ok).toBe(true);
  });

  it("既に同じパスがあれば重複として扱い、失敗にしない", async () => {
    stubFetch(async () => okResponse(forecastBody(3)));
    const { db } = recordingDb();
    const { upload } = uploaderThat({ duplicate: true });

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.ok).toBe(true);
    expect(summary.n_duplicate).toBe(1);
    expect(summary.n_saved).toBe(0);
  });

  it("要求と応答の地点数が違えば、その分割を失敗にする", async () => {
    stubFetch(async () => okResponse(forecastBody(2))); // 3 要求して 2 しか返らない
    const { db, finished } = recordingDb();
    const { upload } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.ok).toBe(false);
    expect(summary.n_failed).toBe(1);
    expect(finished[0]?.status).toBe("failed");
    expect(JSON.stringify(finished[0]?.detail)).toContain("LocationCountMismatch");
  });

  it("1 分割が落ちても、成功した分割は保存済みのまま残る", async () => {
    stubFetch(async (_url) => {
      const count = (new URL(String(_url)).searchParams.get("latitude") ?? "").split(",").length;
      return okResponse(forecastBody(count));
    });
    const { db } = recordingDb({ cells: cellsOf(250) }); // 3 分割
    const { upload, paths } = uploaderThat({ throwsOnBatch: 1 });

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.n_batches).toBe(3);
    expect(summary.n_failed).toBe(1);
    expect(paths).toHaveLength(2); // 落ちた 1 つ以外は保存されている
    expect(summary.ok).toBe(false); // 一部欠けた時間を成功と記録しない
  });

  it("Open-Meteo が 500 を返したら失敗として記録する", async () => {
    stubFetch(async () => new Response("boom", { status: 500 }));
    const { db, finished } = recordingDb();
    const { upload, paths } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.ok).toBe(false);
    expect(paths).toHaveLength(0);
    expect(finished[0]?.status).toBe("failed");
  });

  it("格子が 0 件なら失敗にする（黙って成功にしない）", async () => {
    stubFetch(async () => okResponse(forecastBody(0)));
    const { db, finished } = recordingDb({ cells: [] });
    const { upload } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.ok).toBe(false);
    expect(summary.error?.error_name).toBe("NoWeatherGridCells");
    expect(finished[0]?.status).toBe("failed");
  });

  it("格子の取得に失敗しても記録は残す", async () => {
    stubFetch(async () => okResponse(forecastBody(3)));
    const { db, finished } = recordingDb({ cellsThrow: new Error("db down") });
    const { upload } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.ok).toBe(false);
    expect(finished[0]?.status).toBe("failed");
  });

  it("job_started が失敗してもアーカイブは続ける（記録の不調で予報を失わない）", async () => {
    stubFetch(async () => okResponse(forecastBody(3)));
    const { db } = recordingDb({ startedThrows: new Error("db down") });
    const { upload, paths } = uploaderThat();
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.ok).toBe(true);
    expect(paths).toHaveLength(1);
    spy.mockRestore();
  });

  it("要求 URL にモデルと変数が入る", async () => {
    const seen: string[] = [];
    stubFetch(async (url) => {
      seen.push(String(url));
      return okResponse(forecastBody(3));
    });
    const { db } = recordingDb();
    const { upload } = uploaderThat();

    await archiveWeather(paramsWith(db, upload));

    // jma_msm は日本で最も細かいが降水確率を返さない。best_match だけが返すので両方取る
    expect(seen[0]).toContain("jma_msm");
    expect(seen[0]).toContain("best_match");
    expect(seen[0]).toContain("precipitation_probability");
    // 窓は JST 当日 0 時起点なので、3 日 = 72 時間。jma_msm が欠けない最大（W3 §12 の 77）
    expect(seen[0]).toContain("forecast_days=3");
  });
});
