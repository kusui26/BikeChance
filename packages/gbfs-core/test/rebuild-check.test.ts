/**
 * 生 JSON からの再構築の突き合わせ（`src/rebuild-check.ts`、W4 プラン §6.8 の PR M）。
 *
 * **主題は「作り直せると言い切れること」。** `status_snapshots_y2026m09` は 60 日保持で
 * **2026-11-30** に落ちる。CLAUDE.md §6 は「生 JSON からの再構築スクリプトを常に動く
 * 状態に保つ」と定めているが、**検査は無く CI からも呼ばれていなかった**（§8.5.7）。
 *
 * **実データのフィクスチャで端から端まで通す。** 読む口を差し替えられるので、
 * Storage も PostgREST も要らない——だから CI で毎回回る。
 *
 * ここで固定するのは 3 つ。
 *   - **一致を一致と言う**（縮約した実フィードから `ingest_snapshot` と同じ配列が出る）
 *   - **食い違いを見落とさない**（値・欠け・台帳・件数・パスのどれがずれても出る）
 *   - **1 つ落ちても続ける**（最初の 1 件で止めると全体像が分からない）
 */
import { describe, expect, it } from "vitest";
import {
  ABSENT,
  SNAPSHOT_FIELDS,
  buildIngestArgs,
  compareSnapshot,
  normalizeStationStatus,
  parseStationStatusFeed,
  toIngestArgs,
  verifyRebuild,
  type IngestArgs,
  type LedgerStation,
  type RawObjectRef,
  type SnapshotSource,
  type StoredSnapshot,
} from "../src/index";
import { readFeedFixture } from "./fixtures";

const SYSTEM = "hellocycling" as const;
const OBJECT: RawObjectRef = {
  path: "hellocycling/2026/09/06/station_status_1757140320.json.gz",
  created_at: "2026-09-06T06:52:00.000Z",
};

const feedDocument = (): unknown => readFeedFixture(SYSTEM, "station_status");

const argsOf = (): IngestArgs =>
  toIngestArgs({ system_id: SYSTEM, document: feedDocument(), object: OBJECT });

/** フィードの並びのまま `idx` を 0 から振る（初回取り込みと同じ採番）。 */
const ledgerOf = (args: IngestArgs): LedgerStation[] =>
  args.p_station_ids.map((station_id, idx) => ({ station_id, idx }));

/** `ingest_snapshot` が書いたはずの行を、台帳の順で組み立てる。 */
const storedOf = (args: IngestArgs, ledger: readonly LedgerStation[]): StoredSnapshot => {
  const at = new Map(args.p_station_ids.map((id, i) => [id, i]));
  const pick = (source: readonly number[]): number[] =>
    ledger.map((one) => {
      const i = at.get(one.station_id);
      return i === undefined ? ABSENT : (source[i] ?? ABSENT);
    });
  return {
    observed_at: args.p_observed_at,
    n_stations: args.p_station_ids.length,
    raw_path: OBJECT.path,
    bikes: pick(args.p_bikes),
    docks: pick(args.p_docks),
    flags: pick(args.p_flags),
    reported_age_s: pick(args.p_reported_age_s),
  };
};

const sourceOf = (stored: StoredSnapshot | null): SnapshotSource => ({
  readFeed: () => Promise.resolve(feedDocument()),
  readSnapshot: () => Promise.resolve(stored),
});

