-- ────────────────────────────────────────────────────────────────
-- 0058 — 配信中の合成器の森の古さと、shadow の失敗を見張る（W8 プランの W8-12、PR F）
-- ────────────────────────────────────────────────────────────────
-- どちらも**壊れたようには見えないまま**、配る確率か、その確かめが弱っていく形である。
--
-- 1. **森が古くなっても、鮮度の警報は鳴らない。** 0048 の `check_model_freshness` は
--    `active` の `max(train_days)` を見る。合成器の `train_days` は B3 と森の学習日の和集合
--    （0055、W6-08）なので、**毎週当てはめ直す B3 の最終学習日で決まる**。森は
--    `lgbm-v1-20261005`（09-08〜10-05）のまま 3 週目に入った（当てはめ直しは W8-10 で測る）
--    → `check_forest_freshness`：配信中の合成器が持つ森の最終学習日が
--      `forest_stale_days`（**42 日**）以上前なら鳴らす
--
-- 2. **shadow が落ちても、何も鳴らない。** shadow の例外は `inference_log.detail.shadow` に
--    詰め替えられ、active の結果を変えない（W6-06、契約 34）。それは正しいが、**落ち続けると
--    日次評価の比べ（active 対 shadow）が片側だけになり**、次の昇格の材料が黙って欠ける
--    → `check_shadow`：系統ごとに、配った（`status = 'ok'`）直近 `shadow_fail_cycles`
--      （**3**）周期のどれでも、今の shadow が「歩いて予測ログまで置けた」にならなければ鳴らす
--
-- どちらも **6 時間に 1 回まで**（0048 と同じ）。**鳴るだけで、直さない**——森の入れ替えも
-- shadow の上げ下げも、人が確認してから行う（CLAUDE.md §6、契約 39）。
--
-- 3. **あわせて、B3 の鮮度の閾値を 8 日から 9 日にする**（W8 プランの所見 234）。0048 の 8 日は
--    「週 1 回、最終学習日の翌日に回す」形の正常の上限 7 日 ＋ 1 日だった。**月曜の朝に日曜までで
--    当てはめ直す形**（D-44・D-45）では、最終学習日（日曜）から次の月曜の朝の昇格までが**ちょうど
--    8 日**になり、**毎週月曜の 00:00 に必ず鳴る**（0048 が避けた「狼少年」）。9 日なら、鳴るのは
--    **月曜の回が昇格まで進まなかった週の火曜 00:00 から**だけになる。0048 と同じく `app_config` の
--    値で持つので、**8 のままの環境だけを 9 にする**（手で別の値にしてあれば触らない）。
--
-- **0048 の関数に足さず、検査を 2 つ別に作る。** `monitor_jobs` は検査ごとに例外を切り分ける
-- （0020）。森の欄が読めない合成器（登録側の異常）で例外になっても、B3 の鮮度の検査は走る。
--
-- ────────────────────────────────────────────────────────────────
-- なぜ 42 日か
-- ────────────────────────────────────────────────────────────────
-- **森は週ごとには当てはめ直さない**（W8-02。森を変える週は shadow 3 日が要る。D-44・D-46）。
-- W6 プラン §14.6 の目安「週次なら 15 日」で置くと、22 日目の今から鳴りっぱなしになる。
--
--   42 日 ＝ 学習の窓 35 日（W8-10 で当てはめ直す窓）＋ 1 週
--
-- 当てはめ直したときの学習の窓が**まるごと入れ替わり、さらに週 1 回の機会を 1 回逃した**、
-- だけが意味になる。いまの森（最終学習日 10-05）では **11/16（月）に初めて鳴る**。
--
-- ────────────────────────────────────────────────────────────────
-- shadow の「失敗」の数え方
-- ────────────────────────────────────────────────────────────────
-- * **見るのは active を配った周期だけ**（`inference_log.status = 'ok'`）。active が落ちた周期は
--   shadow を歩かない。そちらは `check_inference` が予測の古さで鳴らす
-- * **健全**は `detail.shadow.status = 'ok'` かつ `forecast_log = 'ok'`。予測ログが無ければ、
--   歩けても日次評価は比べられない。`skipped:no_profile`（契約 33）も健全に数えない
-- * **今の shadow の周期だけで判断する。** `detail.shadow` が無い周期と、別の版を歩いた周期は
--   「判断しない」——昇格の直後（**shadow の上げ下げは推論の合間に psql で行う**）に、前の
--   周期で誤って鳴らないためである。shadow の行を引けなかった周期（版が null）は、今の
--   shadow の失敗に数える
-- * **shadow が登録されていなければ何もしない**（shadow はふだんから置くが、必須ではない）

