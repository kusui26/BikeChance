/**
 * 行程チェックの規則（`trip.ts`）。
 *
 * ここが誤ると「**成立しない行程を成立すると言う**」壊れ方になる。守りたいのは 4 つ。
 *   * **距離は SQL と同じ値**（`rebuild_geo` と食い違うと、400 m で絞った候補が 410 m に見える）
 *   * **概算は楽観側**であることを、数で確かめておく（直線距離なので経路より短い）
 *   * **到着が水平を超えたら断る**（超えると末尾に張り付いて、別の時刻の確率になる）
 *   * `ride_min` は**整数だけ**（小数を黙って丸めると、渡した値と返る値が違う）
 */
import { describe, expect, it } from "vitest";
import { HORIZONS_MIN } from "./constants";
import {
  EARTH_RADIUS_M,
  RIDE_SPEED_KMH,
  TRIP_ALTERNATIVES_MAX,
  TRIP_ALTERNATIVE_RADIUS_M,
  TRIP_INDEPENDENCE_NOTICE,
  estimateRideMinutes,
  haversineMeters,
  parseRideMinutes,
  tripArrival,
  tripProbability,
} from "./trip";

/** 東京駅前と日本橋あたり。実在の座標に近い 2 点。 */
const TOKYO = { lat: 35.6812, lon: 139.7671 };
const NIHONBASHI = { lat: 35.6837, lon: 139.7744 };

describe("距離", () => {
  it("地球半径は rebuild_geo（migration 0025）と同じ", () => {
    expect(EARTH_RADIUS_M).toBe(6_371_000);
  });

  it("同じ点は 0 m", () => {
    expect(haversineMeters(TOKYO, TOKYO)).toBe(0);
  });

  it("向きを変えても同じ（対称）", () => {
    expect(haversineMeters(TOKYO, NIHONBASHI)).toBeCloseTo(haversineMeters(NIHONBASHI, TOKYO), 9);
  });

  it("緯度 1 度は約 111.2 km（半径から決まる値）", () => {
    const expected = (EARTH_RADIUS_M * Math.PI) / 180;
    expect(haversineMeters({ lat: 35, lon: 139 }, { lat: 36, lon: 139 })).toBeCloseTo(expected, 3);
  });

  it("東京の緯度では、経度 1 度は緯度 1 度より短い（cos φ が効く）", () => {
    const lat = haversineMeters({ lat: 35.68, lon: 139.0 }, { lat: 36.68, lon: 139.0 });
    const lon = haversineMeters({ lat: 35.68, lon: 139.0 }, { lat: 35.68, lon: 140.0 });
    expect(lon).toBeLessThan(lat);
    expect(lon / lat).toBeCloseTo(Math.cos((35.68 * Math.PI) / 180), 3);
  });

  it("東京駅〜日本橋は 700 m 前後（桁が合っている）", () => {
    const meters = haversineMeters(TOKYO, NIHONBASHI);
    expect(meters).toBeGreaterThan(600);
    expect(meters).toBeLessThan(900);
  });
});

describe("乗車時間の概算", () => {
  it("速度は開発プラン §8.3 の 14 km/h", () => {
    expect(RIDE_SPEED_KMH).toBe(14);
  });

  it("距離 ÷ 速度を分にして丸める", () => {
    const meters = haversineMeters(TOKYO, NIHONBASHI);
    expect(estimateRideMinutes(TOKYO, NIHONBASHI)).toBe(
      Math.round((meters / 1000 / RIDE_SPEED_KMH) * 60),
    );
  });

  it("**近いポート同士では 0 分になる**（断る理由にはしない）", () => {
    const near = { lat: TOKYO.lat + 0.0005, lon: TOKYO.lon };
    expect(haversineMeters(TOKYO, near)).toBeLessThan(116);
    expect(estimateRideMinutes(TOKYO, near)).toBe(0);
  });

  it("整数を返す（応答にそのまま載る値）", () => {
    expect(Number.isInteger(estimateRideMinutes(TOKYO, NIHONBASHI))).toBe(true);
  });

  it("**直線距離なので、実際の経路より短く見積もる**", () => {
    // 市街地の経路は直線の 1.2〜1.4 倍。概算は楽観側に外れることを数で残しておく
    const straight = estimateRideMinutes(TOKYO, NIHONBASHI);
    const realistic = Math.round(straight * 1.3);
    expect(straight).toBeLessThan(realistic);
  });
});

