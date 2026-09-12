/**
 * 生 JSON から作り直した行が、いま入っている行と一致するかを確かめる（W4 プラン §6.8 の PR M）。
 *
 * CLAUDE.md §6 は「生 JSON からの再構築スクリプトを常に動く状態に保つ」と定めている。
 * `status_snapshots_y2026m09` は 60 日保持で **2026-11-30** に落ちるので、それまでに
 * 「生 JSON → Postgres」が本当に動くことを実データで確かめておく必要がある。
 *
 * **`--dry-run` は「何を流すか」を数えるだけだった。** 一覧とパーティションの存在しか
 * 見ておらず、**生 JSON を 1 バイトも読んでいなかった**。数えるだけでは、正規化が
 * 壊れていても気づけない。ここが読んで・解いて・突き合わせる部分である。
 *
 * **I/O は持たない。** 読む口（`SnapshotSource`）を引数で受け取るので、フィクスチャで
 * 端から端まで動かせる（`test/rebuild-check.test.ts`）。
 *
 * **なぜ「一致するはず」と言えるか。** `ingest_snapshot`（0007）の規則が決まっているから。
 *
 *   - 配列は `stations.idx`（**0 起点・密**）の順に並び、位置は `idx + 1`
 *   - **フィードに現れなかったポートは `-1`**
 *   - `n_stations` は**フィードに在ったポートの数**（台帳の数ではない）
 *
 * 台帳と保存された行があれば、**生 JSON だけから同じ配列を組み立て直せる。**
 */
import type { SystemId } from "@bikechance/shared";
import { buildIngestArgs, type IngestArgs } from "./build-args";
import { normalizeStationStatus } from "./normalize";
import { parseStationStatusFeed } from "./schemas";

/** フィードに現れなかったポートの値（`ingest_snapshot` の §6）。 */
export const ABSENT = -1;

/** 突き合わせる 4 本の配列。 */
export const SNAPSHOT_FIELDS = ["bikes", "docks", "flags", "reported_age_s"] as const;
export type SnapshotField = (typeof SNAPSHOT_FIELDS)[number];

/** 台帳の 1 行。**`idx` が配列の位置を決める。** */
export type LedgerStation = { readonly station_id: string; readonly idx: number };

/** `status_snapshots` の 1 行のうち、突き合わせに要る分だけ。 */
export type StoredSnapshot = {
  readonly observed_at: string;
  readonly n_stations: number;
  readonly raw_path: string | null;
  readonly bikes: readonly number[];
  readonly docks: readonly number[];
  readonly flags: readonly number[];
  readonly reported_age_s: readonly number[];
};

/** 1 つの値の食い違い。**どのポートの・どの列かを名指しする。** */
export type SnapshotDifference = {
  readonly station_id: string;
  readonly idx: number;
  readonly field: SnapshotField;
  readonly rebuilt: number;
  /** `null` は「保存された配列の外」。**その時点では存在しなかったはずの位置。** */
  readonly stored: number | null;
};

/** 1 スナップショットぶんの突き合わせ。 */
export type SnapshotComparison = {
  readonly observed_at: string;
  readonly raw_path: string;
  readonly n_stations_rebuilt: number;
  readonly n_stations_stored: number;
  readonly n_values: number;
  readonly differences: readonly SnapshotDifference[];
  /** 台帳に無いポート。**再構築では登録できない**ので、食い違いとは別に数える。 */
  readonly unknown_stations: readonly string[];
  /** 保存された行の `raw_path` が、読んだファイルと違う。 */
  readonly path_differs: boolean;
  readonly ok: boolean;
};

const lengthOf = (stored: StoredSnapshot): number =>
  Math.max(...SNAPSHOT_FIELDS.map((field) => stored[field].length));

/** フィードから、`idx` の順に並べ直した 1 本の配列。**居ないポートは `-1`。** */
const rebuildOne = (
  args: IngestArgs,
  idxOf: ReadonlyMap<string, number>,
  field: SnapshotField,
  size: number,
): number[] => {
  const values = new Array<number>(size).fill(ABSENT);
  const source = args[`p_${field}`];
  args.p_station_ids.forEach((station_id, at) => {
    const idx = idxOf.get(station_id);
    if (idx !== undefined && idx < size) {
      values[idx] = source[at] ?? ABSENT;
    }
  });
  return values;
};

const differencesIn = (
  rebuilt: readonly number[],
  stored: readonly number[],
  field: SnapshotField,
  nameAt: ReadonlyMap<number, string>,
): SnapshotDifference[] =>
  rebuilt.flatMap((value, idx) =>
    value === stored[idx]
      ? []
      : [
          {
            station_id: nameAt.get(idx) ?? `idx=${idx}`,
            idx,
            field,
            rebuilt: value,
            stored: idx < stored.length ? (stored[idx] ?? null) : null,
          },
        ],
  );

/**
 * 1 スナップショットぶんを突き合わせる。**純粋。**
 *
 * **`fetched_at` は比べない。** 収集器が記録したのは応答を受け取った時刻で、再構築が
 * 使えるのは Storage の `created_at` である。**別のものなので、比べれば必ず落ちる。**
 * スナップショットの中身（台数・枠・フラグ・経過秒）ではないので、一致の対象にしない。
 */
