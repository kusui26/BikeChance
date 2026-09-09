/**
 * 到着の指定と補間（`forecast.ts`）。
 *
 * ここが誤ると「**別の時刻の確率を、指定した時刻の確率として出す**」という、利用者から
 * 見て気づけない壊れ方になる。守りたいのは 4 つ。
 *   * 指定が無ければ予測を返さない（既定の応答を変えない）
 *   * **範囲の検査は丸める前**（`in_min=3` を 5 に化けさせない）
 *   * `at` はタイムゾーン必須（素の日時を UTC と決めつけると 9 時間ずれる）
 *   * **欠けた表から数を作らない**（配列がそろわなければ null）
 */
import { describe, expect, it } from "vitest";
import { HORIZONS_MIN } from "./constants";
import {
  ARRIVAL_MAX_MIN,
  ARRIVAL_MIN_MIN,
  ARRIVAL_STEP_MIN,
  forecastHorizon,
  interpolateForecast,
  parseArrival,
  roundArrival,
} from "./forecast";

const NOW = new Date("2026-09-09T12:00:00.000Z");

const parse = (params: { at?: string | null; in_min?: string | null }) =>
  parseArrival({ at: params.at ?? null, in_min: params.in_min ?? null, now: NOW });

/** 失敗の種類だけを取り出す。通ったときは丸めた分を返す。 */
const resultOf = (params: {
  at?: string | null;
  in_min?: string | null;
}): string | number | null => {
  const result = parse(params);
  return result.ok ? result.in_min : result.problem;
};

describe("受け付ける範囲", () => {
  it("水平の端に合わせる（持っていない先を作らない）", () => {
    expect(ARRIVAL_MIN_MIN).toBe(Math.min(...HORIZONS_MIN));
    expect(ARRIVAL_MAX_MIN).toBe(Math.max(...HORIZONS_MIN));
    expect(ARRIVAL_MAX_MIN).toBe(180);
  });

  it("刻みは 5 分（時間グリッドと同じ）", () => {
    expect(ARRIVAL_STEP_MIN).toBe(5);
    expect(ARRIVAL_MIN_MIN % ARRIVAL_STEP_MIN).toBe(0);
    expect(ARRIVAL_MAX_MIN % ARRIVAL_STEP_MIN).toBe(0);
  });
});

describe("roundArrival", () => {
  it("5 分刻みに丸める", () => {
    expect(roundArrival(32)).toBe(30);
    expect(roundArrival(33)).toBe(35);
    expect(roundArrival(30)).toBe(30);
  });

  it("丸めた値は必ず 5 の倍数（同じ分の要求が同じ URL になる）", () => {
    for (let minutes = ARRIVAL_MIN_MIN; minutes <= ARRIVAL_MAX_MIN; minutes += 0.5) {
      expect(roundArrival(minutes) % ARRIVAL_STEP_MIN).toBe(0);
    }
  });
});

describe("parseArrival — 指定が無い / 重なる", () => {
  it("どちらも無ければ予測を返さない（既定の応答を変えない）", () => {
    expect(resultOf({})).toBeNull();
  });

  it("空文字は指定なしと同じ（`?in_min=` だけ付いた URL）", () => {
    expect(resultOf({ in_min: "" })).toBeNull();
    expect(resultOf({ at: "  " })).toBeNull();
  });

  it("両方来たら断る（片方を黙って無視しない）", () => {
    expect(resultOf({ at: "2026-09-09T12:30:00Z", in_min: "30" })).toBe("arrival_conflict");
  });

  it("両方来たら、中身が壊れていても先に conflict を返す", () => {
    expect(resultOf({ at: "でたらめ", in_min: "30" })).toBe("arrival_conflict");
  });
});