insert into public.app_config (key, value) values
  ('forest_stale_days', '42'),
  ('shadow_fail_cycles', '3')
on conflict (key) do nothing;

-- 3. の閾値。最終学習日が日曜の版は、次の月曜（8 日目）の昇格までが正常
update public.app_config
   set value = '9', updated_at = now()
 where key = 'model_stale_days' and value = '8';

-- ────────────────────────────────────────────────────────────────
-- 検査 10：配信中の合成器が持つ森の古さ
-- ────────────────────────────────────────────────────────────────
-- **森の最終学習日は、合成器の `metrics.components.lightgbm.train_days` の最大で採る**
-- （`build_composite` が書く `[最初の日, 最後の日]`。成果物に入っている森そのものの記録）。
-- 0048 と同じく、並びに頼らず `max` で採る。
--
-- **欄が無い・日付でない合成器は例外にする**（捕まえない）。`monitor_jobs` が
-- `monitor_check_failed:check_forest_freshness` で鳴らす——登録側の異常で、「古い」とは別の
-- 報せ方が要る（0048 と同じ考え）。
create or replace function public.check_forest_freshness()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row    record;
  v_limit  integer := public.config_int('forest_stale_days', 42);
  v_today  date    := (timezone('Asia/Tokyo', now()))::date;
  v_day    date;
  v_age    integer;
  v_alerts integer := 0;
begin
  select m.model_version, m.kind, m.metrics -> 'components' -> 'lightgbm' as forest
    into v_row
    from public.model_versions m
   where m.status = 'active';

  -- **active が無い**のは 0048 が `model_missing` で鳴らす
  if not found then
    return jsonb_build_object('active', null, 'forest', null,
                              'alerts', 0, 'threshold_days', v_limit);
  end if;
  -- **合成器でない**（B3 だけを配る）なら森は無い
  if v_row.kind <> 'composite' then
    return jsonb_build_object('active', v_row.model_version, 'forest', null,
                              'alerts', 0, 'threshold_days', v_limit);
  end if;

  select max(d::date) into v_day
    from jsonb_array_elements_text(v_row.forest -> 'train_days') d;
  if v_day is null then
    raise exception '合成器 % の metrics.components.lightgbm.train_days が読めません',
      v_row.model_version using errcode = '22023';
  end if;

  v_age := v_today - v_day;
  if v_age >= v_limit then
    if public.send_alert('forest_stale', jsonb_build_object(
         'message', '配信中の ' || v_row.model_version || ' の森（'
                    || coalesce(v_row.forest ->> 'model_version', '版が不明') || '）は '
                    || v_age || ' 日前までのデータで当てはめたものです（'
                    || v_limit || ' 日で通知）',
         'hint', '森を当てはめ直し（fit-lightgbm.yml）、合成器を組んで shadow で 3 日測ってから切り替える（D-44・D-46）',
         'model_version', v_row.model_version,
         'forest_version', v_row.forest ->> 'model_version',
         'last_train_day', v_day,
         'age_days', v_age,
         'threshold_days', v_limit), interval '6 hours') then
      v_alerts := 1;
    end if;
  end if;

  return jsonb_build_object('active', v_row.model_version,
                            'forest', v_row.forest ->> 'model_version',
                            'last_train_day', v_day, 'age_days', v_age,
                            'threshold_days', v_limit, 'alerts', v_alerts);
end;
$$;

comment on function public.check_forest_freshness() is
  '配信中の合成器が持つ森の最終学習日が forest_stale_days（既定 42 日）以上前なら、6 時間に 1 回知らせる（0058、W8-12）。合成器でなければ何もしない。入れ替えはしない（CLAUDE.md §6）。';

revoke all on function public.check_forest_freshness() from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- 検査 11：shadow が続けて落ちていないか
-- ────────────────────────────────────────────────────────────────
-- 系統ごとに直近の配った周期を `shadow_fail_cycles` 個だけ読む（`inference_log_recent_idx`
-- の `(system_id, generated_at desc)` で引ける）。冒頭の「数え方」のとおり、**全部が今の shadow
-- の周期で、1 つも健全でないときだけ**鳴らす。
create or replace function public.check_shadow()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_shadow  text;
  v_cycles  integer := public.config_int('shadow_fail_cycles', 3);
  v_row     record;
  v_checked integer := 0;
  v_alerts  integer := 0;
