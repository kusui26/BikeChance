/**
 * バックアップ収集器（Deno）と `packages/shared`（Node）の契約テスト（W3 プラン §9.2）。
 *
 * **Deno は拡張子なしの相対 import を解決せず、ワークスペースの別名も辿れない。**
 * そのため `supabase/functions/collect-gbfs-backup/core.ts` にはパス規約と定数を
 * 写してある。**片方だけ変えたらここが落ちる**ようにするのが、この 1 ファイルの仕事。
 *
 * 置き場所が `apps/web` なのは、`supabase/functions/` が vitest の対象外だから
 * （`packages/*` と `apps/web` にしか設定が無い）。`vercel-crons.test.ts` が
 * リポジトリのルートの `vercel.json` を読んでいるのと同じ前例に倣う。
 */
import {
  GZIP_CONTENT_TYPE,
  ODPT_PUBLIC_BASE_URL,
  RAW_BUCKET,
  SYSTEM_IDS,
  rawObjectPath,
} from "@bikechance/shared";
import { describe, expect, it } from "vitest";
import * as backup from "../../supabase/functions/collect-gbfs-backup/core";

/** 境界を含む時刻。UTC の日付・月・年の変わり目と、JST とずれる時間帯を通す。 */
const EPOCHS = [
  1, // 1970-01-01T00:00:01Z
  1_788_837_095, // 2026-09-08T05:51:35Z（本番の実データ）
  Date.UTC(2026, 0, 1, 0, 0, 0) / 1000, // 年の変わり目
  Date.UTC(2026, 8, 6, 14, 59, 59) / 1000, // JST 09-06 23:59:59（UTC ではまだ 06 日）
  Date.UTC(2026, 8, 6, 15, 0, 0) / 1000, // JST 09-07 00:00:00（UTC ではまだ 06 日）
  Date.UTC(2026, 8, 8, 0, 0, 0) / 1000, // UTC の日付が変わる瞬間
  Date.UTC(2027, 11, 31, 23, 59, 59) / 1000,
] as const;

describe("バックアップ収集器と shared の契約", () => {
  it("定数が一致する", () => {
    expect(backup.RAW_BUCKET).toBe(RAW_BUCKET);
    expect(backup.GZIP_CONTENT_TYPE).toBe(GZIP_CONTENT_TYPE);
    expect(backup.ODPT_PUBLIC_BASE_URL).toBe(ODPT_PUBLIC_BASE_URL);
    expect([...backup.SYSTEM_IDS]).toEqual([...SYSTEM_IDS]);
  });

  it("パスが 1 文字も違わない（境界を含む 7 時刻 × 2 システム）", () => {
    for (const system_id of SYSTEM_IDS) {
      for (const epoch_s of EPOCHS) {
        const expected = rawObjectPath({ system_id, feed: "station_status", epoch_s });
        const actual = backup.rawObjectPath({ system_id, feed: "station_status", epoch_s });
        expect(actual, `${system_id} / ${epoch_s}`).toBe(expected);
      }
    }
  });

  it("本番のパスの形をそのまま固定する", () => {
    // 実データ（2026-09-08T05:51:35Z）。**UTC の日付**で切る
    expect(
      backup.rawObjectPath({
        system_id: "hellocycling",
        feed: "station_status",
        epoch_s: 1_788_837_095,
      }),
    ).toBe("hellocycling/2026/09/08/station_status_1788837095.json.gz");
  });

  it("バックアップが扱うのは station_status だけ", () => {
    expect(backup.FEED_NAME).toBe("station_status");
  });
});

describe("システムの判定", () => {
  it("知っているシステムだけを通す", () => {
    expect(backup.isKnownSystem("hellocycling")).toBe(true);
    expect(backup.isKnownSystem("docomo-cycle")).toBe(true);
  });

  it("知らないシステムは弾く", () => {
    for (const value of ["", "HELLOCYCLING", "hellocycling ", "../hellocycling", "docomo"]) {
      expect(backup.isKnownSystem(value), value).toBe(false);
    }
  });
});

