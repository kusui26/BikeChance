import { describe, expect, it } from "vitest";
import {
  BBOX_MAX_SPAN_DEG,
  BBOX_QUANTUM_DEG,
  bboxSpans,
  formatBbox,
  isInsideBbox,
  parseBbox,
  quantizeBbox,
  type Bbox,
} from "./bbox";

/** 東京駅まわりの小さな矩形。 */
const TOKYO: Bbox = { west: 139.76, south: 35.67, east: 139.78, north: 35.69 };

const ok = (text: string): Bbox => {
  const result = parseBbox(text);
  if (!result.ok) {
    throw new Error(`解釈できるはずの bbox が失敗した: ${result.problem}`);
  }
  return result.bbox;
};

const problemOf = (text: string | null): string => {
  const result = parseBbox(text);
  return result.ok ? "ok" : result.problem;
};

describe("parseBbox", () => {
  it("west,south,east,north の順に読む", () => {
    expect(ok("139.76,35.67,139.78,35.69")).toEqual(TOKYO);
  });

  it("空白は無視する", () => {
    expect(ok(" 139.76 , 35.67 , 139.78 , 35.69 ")).toEqual(TOKYO);
  });

  it("負の値と 0 をまたぐ矩形を読める", () => {
    expect(ok("-0.2,-0.2,0.2,0.2")).toEqual({ west: -0.2, south: -0.2, east: 0.2, north: 0.2 });
  });

  it("未指定と空文字は missing", () => {
    expect(problemOf(null)).toBe("missing");
    expect(problemOf("")).toBe("missing");
    expect(problemOf("   ")).toBe("missing");
  });

  it("要素数が 4 でなければ malformed", () => {
    expect(problemOf("139.76,35.67,139.78")).toBe("malformed");
    expect(problemOf("139.76,35.67,139.78,35.69,0")).toBe("malformed");
  });

  it("数でない要素は malformed（空要素は 0 と読まない）", () => {
    expect(problemOf("139.76,35.67,139.78,x")).toBe("malformed");
    expect(problemOf("139.76,35.67,139.78,")).toBe("malformed");
    expect(problemOf("139.76,35.67,139.78,NaN")).toBe("malformed");
    expect(problemOf("139.76,35.67,139.78,Infinity")).toBe("malformed");
  });

  it("緯度経度の範囲外は out_of_range", () => {
    expect(problemOf("139.76,-91,139.78,35.69")).toBe("out_of_range");
    expect(problemOf("139.76,35.67,181,35.69")).toBe("out_of_range");
  });

  it("west >= east / south >= north は inverted", () => {
    expect(problemOf("139.78,35.67,139.76,35.69")).toBe("inverted");
    expect(problemOf("139.76,35.69,139.78,35.67")).toBe("inverted");
    // 面積 0 も受け取らない（結果が必ず空になり、上限の判定も意味を失う）
    expect(problemOf("139.76,35.67,139.76,35.69")).toBe("inverted");
  });

  it("1 辺が上限を超えたら too_large", () => {
    const over = BBOX_MAX_SPAN_DEG + 0.001;
    expect(problemOf(`139.7,35.6,${139.7 + over},35.7`)).toBe("too_large");
    expect(problemOf(`139.7,35.6,139.8,${35.6 + over}`)).toBe("too_large");
  });

  it("上限ちょうどは通る", () => {
    expect(problemOf(`139.7,35.6,${139.7 + BBOX_MAX_SPAN_DEG},${35.6 + BBOX_MAX_SPAN_DEG}`)).toBe(
      "ok",
    );
  });
});

describe("quantizeBbox", () => {
  it("外側へ丸める（要求された範囲は必ず入る）", () => {
    const quantized = quantizeBbox({
      west: 139.7654,
      south: 35.6789,
      east: 139.7712,
      north: 35.6821,
    });
    expect(quantized).toEqual({ west: 139.76, south: 35.67, east: 139.78, north: 35.69 });
  });

  it("格子ちょうどの矩形は変わらない", () => {
    expect(quantizeBbox(TOKYO)).toEqual(TOKYO);
  });

  it("丸めた結果は必ず元の範囲を含む", () => {
    const samples = [
      { west: 139.7654321, south: 35.6789012, east: 139.7654322, north: 35.6789013 },
      { west: -0.0001, south: -0.0001, east: 0.0001, north: 0.0001 },
      { west: 139.99999, south: 35.99999, east: 140.00001, north: 36.00001 },
    ];
    for (const bbox of samples) {
      const quantized = quantizeBbox(bbox);
      expect(quantized.west).toBeLessThanOrEqual(bbox.west);
      expect(quantized.south).toBeLessThanOrEqual(bbox.south);
      expect(quantized.east).toBeGreaterThanOrEqual(bbox.east);
      expect(quantized.north).toBeGreaterThanOrEqual(bbox.north);
    }
  });

  it("浮動小数の端数を残さない（同じ入力が同じ URL になる）", () => {
    const quantized = quantizeBbox({ west: 139.767, south: 35.681, east: 139.768, north: 35.682 });
    for (const value of Object.values(quantized)) {
      expect(String(value)).toMatch(/^-?\d+(\.\d{1,6})?$/);
    }
  });

  it("近い 2 つの要求が同じ矩形に写る（CDN が効く）", () => {
    const a = quantizeBbox({ west: 139.7601, south: 35.6701, east: 139.7799, north: 35.6899 });
    const b = quantizeBbox({ west: 139.7609, south: 35.6709, east: 139.7791, north: 35.6891 });
    expect(formatBbox(a)).toBe(formatBbox(b));
  });

  it("刻みを渡せる", () => {
    expect(quantizeBbox(TOKYO, 0.1)).toEqual({
      west: 139.7,
      south: 35.6,
      east: 139.8,
      north: 35.7,
    });
  });

  it("刻みは 1 km 程度（位置を細かく受け取らない）", () => {
    expect(BBOX_QUANTUM_DEG).toBe(0.01);
  });
});

describe("bboxSpans / isInsideBbox / formatBbox", () => {
  it("辺の長さを返す", () => {
    const spans = bboxSpans(TOKYO);
    expect(spans.lat).toBeCloseTo(0.02, 10);
    expect(spans.lon).toBeCloseTo(0.02, 10);
  });

  it("境界を含めて判定する", () => {
    expect(isInsideBbox(TOKYO, 35.68, 139.77)).toBe(true);
    expect(isInsideBbox(TOKYO, 35.67, 139.76)).toBe(true);
    expect(isInsideBbox(TOKYO, 35.69, 139.78)).toBe(true);
    expect(isInsideBbox(TOKYO, 35.66, 139.77)).toBe(false);
    expect(isInsideBbox(TOKYO, 35.68, 139.79)).toBe(false);
  });

  it("表記は読み戻せる", () => {
    expect(ok(formatBbox(TOKYO))).toEqual(TOKYO);
  });
});
