import {
  ARCHIVE_WEATHER_MAX_DURATION_S,
  WEATHER_FETCH_TIMEOUT_MS,
  WEATHER_LOCATIONS_PER_MINUTE,
  WEATHER_RATE_WINDOW_MS,
  WEATHER_RECORD_MARGIN_MS,
  WEATHER_RETRY_BACKOFF_MS,
} from "@bikechance/shared";
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

/**
 * 待ちを記録して、実際には待たない。**本当に 1 分待つ試験は書かれなくなる**ので、
 * 「何ミリ秒で呼ばれたか」を見て待ちが挟まったことを確かめる。
 */
const recordingSleep = (): { sleep: (ms: number) => Promise<void>; waits: number[] } => {
  const waits: number[] = [];
  return {
    sleep: async (ms) => {
      waits.push(ms);
    },
    waits,
  };
};

const paramsWith = (
  db: WeatherPort,
  upload: RawUploader,
  sleep?: (ms: number) => Promise<void>,
): ArchiveWeatherParams => ({
  db,
  upload,
  gzip: async (body) => body.slice(0, Math.max(1, Math.floor(body.byteLength / 4))),
  contact_email: CONTACT_EMAIL,
  now: NOW,
  sleep: sleep ?? (async () => {}),
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

describe("投げる速さ（Open-Meteo の 1 分 600 地点）", () => {
  const everyBatchOk = (): void => {
    stubFetch(async (url) => {
      const count = (new URL(String(url)).searchParams.get("latitude") ?? "").split(",").length;
      return okResponse(forecastBody(count));
    });
  };

  it("**600 格子までは待たない**（いままで通っていた大きさ）", async () => {
    everyBatchOk();
    const { db } = recordingDb({ cells: cellsOf(500) });
    const { upload } = uploaderThat();
    const { sleep, waits } = recordingSleep();
    const summary = await archiveWeather(paramsWith(db, upload, sleep));
    expect(summary.ok).toBe(true);
    expect(waits).toEqual([]);
    expect(summary.waited_ms).toBe(0);
  });

  it("**601 格子で窓をまたぐ**（ここが 429 になっていた）", async () => {
    everyBatchOk();
    const { db } = recordingDb({ cells: cellsOf(601) });
    const { upload } = uploaderThat();
    const { sleep, waits } = recordingSleep();
    const summary = await archiveWeather(paramsWith(db, upload, sleep));
    expect(summary.ok).toBe(true);
    expect(waits).toEqual([WEATHER_RATE_WINDOW_MS]);
    expect(summary.waited_ms).toBe(WEATHER_RATE_WINDOW_MS);
  });

  it("**どの窓も 500 地点を超えない**（実際に投げた順で数える）", async () => {
    const sent: number[] = [];
    stubFetch(async (url) => {
      const count = (new URL(String(url)).searchParams.get("latitude") ?? "").split(",").length;
      sent.push(count);
      return okResponse(forecastBody(count));
    });
    const { db } = recordingDb({ cells: cellsOf(602) });
    const { upload } = uploaderThat();
    const waits: number[] = [];
    const sleep = async (ms: number): Promise<void> => {
      // 待った位置を「そこまでに投げた数」で記録する
      waits.push(sent.length);
      await Promise.resolve(ms);
    };
    await archiveWeather(paramsWith(db, upload, sleep));

    expect(sent).toEqual([100, 100, 100, 100, 100, 100, 2]);
    expect(waits).toEqual([5]); // 5 分割（500 地点）投げた直後に待つ
    let spent = 0;
    for (const [index, size] of sent.entries()) {
      if (waits.includes(index)) {
        spent = 0;
      }
      spent += size;
      expect(spent).toBeLessThanOrEqual(WEATHER_LOCATIONS_PER_MINUTE);
    }
  });

  it("待った時間は job_runs の記録に残る", async () => {
    everyBatchOk();
    const { db, finished } = recordingDb({ cells: cellsOf(602) });
    const { upload } = uploaderThat();
    const { sleep } = recordingSleep();
    await archiveWeather(paramsWith(db, upload, sleep));
    const detail = finished.at(-1)?.detail;
    expect(detail?.waited_ms).toBe(WEATHER_RATE_WINDOW_MS);
    expect(detail?.n_cells).toBe(602);
  });
});

describe("取り直し（W7-01、W6 プランの所見 223）", () => {
  /** 本物の時計の代わり。**待ちと取得の所要だけで進む**ので、時間の予算を決まった値で試せる。 */
  const fakeClock = (): { clock: () => number; advance: (ms: number) => void } => {
    const time = { ms: 0 };
    return { clock: () => time.ms, advance: (ms) => (time.ms += ms) };
  };

  const timeoutError = (): DOMException =>
    new DOMException("The operation was aborted due to timeout", "TimeoutError");

  const countOf = (url: string | URL): number =>
    (new URL(String(url)).searchParams.get("latitude") ?? "").split(",").length;

  /** 何回目の要求を落とすかを決めて、落とさない要求は応答する。投げた地点の数も残す。 */
  const fetchFailingOn = (
    failingCalls: ReadonlySet<number>,
    failure: () => Response | Promise<Response>,
  ): { sent: number[] } => {
    const sent: number[] = [];
    stubFetch(async (url) => {
      const call = sent.length;
      sent.push(countOf(url));
      if (failingCalls.has(call)) {
        return failure();
      }
      return okResponse(forecastBody(countOf(url)));
    });
    return { sent };
  };

  it("**1 分割の時間切れは、同じ回の取り直しで埋まる**（10/17 00:17・07:17 の形）", async () => {
    const { sent } = fetchFailingOn(new Set([0]), () => {
      throw timeoutError();
    });
    const { db, finished } = recordingDb();
    const { upload, paths } = uploaderThat();
    const { sleep, waits } = recordingSleep();

    const summary = await archiveWeather(paramsWith(db, upload, sleep));

    expect(sent).toEqual([3, 3]);
    expect(waits).toEqual([WEATHER_RETRY_BACKOFF_MS]); // 取り直しの前に 1 度だけ待つ
    expect(summary.ok).toBe(true);
    expect(summary.n_retried).toBe(1);
    expect(summary.n_recovered).toBe(1);
    expect(summary.batches[0]?.attempts).toBe(2);
    expect(paths).toEqual([`2026/09/08/jma_msm_${HOUR_EPOCH}_00.json.gz`]);
    expect(finished[0]?.status).toBe("ok");
    expect(finished[0]?.detail).toMatchObject({ n_retried: 1, n_recovered: 1, n_skipped: 0 });
  });

  /** ヘッダは返り、**本文の途中で時間切れ**になる応答（9/26・10/17 12:17 の形）。 */
  const stalledBody = (): Response =>
    new Response(
      new ReadableStream<Uint8Array>({
        start: (controller) => {
          controller.error(timeoutError());
        },
      }),
      { status: 200 },
    );

  it("**本文の途中の時間切れも、取得の失敗として取り直す**（所見 227：`unknown` に分類されて漏れていた）", async () => {
    const { sent } = fetchFailingOn(new Set([0]), stalledBody);
    const { db, finished } = recordingDb();
    const { upload } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(sent).toEqual([3, 3]);
    expect(summary.ok).toBe(true);
    expect(summary.n_recovered).toBe(1);
    expect(finished[0]?.status).toBe("ok");
  });

  it("本文の途中の時間切れが続けば、`fetch/TimeoutError` として記録に残す（`unknown` にしない）", async () => {
    fetchFailingOn(new Set([0, 1]), stalledBody);
    const { db } = recordingDb();
    const { upload } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.n_retried).toBe(1);
    expect(summary.batches[0]?.error).toMatch(/^fetch\/TimeoutError: /);
  });

  it("5xx は取り直す。**取り直しても落ちれば failed のまま**", async () => {
    fetchFailingOn(new Set([0, 1]), () => new Response("boom", { status: 503 }));
    const { db, finished } = recordingDb();
    const { upload } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(summary.ok).toBe(false);
    expect(summary.n_retried).toBe(1);
    expect(summary.n_recovered).toBe(0);
    expect(finished[0]?.status).toBe("failed");
  });

  it("**4xx（429 を含む）は取り直さない**——速さの枠を越えた印で、すぐ投げ直すと悪くなる", async () => {
    const { sent } = fetchFailingOn(new Set([0]), () => new Response("slow down", { status: 429 }));
    const { db } = recordingDb();
    const { upload } = uploaderThat();

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(sent).toHaveLength(1);
    expect(summary.n_retried).toBe(0);
    expect(summary.ok).toBe(false);
  });

  it("保存の失敗は取り直さない（取得は通っているので、投げ直すと速さの予算を無駄に使う）", async () => {
    const { sent } = fetchFailingOn(new Set(), () => okResponse("[]"));
    const { db } = recordingDb();
    const { upload } = uploaderThat({ throwsOnBatch: 0 });

    const summary = await archiveWeather(paramsWith(db, upload));

    expect(sent).toHaveLength(1);
    expect(summary.n_retried).toBe(0);
    expect(summary.ok).toBe(false);
  });

  it("**取り直しも速さの予算を守る**——最初の回と同じ 1 分の枠で数える", async () => {
    // 602 格子：最初の 5 分割が時間切れ。取り直しは 2 つ目の窓の残り（500 − 102）に 3 つ入り、
    // 4 つ目の前でもう 1 度窓をまたぐ
    const { sent } = fetchFailingOn(new Set([0, 1, 2, 3, 4]), () => {
      throw timeoutError();
    });
    const { db } = recordingDb({ cells: cellsOf(602) });
    const { upload } = uploaderThat();
    const { sleep, waits } = recordingSleep();

    const summary = await archiveWeather(paramsWith(db, upload, sleep));

    expect(sent).toEqual([100, 100, 100, 100, 100, 100, 2, 100, 100, 100, 100, 100]);
    expect(waits).toEqual([
      WEATHER_RATE_WINDOW_MS,
      WEATHER_RETRY_BACKOFF_MS,
      WEATHER_RATE_WINDOW_MS,
    ]);
    expect(summary.ok).toBe(true);
    expect(summary.n_recovered).toBe(5);
  });

  it("**全分割が時間切れでも上限を越えない**：取り直しは、始めても上限に収まるものだけ（10/17 09:17 の形）", async () => {
    const { clock, advance } = fakeClock();
    stubFetch(async () => {
      advance(WEATHER_FETCH_TIMEOUT_MS);
      throw timeoutError();
    });
    const { db, finished } = recordingDb({ cells: cellsOf(617) });
    const { upload } = uploaderThat();

    const summary = await archiveWeather({
      ...paramsWith(db, upload, async (ms) => advance(ms)),
      clock,
    });

    // 最初の回：待ち 65 秒 ＋ 7 × 15 秒 ＝ 170 秒。取り直しは 2 つ目の窓の残りに 3 つ（待ち 10 秒）、
    // 4 つ目は窓をまたぐ 65 秒を待つと上限に収まらないので投げない
    expect(summary.n_retried).toBe(3);
    expect(summary.n_recovered).toBe(0);
    expect(summary.duration_ms).toBe(
      170_000 + WEATHER_RETRY_BACKOFF_MS + 3 * WEATHER_FETCH_TIMEOUT_MS,
    );
    expect(summary.duration_ms + WEATHER_RECORD_MARGIN_MS).toBeLessThanOrEqual(
      ARCHIVE_WEATHER_MAX_DURATION_S * 1000,
    );
    expect(finished[0]?.status).toBe("failed");
  });

  it("最初の回でも、時間の予算が足りなければ投げずに failed として残す（黙って欠けさせない）", async () => {
    const { clock, advance } = fakeClock();
    const { sent } = fetchFailingOn(new Set(), () => okResponse("[]"));
    const slowDb: WeatherPort = {
      ...recordingDb().db,
      listGridCells: async () => {
        advance(ARCHIVE_WEATHER_MAX_DURATION_S * 1000 - WEATHER_RECORD_MARGIN_MS); // DB が遅く、もう余裕が無い
        return cellsOf(3);
      },
    };
    const { upload } = uploaderThat();

    const summary = await archiveWeather({ ...paramsWith(slowDb, upload), clock });

    expect(sent).toHaveLength(0);
    expect(summary.n_skipped).toBe(1);
    expect(summary.ok).toBe(false);
    expect(summary.batches[0]?.error).toContain("TimeBudget");
  });

  it("**飛ばしたら後ろは投げない**——飛ばした分割の待ちを前提に後ろの待ちが決まっている", async () => {
    // 602 格子：5 分割を投げると 1 つ 40 秒かかって 200 秒。6 つ目は 65 秒の待ちで収まらず、
    // 7 つ目は待ち 0 なら収まって見えるが、投げると 1 分の枠を越える
    const { clock, advance } = fakeClock();
    const sent: number[] = [];
    stubFetch(async (url) => {
      sent.push(countOf(url));
      advance(40_000);
      return okResponse(forecastBody(countOf(url)));
    });
    const { db } = recordingDb({ cells: cellsOf(602) });
    const { upload } = uploaderThat();

    const summary = await archiveWeather({
      ...paramsWith(db, upload, async (ms) => advance(ms)),
      clock,
    });

    expect(sent).toEqual([100, 100, 100, 100, 100]);
    expect(summary.n_skipped).toBe(2);
    expect(summary.waited_ms).toBe(0); // 投げなかった分割の前の待ちは数えない
  });

  it("取り直しが無い回も、数（0）を記録に残す（見張りが数えられるように）", async () => {
    fetchFailingOn(new Set(), () => okResponse("[]"));
    const { db, finished } = recordingDb();
    const { upload } = uploaderThat();

    await archiveWeather(paramsWith(db, upload));

    expect(finished[0]?.detail).toMatchObject({ n_retried: 0, n_recovered: 0, n_skipped: 0 });
  });
});