begin
  select m.model_version into v_shadow from public.model_versions m where m.status = 'shadow';
  if not found then
    return jsonb_build_object('shadow', null, 'checked', 0, 'alerts', 0,
                              'threshold_cycles', v_cycles);
  end if;

  for v_row in
    select sy.system_id, r.n, r.judged, r.healthy, r.outcomes, r.newest
      from public.systems sy
      cross join lateral (
        select count(*)::integer as n,
               (count(*) filter (where recent.judged))::integer as judged,
               (count(*) filter (where recent.healthy))::integer as healthy,
               array_agg(recent.outcome order by recent.generated_at desc) as outcomes,
               max(recent.generated_at) as newest
          from (
            select l.generated_at,
                   (l.detail ? 'shadow'
                    and coalesce(l.detail -> 'shadow' ->> 'model_version', v_shadow) = v_shadow)
                     as judged,
                   (l.detail -> 'shadow' ->> 'status' = 'ok'
                    and l.detail -> 'shadow' ->> 'forecast_log' = 'ok') as healthy,
                   -- 通知に載せる 1 周期の結果。歩けて予測ログで落ちたなら、そちらを書く
                   case
                     when l.detail -> 'shadow' ->> 'status' = 'ok'
                      and coalesce(l.detail -> 'shadow' ->> 'forecast_log', '') <> 'ok'
                       then 'forecast_log:' || coalesce(l.detail -> 'shadow' ->> 'forecast_log', '')
                     else coalesce(l.detail -> 'shadow' ->> 'status', 'none')
                   end as outcome
              from public.inference_log l
             where l.system_id = sy.system_id and l.status = 'ok'
             order by l.generated_at desc
             limit v_cycles
          ) recent
      ) r
     where sy.is_active
  loop
    v_checked := v_checked + 1;
    if v_row.n = v_cycles and v_row.judged = v_cycles and v_row.healthy = 0 then
      -- **系統ごとに鍵を分ける。** 片方だけ落ちる形（成果物にその系統が無い等）も隠れない
      if public.send_alert('shadow_failing:' || v_row.system_id, jsonb_build_object(
           'message', v_row.system_id || ' の shadow（' || v_shadow || '）が、直近 ' || v_cycles
                      || ' 周期続けて予測ログまで届いていません（'
                      || array_to_string(v_row.outcomes, '・') || '）',
           'hint', '推論の detail.shadow を見る。active の配信は止まっていない。落ち続けると日次評価の比べが片側だけになる',
           'shadow', v_shadow,
           'outcomes', to_jsonb(v_row.outcomes),
           'newest', v_row.newest,
           'threshold_cycles', v_cycles), interval '6 hours') then
        v_alerts := v_alerts + 1;
      end if;
    end if;
  end loop;

  return jsonb_build_object('shadow', v_shadow, 'checked', v_checked, 'alerts', v_alerts,
                            'threshold_cycles', v_cycles);
end;
$$;

comment on function public.check_shadow() is
  '系統ごとに、active を配った直近 shadow_fail_cycles（既定 3）周期で今の shadow が 1 度も予測ログまで届かなければ、6 時間に 1 回知らせる（0058、W8-12）。shadow が無ければ何もしない。上げ下げはしない（契約 39）。';

revoke all on function public.check_shadow() from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- monitor_jobs に 10・11 番目として組み込む
-- ────────────────────────────────────────────────────────────────
-- 本体は 0056 のまま。**並びの末尾に足すだけ**（検査どうしは独立していて、
-- 1 つが落ちても他は走る）。
create or replace function public.monitor_jobs()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run    bigint;
  v_names  text[] := array['check_jobs_missing', 'check_jobs_failed', 'check_cron_jobs',
                           'check_parquet_gap', 'check_reference_data', 'check_inference',
                           'check_model_freshness', 'check_weather_stale', 'check_jobs_stuck',
                           'check_forest_freshness', 'check_shadow'];
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
$$;

comment on function public.monitor_jobs() is
  '11 の検査（欠落・失敗・pg_cron・Parquet の穴・参照データ・推論・モデルの鮮度・天気の凍結・閉じられないジョブの記録・森の鮮度・shadow の失敗）を順に呼ぶ。';

revoke all on function public.monitor_jobs() from public, anon, authenticated;

notify pgrst, 'reload schema';
