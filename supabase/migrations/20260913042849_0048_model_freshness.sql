-- 0048 配信中のモデルが古くなったら鳴らす（W5 プラン §6.13、PR M。W5-17）
--
-- **当てはめ直しが 1 行になったので、忘れられるようになった。** `fit_baseline --upload`
-- は期間を書かなくても走る（W5-16）。楽になった分だけ「今週は回したか」が記憶頼りに
-- なり、**回し忘れても何も起きない**——古いモデルは黙って確率を出し続ける。
--
-- **古いモデルは「壊れた」ようには見えない。** `check_inference` は予測が新しいかを
-- 見るので、**2 週間前のデータで当てはめた B1・B2 でも、5 分毎に新しい予測が出ていれば
-- 何も言わない**。劣化は Brier がじわじわ悪くなる形で現れ、日次評価（PR L）が入るまで
-- 誰も見ていない。だから**「材料の古さ」そのもの**を見張る。
--
-- ────────────────────────────────────────────────────────────────
-- なぜ 8 日か
-- ────────────────────────────────────────────────────────────────
-- 当てはめ直しの周期は**週 1 回**（W6 以降。§7.3）。週次で回すかぎり、最終学習日から
-- 今日までの日数は 1 日（回した当日）→ 7 日（次に回す前日）を往復する。
--
--   7 日 … **正常の上限**。ここで鳴らすと毎週 1 回必ず鳴る（狼少年になる）
--   8 日 … **1 回飛ばした**。回すはずの日に回らなかった、だけが意味になる
--   3 日 … W5 の途中（毎日は回さない）では 09-15 から 09-21 まで鳴りっぱなしになる
--          （実測：09-12 までで当てはめた版が active。3 日なら 09-15 に鳴り、以後 24 通）
--
-- **鳴った日に何をするかが 1 つに決まる閾値**を選ぶ。8 日なら答えは常に
-- 「`fit_baseline --upload` を回して、測ってから昇格する」である。
--
-- いまの active（`baseline-b3-v0-20260912`、最終学習日 09-12）では **09-20 に初めて鳴る**。
-- ⭐ 9/21 の当てはめ直し（プロファイルに `sat` と `sun_holiday` が 2 日ずつ入る最初の版。
-- §7.2）の**前日**に当たるので、W5 の間は「予定を思い出させる 1 通」として働く。
--
-- **閾値は `app_config` に置く**（`infer_alert_s` と同じ作法。W1-29）。周期を変えたときに
-- マイグレーションを書かずに追随できる。
--
-- ────────────────────────────────────────────────────────────────
-- 昇格は自動化しない
-- ────────────────────────────────────────────────────────────────
-- この検査がするのは**通知だけ**である。`trigger_infer` のように「自分で叩いて直す」形に
-- しないのは、直し方が **`active` の切り替え**を含むから（CLAUDE.md §6「モデルの `active`
-- 切替・ロールバックは確認なしに実行しない」）。0038 は `promote_model_version` に誰にも
-- grant していない。**この migration もそれを触らない。**
-- `apps/ml/tests/test_no_auto_promote.py` が、呼ぶ形がコードに現れていないことを見張る。

-- ────────────────────────────────────────────────────────────────
-- 閾値
-- ────────────────────────────────────────────────────────────────
insert into public.app_config (key, value) values
  ('model_stale_days', '8')
on conflict (key) do nothing;

-- ────────────────────────────────────────────────────────────────
-- 検査 7：配信中のモデルの学習データの古さ
-- ────────────────────────────────────────────────────────────────
-- **最終学習日は `max(train_days)` で採る。** 末尾の要素ではない——列の説明は「昇順」だが、
-- 並びを前提にすると、いつか昇順でない行が入ったときに**古い日を新しい日として読む**
-- （鳴るべきときに鳴らない側に倒れる）。要素は高々 30 個なので数え直しは安い。
--
-- `train_days` は `text[]` なので `::date` は失敗しうる。**ここでは捕まえない**：
-- `monitor_jobs` が検査ごとに例外を切り分けて `monitor_check_failed:` で鳴らす（0020）。
-- 日付でない値が入っているのは登録側の異常で、「古い」とは別の報せ方が要る。
create or replace function public.check_model_freshness()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row    record;
  v_limit  integer := public.config_int('model_stale_days', 8);
  v_today  date    := (timezone('Asia/Tokyo', now()))::date;
  v_age    integer;
  v_alerts integer := 0;
begin
  select m.model_version, m.kind, m.promoted_at,
         (select max(d::date) from unnest(m.train_days) d) as last_train_day
    into v_row
    from public.model_versions m
   where m.status = 'active';

  -- **配信するモデルが無い。** `/ml/infer` は版を引けずに止まるので、
  -- `check_inference` も遅れて鳴るが、あちらの文面は「予測が古い」になる。
  -- 原因を言えるのはここだけである
  if not found then
    if public.send_alert('model_missing', jsonb_build_object(
         'message', '配信中のモデルがありません（model_versions に status = ''active'' の行が無い）',
         'hint', 'fit_baseline --upload で作り、測ってから promote_model_version() で切り替える'),
       interval '6 hours') then
      v_alerts := 1;
    end if;
    return jsonb_build_object('active', null, 'alerts', v_alerts, 'threshold_days', v_limit);
  end if;

  v_age := v_today - v_row.last_train_day;
  if v_age >= v_limit then
    if public.send_alert('model_stale', jsonb_build_object(
         'message', '配信中の ' || v_row.model_version || ' は '
                    || v_age || ' 日前までのデータで当てはめたものです（'
                    || v_limit || ' 日で通知）',
         'hint', 'fit_baseline --upload で当てはめ直し、測ってから promote_model_version() で切り替える',
         'model_version', v_row.model_version,
         'kind', v_row.kind,
         'last_train_day', v_row.last_train_day,
         'promoted_at', v_row.promoted_at,
         'age_days', v_age,
         'threshold_days', v_limit), interval '6 hours') then
      v_alerts := 1;
    end if;
  end if;

  return jsonb_build_object('active', v_row.model_version, 'last_train_day', v_row.last_train_day,
                            'age_days', v_age, 'threshold_days', v_limit, 'alerts', v_alerts);
end;
$$;

comment on function public.check_model_freshness() is
  'active なモデルの最終学習日が model_stale_days より古ければ通知する。active が無い場合も通知する。昇格はしない（CLAUDE.md §6）。';

revoke all on function public.check_model_freshness() from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- monitor_jobs に 7 つ目として組み込む
-- ────────────────────────────────────────────────────────────────
-- 本体は 0029 のまま。**並びの末尾に足すだけ**（検査どうしは独立していて、
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
                           'check_model_freshness'];
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
  '7 つの検査を例外を切り分けて呼ぶ。1 つ落ちても他は走り、落ちた事実は detail・通知・status の 3 つに残る。';