describe("compareSnapshot", () => {
  it("実フィードから作り直した配列が、保存された行と一致する", () => {
    const args = argsOf();
    const ledger = ledgerOf(args);
    const outcome = compareSnapshot({
      args,
      stored: storedOf(args, ledger),
      ledger,
      raw_path: OBJECT.path,
    });

    expect(outcome.ok).toBe(true);
    expect(outcome.differences).toEqual([]);
    expect(outcome.n_stations_rebuilt).toBe(args.p_station_ids.length);
    expect(outcome.n_values).toBe(ledger.length * SNAPSHOT_FIELDS.length);
    expect(ledger.length).toBeGreaterThan(10);
  });

  it("台帳に在ってフィードに無いポートは -1 として一致する", () => {
    const args = argsOf();
    // あとから登録されたポートを 1 つ足す。**この行では -1 のはず**
    const ledger = [...ledgerOf(args), { station_id: "later", idx: args.p_station_ids.length }];
    const outcome = compareSnapshot({
      args,
      stored: storedOf(args, ledger),
      ledger,
      raw_path: OBJECT.path,
    });

    expect(outcome.ok).toBe(true);
    expect(outcome.n_values).toBe(ledger.length * SNAPSHOT_FIELDS.length);
  });

  it("値が 1 つ違えば、どのポートの・どの列かまで出す", () => {
    const args = argsOf();
    const ledger = ledgerOf(args);
    const stored = storedOf(args, ledger);
    const broken = {
      ...stored,
      bikes: [...stored.bikes.slice(0, 3), 99, ...stored.bikes.slice(4)],
    };

    const outcome = compareSnapshot({ args, stored: broken, ledger, raw_path: OBJECT.path });

    expect(outcome.ok).toBe(false);
    expect(outcome.differences).toHaveLength(1);
    expect(outcome.differences[0]).toMatchObject({
      station_id: ledger[3]!.station_id,
      idx: 3,
      field: "bikes",
      stored: 99,
    });
  });

  it("4 本とも見る（どれか 1 本だけを見ていれば素通りする）", () => {
    const args = argsOf();
    const ledger = ledgerOf(args);
    const stored = storedOf(args, ledger);
    const broken = {
      ...stored,
      reported_age_s: [...stored.reported_age_s.slice(1), ABSENT],
    };

    const outcome = compareSnapshot({ args, stored: broken, ledger, raw_path: OBJECT.path });

    expect(outcome.ok).toBe(false);
    expect(new Set(outcome.differences.map((one) => one.field))).toEqual(
      new Set(["reported_age_s"]),
    );
  });

  it("台帳に無いポートは、食い違いとは別に数える", () => {
    const args = argsOf();
    // **先頭のポートだけ台帳に無い。** 残りは詰めて採番し直すので、値はすべて一致する
    const ledger = args.p_station_ids.slice(1).map((station_id, idx) => ({ station_id, idx }));
    const outcome = compareSnapshot({
      args,
      stored: storedOf(args, ledger),
      ledger,
      raw_path: OBJECT.path,
    });

    // **値は 1 つも違わない。** 落ちる理由は「台帳に無い」ことだけである
    expect(outcome.differences).toEqual([]);
    expect(outcome.unknown_stations).toEqual([args.p_station_ids[0]]);
    expect(outcome.ok).toBe(false);
  });

  it("件数が合わなければ落とす（台帳の数と取り違えていないか）", () => {
    const args = argsOf();
    const ledger = ledgerOf(args);
    const stored = { ...storedOf(args, ledger), n_stations: ledger.length + 5 };

    expect(compareSnapshot({ args, stored, ledger, raw_path: OBJECT.path }).ok).toBe(false);
  });

  it("行の raw_path が読んだファイルと違えば落とす", () => {
    const args = argsOf();
    const ledger = ledgerOf(args);
    const stored = { ...storedOf(args, ledger), raw_path: "hellocycling/別のファイル.json.gz" };

    const outcome = compareSnapshot({ args, stored, ledger, raw_path: OBJECT.path });

    expect(outcome.path_differs).toBe(true);
    expect(outcome.ok).toBe(false);
  });

  it("raw_path が NULL の行は、パスでは落とさない（古い行に無いことがある）", () => {
    const args = argsOf();
    const ledger = ledgerOf(args);
    const stored = { ...storedOf(args, ledger), raw_path: null };

    expect(compareSnapshot({ args, stored, ledger, raw_path: OBJECT.path }).ok).toBe(true);
  });

  it("fetched_at は比べない（収集の受信時刻と Storage の保存時刻は別物）", () => {
    const document = feedDocument();
    const later: RawObjectRef = { ...OBJECT, created_at: "2026-09-06T23:59:59.000Z" };
    const args = toIngestArgs({ system_id: SYSTEM, document, object: OBJECT });
    const ledger = ledgerOf(args);
    const shifted = toIngestArgs({ system_id: SYSTEM, document, object: later });

    expect(shifted.p_fetched_at).not.toBe(args.p_fetched_at);
    expect(
      compareSnapshot({
        args: shifted,
        stored: storedOf(args, ledger),
        ledger,
        raw_path: OBJECT.path,
      }).ok,
    ).toBe(true);
  });
});