describe("parseArrival — in_min", () => {
  it("そのままの分を返す", () => {
    expect(resultOf({ in_min: "30" })).toBe(30);
  });

  it("5 分刻みに丸める", () => {
    expect(resultOf({ in_min: "37" })).toBe(35);
    expect(resultOf({ in_min: "32" })).toBe(30);
  });

  it("**範囲の検査は丸める前**（3 分を 5 分に化けさせない）", () => {
    expect(resultOf({ in_min: "3" })).toBe("arrival_out_of_range");
    expect(resultOf({ in_min: "4" })).toBe("arrival_out_of_range");
  });

  it("端はちょうど通る", () => {
    expect(resultOf({ in_min: String(ARRIVAL_MIN_MIN) })).toBe(ARRIVAL_MIN_MIN);
    expect(resultOf({ in_min: String(ARRIVAL_MAX_MIN) })).toBe(ARRIVAL_MAX_MIN);
  });

  it("端の外は 400（末尾の値に張り付けて返さない）", () => {
    expect(resultOf({ in_min: "181" })).toBe("arrival_out_of_range");
    expect(resultOf({ in_min: "300" })).toBe("arrival_out_of_range");
    expect(resultOf({ in_min: "0" })).toBe("arrival_out_of_range");
    expect(resultOf({ in_min: "-30" })).toBe("arrival_out_of_range");
  });

  it("範囲外の応答には受け取った値を書く（どれだけ外れたか分かる）", () => {
    const result = parse({ in_min: "300" });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.detail).toContain("300");
      expect(result.detail).toContain(String(ARRIVAL_MAX_MIN));
    }
  });

  it("数でなければ 400", () => {
    expect(resultOf({ in_min: "abc" })).toBe("arrival_malformed");
    expect(resultOf({ in_min: "30分" })).toBe("arrival_malformed");
    expect(resultOf({ in_min: "NaN" })).toBe("arrival_malformed");
  });

  it("10 進の数だけを受ける（0x1e や 1e2 を別の分として通さない）", () => {
    expect(resultOf({ in_min: "0x1e" })).toBe("arrival_malformed");
    expect(resultOf({ in_min: "1e2" })).toBe("arrival_malformed");
    expect(resultOf({ in_min: "Infinity" })).toBe("arrival_malformed");
  });

  it("小数は受けて丸める（クライアントが割り算した値を送っても壊れない）", () => {
    expect(resultOf({ in_min: "12.5" })).toBe(15);
  });
});

describe("parseArrival — at", () => {
  it("いまからの差を分にする", () => {
    expect(resultOf({ at: "2026-09-09T12:30:00Z" })).toBe(30);
  });

  it("5 分刻みに丸める", () => {
    expect(resultOf({ at: "2026-09-09T12:37:00Z" })).toBe(35);
  });

  it("タイムゾーンが違っても同じ瞬間なら同じ分になる", () => {
    expect(resultOf({ at: "2026-09-09T21:30:00+09:00" })).toBe(30);
    expect(resultOf({ at: "2026-09-09T21:30:00+0900" })).toBe(30);
  });

  it("**タイムゾーンが無ければ 400**（素の日時を UTC と決めつけない）", () => {
    expect(resultOf({ at: "2026-09-09T12:30:00" })).toBe("arrival_malformed");
    expect(resultOf({ at: "2026-09-09" })).toBe("arrival_malformed");
  });

  it("日時として読めなければ 400", () => {
    expect(resultOf({ at: "でたらめ" })).toBe("arrival_malformed");
    expect(resultOf({ at: "2026-13-45T00:00:00Z" })).toBe("arrival_malformed");
  });

  it("過去の時刻は範囲外（過ぎた到着の確率は出さない）", () => {
    expect(resultOf({ at: "2026-09-09T11:30:00Z" })).toBe("arrival_out_of_range");
  });

  it("いまちょうども範囲外（最短の水平は 5 分先）", () => {
    expect(resultOf({ at: NOW.toISOString() })).toBe("arrival_out_of_range");
  });

  it("水平の先は 400", () => {
    expect(resultOf({ at: "2026-09-09T15:30:00Z" })).toBe("arrival_out_of_range");
  });

  it("端ちょうど（180 分先）は通る", () => {
    expect(resultOf({ at: "2026-09-09T15:00:00Z" })).toBe(180);
  });
});

