import { describe, expect, it } from "vitest";
import { toJobRunRecords } from "./dispatch-port";
import { isJobError } from "./errors";

describe("job_runs の行の読み方", () => {
  it("判断に使う欄だけを受ける（detail は null でもよい）", () => {
    const rows = toJobRunRecords([
      {
        id: 1,
        job_name: "build_features",
        status: "ok",
        started_at: "2026-11-30T01:00:00+00:00",
        detail: { date: "2026-11-29", rows: 621_252 },
      },
      {
        id: 2,
        job_name: "dispatch_daily",
        status: "running",
        started_at: "2026-11-30T01:15:20+00:00",
        detail: null,
      },
    ]);
    expect(rows.map((row) => row.id)).toEqual([1, 2]);
    expect(rows[0]?.detail?.["date"]).toBe("2026-11-29");
  });

  it("**1 行でも形が違えば投げる**（黙って落とすと、済んだ枠を起こし直しうる）", () => {
    const shapes: unknown[] = [
      null,
      [{ id: "1", job_name: "build_features", status: "ok", started_at: "x", detail: null }],
      [{ id: 1, job_name: "build_features", status: "ok", detail: null }],
    ];
    for (const shape of shapes) {
      let caught: unknown = null;
      try {
        toJobRunRecords(shape);
      } catch (cause) {
        caught = cause;
      }
      expect(isJobError(caught) && caught.failure.error_name).toBe("UnexpectedJobRunsShape");
    }
  });
});