describe("toIngestArgs", () => {
  it("本番と同じ経路を通る（収集器が作る引数と 1 バイトも違わない）", () => {
    const document = feedDocument();
    const parsed = parseStationStatusFeed(document);
    if (!parsed.ok) throw new Error(parsed.issues.join());
    const collector = buildIngestArgs({
      system_id: SYSTEM,
      feed: normalizeStationStatus(parsed.feed),
      fetched_at: new Date(OBJECT.created_at),
      etag: null,
      raw_path: OBJECT.path,
    });

    expect(toIngestArgs({ system_id: SYSTEM, document, object: OBJECT })).toEqual(collector);
  });

  it("検証に落ちるフィードは、そう言って止まる", () => {
    expect(() =>
      toIngestArgs({ system_id: SYSTEM, document: { 壊れている: true }, object: OBJECT }),
    ).toThrow(/検証に失敗/);
  });
});

describe("verifyRebuild", () => {
  const ledgerAndStored = (): { ledger: LedgerStation[]; stored: StoredSnapshot } => {
    const args = argsOf();
    const ledger = ledgerOf(args);
    return { ledger, stored: storedOf(args, ledger) };
  };

  it("一致すれば ok", async () => {
    const { ledger, stored } = ledgerAndStored();
    const outcome = await verifyRebuild({
      source: sourceOf(stored),
      system_id: SYSTEM,
      ledger,
      objects: [OBJECT],
    });

    expect(outcome).toMatchObject({ ok: true, checked: 1, matched: 1 });
    expect(outcome.differing).toEqual([]);
    expect(outcome.n_values).toBe(ledger.length * SNAPSHOT_FIELDS.length);
  });

  it("行が無ければ「取り込み漏れ」として数える（一致とは言わない）", async () => {
    const { ledger } = ledgerAndStored();
    const outcome = await verifyRebuild({
      source: sourceOf(null),
      system_id: SYSTEM,
      ledger,
      objects: [OBJECT],
    });

    expect(outcome.ok).toBe(false);
    expect(outcome.missing_rows).toEqual([OBJECT.path]);
    expect(outcome.checked).toBe(0);
  });

  it("1 つも見ていなければ ok にしない（0 件どうしを一致と言わない）", async () => {
    const { ledger } = ledgerAndStored();
    const outcome = await verifyRebuild({
      source: sourceOf(null),
      system_id: SYSTEM,
      ledger,
      objects: [],
    });

    expect(outcome).toMatchObject({ ok: false, checked: 0 });
  });

  it("読めないファイルが在っても、残りは続ける", async () => {
    const { ledger, stored } = ledgerAndStored();
    const broken: RawObjectRef = { ...OBJECT, path: "hellocycling/壊れ.json.gz" };
    const source: SnapshotSource = {
      readFeed: (path) =>
        path === broken.path
          ? Promise.reject(new Error("読めない"))
          : Promise.resolve(feedDocument()),
      readSnapshot: () => Promise.resolve(stored),
    };

    const outcome = await verifyRebuild({
      source,
      system_id: SYSTEM,
      ledger,
      objects: [broken, OBJECT],
    });

    expect(outcome.ok).toBe(false);
    expect(outcome.unreadable).toEqual([{ path: broken.path, error: "読めない" }]);
    expect(outcome.matched).toBe(1);
  });

  it("食い違ったスナップショットを並べて返す", async () => {
    const { ledger, stored } = ledgerAndStored();
    const broken = { ...stored, docks: stored.docks.map(() => 0) };
    const outcome = await verifyRebuild({
      source: sourceOf(broken),
      system_id: SYSTEM,
      ledger,
      objects: [OBJECT],
    });

    expect(outcome.ok).toBe(false);
    expect(outcome.differing).toHaveLength(1);
    expect(outcome.differing[0]!.differences.length).toBeGreaterThan(0);
  });
});