describe("interpolateForecast", () => {
  const horizons = [...HORIZONS_MIN];
  /** 水平をそのまま値にした表。補間の結果を暗算で確かめられる。 */
  const ramp = horizons.map((minutes) => minutes);

  const at = (in_min: number, values: readonly number[] = ramp) =>
    interpolateForecast({ horizons_min: horizons, values_x1000: values, horizon_min: in_min });

  it("水平そのものは、その値をそのまま返す", () => {
    expect(at(30)).toBe(0.03);
    expect(at(180)).toBe(0.18);
  });

  it("水平の間は線形に補間する", () => {
    // 20 分（20）と 30 分（30）の中点 → 25
    expect(at(25)).toBe(0.025);
    // 60 分（60）と 90 分（90）の 1/3 → 70
    expect(at(70)).toBe(0.07);
  });

  it("端の外は張り付ける（外挿しない）", () => {
    expect(at(1)).toBe(0.005);
    expect(at(600)).toBe(0.18);
  });

  it("確率は 0〜1 に写る（元は 1/1000 刻み）", () => {
    const values = horizons.map(() => 0);
    expect(
      interpolateForecast({ horizons_min: horizons, values_x1000: values, horizon_min: 30 }),
    ).toBe(0);
    const full = horizons.map(() => 1000);
    expect(
      interpolateForecast({ horizons_min: horizons, values_x1000: full, horizon_min: 30 }),
    ).toBe(1);
  });

  it("1/1000 の分解能に丸める（見せかけの精度を作らない）", () => {
    const values = [0, 1, 1, 1, 1, 1, 1, 1, 1, 1];
    const result = at(7, values); // 5 分（0）と 10 分（1）の中点＝0.5/1000
    expect(result).not.toBeNull();
    if (result !== null) {
      expect(Number.isInteger(result * 1000)).toBe(true);
    }
  });

  it("配列が無ければ null（欠けた表から数を作らない）", () => {
    expect(
      interpolateForecast({ horizons_min: null, values_x1000: ramp, horizon_min: 30 }),
    ).toBeNull();
    expect(
      interpolateForecast({ horizons_min: horizons, values_x1000: null, horizon_min: 30 }),
    ).toBeNull();
  });

  it("長さがそろわなければ null", () => {
    expect(
      interpolateForecast({ horizons_min: horizons, values_x1000: [1, 2], horizon_min: 30 }),
    ).toBeNull();
  });

  it("空でも null（0 と言わない）", () => {
    expect(interpolateForecast({ horizons_min: [], values_x1000: [], horizon_min: 30 })).toBeNull();
  });

  it("水平が重複していても NaN を作らない", () => {
    const result = interpolateForecast({
      horizons_min: [5, 30, 30, 180],
      values_x1000: [100, 200, 300, 400],
      horizon_min: 20,
    });
    expect(result).not.toBeNull();
    expect(Number.isNaN(result)).toBe(false);
  });

  it("1 点しか無くても答えを返す（張り付け）", () => {
    expect(interpolateForecast({ horizons_min: [30], values_x1000: [250], horizon_min: 60 })).toBe(
      0.25,
    );
  });

  it("下りの表でも補間の向きを間違えない", () => {
    const falling = horizons.map((minutes) => 1000 - minutes * 5);
    expect(
      interpolateForecast({ horizons_min: horizons, values_x1000: falling, horizon_min: 25 }),
    ).toBe(0.875);
  });
});

describe("parseArrival と interpolateForecast のつなぎ", () => {
  it("**通った in_min は必ず補間できる**（範囲と水平がずれていない）", () => {
    const values = [...HORIZONS_MIN].map((minutes) => minutes * 5);
    for (let minutes = ARRIVAL_MIN_MIN; minutes <= ARRIVAL_MAX_MIN; minutes += 1) {
      const parsed = parseArrival({ at: null, in_min: String(minutes), now: NOW });
      expect(parsed.ok).toBe(true);
      if (parsed.ok && parsed.in_min !== null) {
        const value = interpolateForecast({
          horizons_min: [...HORIZONS_MIN],
          values_x1000: values,
          horizon_min: parsed.in_min,
        });
        expect(value).not.toBeNull();
      }
    }
  });
});

describe("forecastHorizon", () => {
  const horizonOf = (age_s: number, in_min = 30) =>
    forecastHorizon({
      in_min,
      generated_at: new Date(NOW.getTime() - age_s * 1000),
      now: NOW,
    });

  it("**予測行の年齢を足す**（水平の起点は generated_at）", () => {
    expect(horizonOf(120)).toBe(32);
    expect(horizonOf(126)).toBe(32.1);
  });

  it("同じ瞬間に作られた行なら in_min のまま", () => {
    expect(horizonOf(0)).toBe(30);
  });

  it("未来の時刻は 0 として扱う（頼まれた到着より手前を読まない）", () => {
    expect(horizonOf(-600)).toBe(30);
  });

  it("推論周期（5 分）ぶん古い行でも、足すのは 5 分だけ", () => {
    expect(horizonOf(300)).toBe(35);
  });

  it("鮮度の上限まで古いと 15 分足される（それ以上は返さない側で切る）", () => {
    expect(horizonOf(900)).toBe(45);
  });

  it("**足すと 180 を超えることがある**（張り付けて返す）", () => {
    const horizon = horizonOf(300, ARRIVAL_MAX_MIN);
    expect(horizon).toBeGreaterThan(ARRIVAL_MAX_MIN);
    const values = [...HORIZONS_MIN].map((minutes) => minutes * 5);
    expect(
      interpolateForecast({
        horizons_min: [...HORIZONS_MIN],
        values_x1000: values,
        horizon_min: horizon,
      }),
    ).toBe(0.9);
  });

  it("起点を無視すると別の時刻の確率になる（この関数が守っているもの）", () => {
    const horizons = [...HORIZONS_MIN];
    // 30 分（800）と 45 分（600）の間で、2 分ぶんずれると 26.7 ポイント動く
    const values = [1000, 1000, 1000, 1000, 800, 600, 400, 300, 200, 100];
    const naive = interpolateForecast({
      horizons_min: horizons,
      values_x1000: values,
      horizon_min: 30,
    });
    const correct = interpolateForecast({
      horizons_min: horizons,
      values_x1000: values,
      horizon_min: horizonOf(120),
    });
    expect(naive).toBe(0.8);
    expect(correct).toBe(0.773);
  });
});