describe("URL の組み立て", () => {
  it("ODPT は公開エンドポイント（トークンを付けない）", () => {
    const url = backup.statusFeedUrl("hellocycling");
    expect(url).toBe("https://api-public.odpt.org/api/v4/gbfs/hellocycling/station_status.json");
    // **これが漏れの検査そのもの。** クエリが付いたら認証付きに戻っている
    expect(url).not.toContain("?");
    expect(url).not.toContain("consumerKey");
  });

  it("Storage と RPC の URL で末尾のスラッシュを重ねない", () => {
    expect(backup.storageObjectUrl("https://x.example/", "gbfs-raw", "a/b.json.gz")).toBe(
      "https://x.example/storage/v1/object/gbfs-raw/a/b.json.gz",
    );
    expect(backup.rpcUrl("https://x.example/", "job_started")).toBe(
      "https://x.example/rest/v1/rpc/job_started",
    );
  });
});

describe("last_updated の読み取り", () => {
  it("トップレベルの整数を返す", () => {
    expect(backup.readLastUpdated('{"last_updated":1788837095,"data":{"stations":[]}}')).toBe(
      1_788_837_095,
    );
  });

  it("末尾に置かれていても読める（実際の応答はこの順）", () => {
    expect(backup.readLastUpdated('{"ttl":60,"data":{},"last_updated":1788837095}')).toBe(
      1_788_837_095,
    );
  });

  it("整数でなければ止まる。**0 や欠損で先に進まない**", () => {
    for (const text of [
      '{"data":{}}',
      '{"last_updated":null}',
      '{"last_updated":"1788837095"}',
      '{"last_updated":0}',
      '{"last_updated":-1}',
      '{"last_updated":1.5}',
      "[]",
      "null",
    ]) {
      expect(() => backup.readLastUpdated(text), text).toThrow();
    }
  });

  it("例外に応答の中身を入れない（CLAUDE.md §5）", () => {
    try {
      backup.readLastUpdated('{"last_updated":"秘密っぽい値"}');
      expect.unreachable();
    } catch (cause) {
      expect(cause).toBeInstanceOf(Error);
      expect(String(cause)).not.toContain("秘密っぽい値");
    }
  });
});

describe("重複の判定", () => {
  // **Storage の REST API は重複を HTTP 400 で返す**（実測 2026-09-08）。
  // 409 は本文の中にあり、しかも文字列。HTTP ステータスだけを見ると
  // 「重複」が「想定外の失敗」に化け、毎回 500 になって job_runs が failed で埋まる
  const duplicateBody = {
    statusCode: "409",
    error: "Duplicate",
    message: "The resource already exists",
    code: "KeyAlreadyExists",
  };

  it("本番が返す形（HTTP 400 ＋ 本文に 409）を重複と読む", () => {
    expect(backup.isDuplicateUpload(400, duplicateBody)).toBe(true);
  });

  it("statusCode が数値でも読む（版によって揺れる）", () => {
    expect(backup.isDuplicateUpload(400, { statusCode: 409 })).toBe(true);
  });

  it("code だけでも読む", () => {
    expect(backup.isDuplicateUpload(400, { code: "KeyAlreadyExists" })).toBe(true);
  });

  it("HTTP 409 も拾う（API が素直になっても壊れない）", () => {
    expect(backup.isDuplicateUpload(409, null)).toBe(true);
  });

  it("**本当の失敗を重複と読み違えない**", () => {
    expect(backup.isDuplicateUpload(400, { statusCode: "400", error: "InvalidRequest" })).toBe(
      false,
    );
    expect(backup.isDuplicateUpload(403, { statusCode: "403" })).toBe(false);
    expect(backup.isDuplicateUpload(500, null)).toBe(false);
    expect(backup.isDuplicateUpload(500, "文字列の本文")).toBe(false);
    expect(backup.isDuplicateUpload(413, { statusCode: "413", error: "EntityTooLarge" })).toBe(
      false,
    );
  });
});

describe("認証", () => {
  it("一致すれば通す", async () => {
    await expect(backup.isAuthorized("Bearer s3cret", "s3cret")).resolves.toBe(true);
  });

  it("違えば弾く", async () => {
    await expect(backup.isAuthorized("Bearer wrong", "s3cret")).resolves.toBe(false);
    await expect(backup.isAuthorized("s3cret", "s3cret")).resolves.toBe(false);
    await expect(backup.isAuthorized("bearer s3cret", "s3cret")).resolves.toBe(false);
  });

  it("**設定漏れで素通りさせない**", async () => {
    await expect(backup.isAuthorized(null, "s3cret")).resolves.toBe(false);
    await expect(backup.isAuthorized("Bearer ", "")).resolves.toBe(false);
    await expect(backup.isAuthorized("Bearer x", undefined)).resolves.toBe(false);
  });
});
