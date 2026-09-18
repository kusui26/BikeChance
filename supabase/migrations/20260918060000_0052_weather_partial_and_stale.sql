-- ────────────────────────────────────────────────────────────────
-- 0052 — 途中まで保存できた発行を取り込めるようにし、天気の凍結を見張る
-- ────────────────────────────────────────────────────────────────
-- 2026-09-17 04:17 から 2026-09-18 まで、`archive_weather` が毎時 429 で落ちていた
-- （Open-Meteo の 1 分 600 地点の枠。格子が 601 になった瞬間に始まった。W5 プラン
-- §12 の 173）。**7 分割のうち 6 つは保存できていた**——1 時間あたり 600 / 602 セルが
-- `weather-raw` に在る。
--
-- それでも `weather_hourly` は **09-17 03:00 JST で止まった**。理由が 2 つある。
--
--   1. `v_weather_pending` が `status = 'ok'` で絞っていた。1 分割でも落ちると
--      ジョブは `failed` になるので、**保存済みの 600 セルが未処理として現れない**
--   2. 取り込み側（`load_weather.py` の `read_issue`）が「1 つでも欠ければ止める」
--
-- **ここで直すのは 1 のほう。** 2 は Python 側で直す（同じ PR）。
--
-- ついでに **`check_weather_stale`** を足す。今回いちばん困ったのは、
-- `load_weather` が 34 時間ずっと `ok`（`rows: 0`）を返し続けたことである
-- ——**入れるものが無いことと、入れる必要が無いことが区別できていなかった。**
-- ジョブの見張りは「動いたか」しか見ないので、**結果が進んでいるか**を別に見る。
-- ────────────────────────────────────────────────────────────────

-- ────────────────────────────────────────────────────────────────
-- v_weather_pending — 途中まで保存できた発行を 1 度だけ出す
-- ────────────────────────────────────────────────────────────────
-- 0037 の意図（「途中で落ちた発行も未処理として出る」）を、**実際にそうする**。
--
-- **`status = 'ok'` をやめて `n_saved > 0` にする。** 見たいのは「Storage に何か在るか」
-- であって、ジョブが緑だったかではない。
--
-- **落ちた発行は 1 度しか出さない**（`n_loaded = 0` のときだけ）。ここを素直に
-- 「入り切るまで出す」にすると、**永久に埋まらない発行が居座る**：欠けた 2 セルは
-- 二度と取れないので `n_loaded < n_cells` が永遠に真になり、`load_weather` は
-- 1 回 6 発行・古い順なので、**新しい発行に永久に到達しない**（実測で 34 時間ぶんが
-- 該当した）。取り込みは発行ごとに「読めたものを全部 1 回で入れる」ので、
-- 1 度渡せば取れるぶんは取れる。
--
-- **緑だった発行のふるまいは変わらない**（`status = 'ok'` なら今までどおり、
-- 入り切るまで何度でも出る）。
create or replace view public.v_weather_pending as
select f.hour_epoch_s,
       f.forecast_hour as issued_hour,
       f.available_at,
       f.n_cells,
       coalesce(l.n_loaded, 0) as n_loaded
  from public.v_weather_files f
  left join (
    select issued_hour, count(*)::integer as n_loaded
      from public.weather_hourly
     group by issued_hour
  ) l on l.issued_hour = f.forecast_hour
 where f.n_saved > 0
   and coalesce(l.n_loaded, 0) < f.n_cells
   and (f.status = 'ok' or coalesce(l.n_loaded, 0) = 0);

comment on view public.v_weather_pending is
  'まだ weather_hourly に入っていない（または途中までしか入っていない）予報の発行。'
  '分割が欠けた発行は「まだ 1 行も入っていない」あいだだけ出る——欠けた分は二度と'
  '取れないので、入り切るまで出し続けると新しい発行に到達しなくなる（0052）。'
  '読む側は available_at の下限を必ず添える。';

-- ────────────────────────────────────────────────────────────────
-- 検査 8：天気が進んでいるか
-- ────────────────────────────────────────────────────────────────
-- **ジョブの見張りでは捕まらない穴を塞ぐ。** `check_jobs_missing` と
-- `check_jobs_failed` は「動いたか・落ちたか」を見るが、**動いて `ok` を返しながら
-- 何も進んでいない**ことがある。2026-09-17〜18 の `load_weather` がまさにそれで、
-- 34 時間ぶん `{"ok": true, "rows": 0, "pending": 0}` を返し続けた。
--
-- **見るのは結果（`weather_hourly` の最新の発行）**である。予報は毎時 1 発行ずつ
-- 進むので、**4 時間動かなければ 3 回続けて取りこぼしている**。
--
-- **`issued_hour` で見る**（`available_at` ではない）。入手した時刻ではなく、
-- **どの時点の予報まで揃っているか**が特徴量にとっての意味である。
create or replace function public.check_weather_stale()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  -- 予報は毎時 1 発行。3 回続けて取りこぼすまでは鳴らさない
  v_limit  interval := interval '4 hours';
  v_latest timestamptz;
  v_age    interval;
  v_alerts integer := 0;