export const compareSnapshot = (params: {
  readonly args: IngestArgs;
  readonly stored: StoredSnapshot;
  readonly ledger: readonly LedgerStation[];
  readonly raw_path: string;
}): SnapshotComparison => {
  const { args, stored, ledger, raw_path } = params;
  const idxOf = new Map(ledger.map((one) => [one.station_id, one.idx]));
  const nameAt = new Map(ledger.map((one) => [one.idx, one.station_id]));
  const size = Math.max(
    lengthOf(stored),
    ...args.p_station_ids.map((id) => (idxOf.get(id) ?? -1) + 1),
  );
  const differences = SNAPSHOT_FIELDS.flatMap((field) =>
    differencesIn(rebuildOne(args, idxOf, field, size), stored[field], field, nameAt),
  );
  const unknown_stations = args.p_station_ids.filter((id) => !idxOf.has(id));
  const path_differs = stored.raw_path !== null && stored.raw_path !== raw_path;
  return {
    observed_at: args.p_observed_at,
    raw_path,
    n_stations_rebuilt: args.p_station_ids.length,
    n_stations_stored: stored.n_stations,
    n_values: size * SNAPSHOT_FIELDS.length,
    differences,
    unknown_stations,
    path_differs,
    ok:
      differences.length === 0 &&
      unknown_stations.length === 0 &&
      !path_differs &&
      stored.n_stations === args.p_station_ids.length,
  };
};

/** 読む口。**実装は呼ぶ側**（本番は Storage と PostgREST、検査はフィクスチャ）。 */
export type SnapshotSource = {
  /** 生 gzip JSON を解いて `JSON.parse` したもの。 */
  readonly readFeed: (path: string) => Promise<unknown>;
  /** その `observed_at` の行。**無ければ `null`**（例外にしない）。 */
  readonly readSnapshot: (observed_at: string) => Promise<StoredSnapshot | null>;
};

/** 突き合わせる 1 オブジェクト。 */
export type RawObjectRef = {
  readonly path: string;
  /** Storage に保存された時刻。**`fetched_at` の代わり**（比べない。上の注記）。 */
  readonly created_at: string;
};

export type RebuildVerification = {
  readonly checked: number;
  readonly matched: number;
  /** 実際に突き合わせた値の数。**「何件見たか」ではなく「何個比べたか」。** */
  readonly n_values: number;
  /** 生 JSON は在るのに `status_snapshots` に行が無い。**取り込み漏れ。** */
  readonly missing_rows: readonly string[];
  /** 読めなかった・検証に落ちたファイル。 */
  readonly unreadable: readonly { readonly path: string; readonly error: string }[];
  readonly differing: readonly SnapshotComparison[];
  readonly ok: boolean;
};

const messageOf = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

/** 生 JSON 1 つを、取り込みに渡す引数まで持っていく。**本番と同じ経路を通す。** */
export const toIngestArgs = (params: {
  readonly system_id: SystemId;
  readonly document: unknown;
  readonly object: RawObjectRef;
}): IngestArgs => {
  const parsed = parseStationStatusFeed(params.document);
  if (!parsed.ok) {
    throw new Error(`検証に失敗: ${parsed.issues.join(" / ")}`);
  }
  return buildIngestArgs({
    system_id: params.system_id,
    feed: normalizeStationStatus(parsed.feed),
    fetched_at: new Date(params.object.created_at),
    // 応答の ETag は生 JSON に残らない。取得の記録であってスナップショットの内容ではない
    etag: null,
    raw_path: params.object.path,
  });
};

/**
 * 生 JSON の一覧を、いま入っている行と突き合わせる。**書かない。**
 *
 * **1 つ落ちても続ける。** 途中で止めると「最初の 1 件しか見ていない報告」になり、
 * 全体が壊れているのか 1 件だけなのかが分からない。
 */
export const verifyRebuild = async (params: {
  readonly source: SnapshotSource;
  readonly system_id: SystemId;
  readonly ledger: readonly LedgerStation[];
  readonly objects: readonly RawObjectRef[];
}): Promise<RebuildVerification> => {
  const missing_rows: string[] = [];
  const unreadable: { path: string; error: string }[] = [];
  const differing: SnapshotComparison[] = [];
  let checked = 0;
  let matched = 0;
  let n_values = 0;

  for (const object of params.objects) {
    try {
      const document = await params.source.readFeed(object.path);
      const args = toIngestArgs({ system_id: params.system_id, document, object });
      const stored = await params.source.readSnapshot(args.p_observed_at);
      if (stored === null) {
        missing_rows.push(object.path);
        continue;
      }
      checked += 1;
      const comparison = compareSnapshot({
        args,
        stored,
        ledger: params.ledger,
        raw_path: object.path,
      });
      n_values += comparison.n_values;
      if (comparison.ok) matched += 1;
      else differing.push(comparison);
    } catch (cause) {
      unreadable.push({ path: object.path, error: messageOf(cause) });
    }
  }

  return {
    checked,
    matched,
    n_values,
    missing_rows,
    unreadable,
    differing,
    ok: checked > 0 && matched === checked && missing_rows.length === 0 && unreadable.length === 0,
  };
};
