/**
 * `vercel.json` の `rewrites` の**順序**を固定する。
 *
 * rewrites は上から順に評価され、最初に一致したものが勝つ。現在の設定は
 * `/(.*)` → web という総なめの 1 本を含むので、**より狭い規則をその前に置かないと
 * 全部 web に流れる**。`/ml/*` が web の catch-all に食われると、404 になるだけで
 * エラーもログも出ない。気づけない壊れ方なので機械で守る。
 *
 * 同じ理由で、収集の経路（`/api/jobs/*`）と公開 API（`/v1/*`）が web に届くことも
 * 確かめる。サービスを増やすときに、この 2 つを奪っていないかを検査する。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

type Rewrite = {
  readonly source: string;
  readonly destination: { readonly service: string };
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

const isRewrite = (value: unknown): value is Rewrite => {
  if (!isRecord(value) || typeof value["source"] !== "string") {
    return false;
  }
  const destination = value["destination"];
  return isRecord(destination) && typeof destination["service"] === "string";
};

const readRewrites = (): readonly Rewrite[] => {
  const file = fileURLToPath(new URL("../../vercel.json", import.meta.url));
  const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
  if (!isRecord(parsed) || !Array.isArray(parsed["rewrites"])) {
    throw new Error("vercel.json に rewrites の配列がありません");
  }
  const entries: readonly unknown[] = parsed["rewrites"];
  return entries.filter(isRewrite);
};

const readServices = (): readonly string[] => {
  const file = fileURLToPath(new URL("../../vercel.json", import.meta.url));
  const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
  if (!isRecord(parsed) || !isRecord(parsed["services"])) {
    throw new Error("vercel.json に services がありません");
  }
  return Object.keys(parsed["services"]);
};

/** rewrites を上から順に評価し、最初に一致したサービスを返す（Vercel と同じ規則）。 */
const routeOf = (pathname: string): string | null => {
  for (const rewrite of readRewrites()) {
    if (new RegExp(`^${rewrite.source}$`).test(pathname)) {
      return rewrite.destination.service;
    }
  }
  return null;
};

describe("vercel.json の services", () => {
  it("web と ml がある", () => {
    expect([...readServices()].sort()).toEqual(["ml", "web"]);
  });
});

describe("vercel.json の rewrites の順序", () => {
  it("総なめの規則は最後に置く", () => {
    const sources = readRewrites().map((rewrite) => rewrite.source);
    expect(sources.at(-1)).toBe("/(.*)");
    // 総なめが 1 本だけであることも確かめる。2 本あると後ろが死ぬ
    expect(sources.filter((source) => source === "/(.*)")).toHaveLength(1);
  });

  it("/ml/* は ml サービスに届く（web の catch-all に食われない）", () => {
    expect(routeOf("/ml/health")).toBe("ml");
    expect(routeOf("/ml/compact")).toBe("ml");
  });

  it("収集の経路は web のまま", () => {
    // ここが変わると収集が止まる。W2 で最も守りたい 1 行
    expect(routeOf("/api/jobs/collect/hellocycling")).toBe("web");
    expect(routeOf("/api/jobs/collect/docomo-cycle")).toBe("web");
    expect(routeOf("/api/jobs/sync-stations/hellocycling")).toBe("web");
    expect(routeOf("/api/jobs/archive-weather")).toBe("web");
  });

  it("公開 API は web のまま", () => {
    expect(routeOf("/v1/meta")).toBe("web");
    expect(routeOf("/v1/stations")).toBe("web");
  });

  it("Cron の宛先がすべてどこかのサービスに届く", () => {
    const file = fileURLToPath(new URL("../../vercel.json", import.meta.url));
    const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
    if (!isRecord(parsed) || !Array.isArray(parsed["crons"])) {
      throw new Error("vercel.json に crons がありません");
    }
    const crons: readonly unknown[] = parsed["crons"];
    for (const cron of crons) {
      if (isRecord(cron) && typeof cron["path"] === "string") {
        expect(routeOf(cron["path"]), `${cron["path"]} の宛先が無い`).not.toBeNull();
      }
    }
  });
});