begin
  select max(issued_hour) into v_latest from public.weather_hourly;

  -- **1 行も無いのは「まだ始まっていない」**。W2 より前の環境で誤報を出さない
  if v_latest is null then
    return jsonb_build_object('latest', null, 'alerts', 0, 'note', 'weather_hourly が空');
  end if;

  v_age := now() - v_latest;
  if v_age > v_limit then
    if public.send_alert('weather_stale', jsonb_build_object(
         'message', '天気の発行が ' || to_char(v_age, 'HH24:MI') || ' 進んでいません。'
                    || 'archive_weather（取得）と load_weather（取り込み）の両方を見てください'
                    || '——ジョブが ok でも止まることがあります',
         'latest_issued_hour', to_char(v_latest at time zone 'Asia/Tokyo', 'YYYY-MM-DD HH24:MI') || ' JST',
         'age', to_char(v_age, 'HH24:MI'),
         'threshold', to_char(v_limit, 'HH24:MI')),
         interval '6 hours') then
      v_alerts := 1;
    end if;
  end if;

  return jsonb_build_object(
    'latest_issued_hour', v_latest,
    'age_hours', round(extract(epoch from v_age) / 3600.0, 2),
    'threshold_hours', extract(epoch from v_limit) / 3600.0,
    'alerts', v_alerts);
end;
$$;

comment on function public.check_weather_stale() is
  'weather_hourly の最新 issued_hour が 4 時間進んでいなければ通知する。'
  'ジョブが ok を返しながら何も入っていない状態（2026-09-17 の load_weather）を捕まえる。';

revoke all on function public.check_weather_stale() from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- monitor_jobs に 8 つ目として組み込む
-- ────────────────────────────────────────────────────────────────
-- 本体は 0048 のまま。**並びの末尾に足すだけ**（検査どうしは独立していて、
-- 1 つが落ちても他は走る）。
CREATE OR REPLACE FUNCTION public.monitor_jobs()
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare
  v_run    bigint;
  v_names  text[] := array['check_jobs_missing', 'check_jobs_failed', 'check_cron_jobs',
                           'check_parquet_gap', 'check_reference_data', 'check_inference',
                           'check_model_freshness', 'check_weather_stale'];
  v_name   text;
  v_one    jsonb;
  v_checks jsonb := '{}'::jsonb;
  v_alerts integer := 0;
  v_errors integer := 0;
begin
  -- ジョブごとに別の object_id を使う（W1-30）。1〜5 は 0009・0011・0013 が使っている
  if not pg_try_advisory_xact_lock(8423, 6) then
    return jsonb_build_object('status', 'locked');
  end if;
  v_run := public.job_started('monitor_jobs');

  foreach v_name in array v_names loop
    begin
      execute format('select public.%I()', v_name) into v_one;
      v_alerts := v_alerts + coalesce((v_one->>'alerts')::integer, 0);
    exception when others then
      v_errors := v_errors + 1;
      v_one := jsonb_build_object('error', sqlerrm, 'code', sqlstate);
      -- 通知の失敗でこの関数を落とさない。記録のほうが確実に残る
      begin
        perform public.send_alert('monitor_check_failed:' || v_name, jsonb_build_object(
          'message', '監視の検査 ' || v_name || ' が失敗しました',
          'error', sqlerrm, 'code', sqlstate), interval '1 hour');
      exception when others then
        null;
      end;
    end;
    v_checks := v_checks || jsonb_build_object(v_name, v_one);
  end loop;

  v_one := jsonb_build_object('alerts', v_alerts, 'errors', v_errors, 'checks', v_checks);
  perform public.job_finished(v_run, case when v_errors = 0 then 'ok' else 'failed' end, v_one);
  return v_one;
end;
$function$;

comment on function public.monitor_jobs() is
  '8 つの検査（欠落・失敗・pg_cron・Parquet の穴・参照データ・推論・モデルの鮮度・天気の凍結）を順に呼ぶ。';