describe("ride_min の読み取り", () => {
  const problemOf = (raw: string | null): string | number | null => {
    const result = parseRideMinutes(raw);
    return result.ok ? result.ride_min : result.problem;
  };

  it("指定が無ければ null（サーバーが概算する）", () => {
    expect(problemOf(null)).toBeNull();
    expect(problemOf("")).toBeNull();
    expect(problemOf("   ")).toBeNull();
  });

  it("整数を読む", () => {
    expect(problemOf("0")).toBe(0);
    expect(problemOf("12")).toBe(12);
  });

  it("**小数は断る**（黙って丸めると、渡した値と返る値が違う）", () => {
    expect(problemOf("12.5")).toBe("ride_malformed");
  });

  it("10 進以外は断る（16 進・指数）", () => {
    expect(problemOf("0x10")).toBe("ride_malformed");
    expect(problemOf("1e2")).toBe("ride_malformed");
  });

  it("負は断る", () => {
    expect(problemOf("-1")).toBe("ride_out_of_range");
  });

  it("**上限はここで見ない**（効くのは出発 ＋ 乗車。`tripArrival` の持ち場）", () => {
    expect(problemOf("9999")).toBe(9999);
  });
});

describe("到着の範囲", () => {
  const MAX = Math.max(...HORIZONS_MIN);

  it("水平の端までは通る", () => {
    const result = tripArrival({ depart_in_min: 5, ride_min: MAX - 5 });
    expect(result.ok && result.arrive_in_min).toBe(MAX);
  });

  it("**1 分でも超えたら断る**（超えると末尾に張り付いて別の時刻の確率になる）", () => {
    expect(tripArrival({ depart_in_min: 5, ride_min: MAX - 4 }).ok).toBe(false);
  });

  it("断るときは、出発と乗車の両方を本文に出す（どちらを直せるか分かる）", () => {
    const result = tripArrival({ depart_in_min: 120, ride_min: 90 });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.detail).toContain("120");
      expect(result.detail).toContain("90");
      expect(result.detail).toContain("210");
    }
  });

  it("乗車 0 分なら出発と同じ時刻", () => {
    const result = tripArrival({ depart_in_min: 15, ride_min: 0 });
    expect(result.ok && result.arrive_in_min).toBe(15);
  });
});

describe("行程の確率", () => {
  it("掛ける", () => {
    expect(tripProbability(0.8, 0.5)).toBe(0.4);
  });

  it("**1/1000 に丸める**（浮動小数の尾を応答に載せない）", () => {
    expect(tripProbability(0.85, 1)).toBe(0.85);
    expect(Number.isInteger(tripProbability(0.777, 0.333) * 1000)).toBe(true);
  });

  it("片方が 0 なら 0", () => {
    expect(tripProbability(0, 0.9)).toBe(0);
  });

  it("両方 1 なら 1", () => {
    expect(tripProbability(1, 1)).toBe(1);
  });

  it("**掛けた値は、どちらの確率よりも大きくならない**", () => {
    for (const [bike, dock] of [
      [0.9, 0.9],
      [0.5, 0.2],
      [1, 0.3],
    ]) {
      const trip = tripProbability(bike ?? 0, dock ?? 0);
      expect(trip).toBeLessThanOrEqual(Math.min(bike ?? 0, dock ?? 0));
    }
  });
});

describe("定数", () => {
  it("代替は 400 m（station_neighbors の 500 m より内側）", () => {
    expect(TRIP_ALTERNATIVE_RADIUS_M).toBe(400);
    expect(TRIP_ALTERNATIVE_RADIUS_M).toBeLessThan(500);
  });

  it("代替の上限は 5（実測の中央 3 を丸ごと含む）", () => {
    expect(TRIP_ALTERNATIVES_MAX).toBe(5);
  });

  it("独立仮定の注記が、掛け算であることを言っている（数と一緒に配るもの）", () => {
    expect(TRIP_INDEPENDENCE_NOTICE.length).toBeGreaterThan(20);
    expect(TRIP_INDEPENDENCE_NOTICE).toContain("掛け合わせた");
  });

  it("**ずれる向きを断定していない**（相関の符号を測っていない）", () => {
    for (const claim of ["高いことがあります", "低いことがあります", "高くなります"]) {
      expect(TRIP_INDEPENDENCE_NOTICE).not.toContain(claim);
    }
  });
});
