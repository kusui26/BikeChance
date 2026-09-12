-- 0042 予測ログのバケット（開発プラン §8.2・D-24、W5 プラン §6.1 の PR A）
--
-- **配った確率を、あとから測れる形で残す。** `station_forecasts` は 1 ポート 1 行で
-- **5 分毎に上書きされる**ので、記録しなかったサイクルは「作り直す」しかない。
-- 作り直しには `status_snapshots`（60 日）と `weather_hourly`（**30 日**）が要るので、
-- **30 日の導火線がある**（W5-10）。W6 の `shadow` の前提でもある（§8.4）。
--
-- パスは `{system}/date=YYYY-MM-DD/hour=HH/{base_epoch_s}_{model_version}.parquet`
-- （**UTC**。結合する相手が `gbfs-parquet` の実測だから。W4 プラン §6.8 の PR N）。
-- 形は `apps/ml/bikechance_ml/jobs/forecast_log.py` が持つ。
--
-- **`gbfs-parquet` に相乗りさせない。** 中身は同じ Parquet で MIME も通るが、
-- **寿命と作り直し方が違う**：あちらは無期限の学習用アーカイブで、こちらは
-- **12 か月保持**の記録である。混ぜると保持期間を別々に決められない
-- （0027 が `models` で採ったのと同じ判断。W3 プラン §12 の 106）。
--
-- **保持の規則（12 か月）は `run_maintenance` にまだ足さない。** 最初に消す対象が
-- 出るのは 2027-09 で、それまで**動かない規則が 1 つ増えるだけ**になる。
-- 消し方を決めるのは D-22（`features/` の保持）と同じ時期——Storage が 60 GB に
-- 達する 2027-11 ごろで、そこで両方まとめて決める（開発プラン §4.5b）。
--
-- `supabase db diff` は Storage バケットを検出しないため手書きで管理する
-- （W1 プラン §4.3 の 17）。0001・0015・0017・0027 と同じ形にしてある。

do $$
begin
  if not exists (
    select 1 from information_schema.schemata where schema_name = 'storage'
  ) then
    raise notice 'storage スキーマが無いためバケット作成をスキップした';
    return;
  end if;

  insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
  values (
    'forecast-log',
    'forecast-log',
    false,               -- 非公開。読み書きはサービスロールのみ
    52428800,            -- 50 MiB。実測は 1 サイクル HELLO 93 KB / ドコモ 36 KB
    -- 0017 と同じ IANA 登録済みの型。Content-Type を付けない実装を弾く
    array['application/vnd.apache.parquet']
  )
  on conflict (id) do nothing;
end
$$;

-- ────────────────────────────────────────────────────────────────
-- 検査 6（`check_inference`）に「予測ログが置けていない」を足す
-- ────────────────────────────────────────────────────────────────
-- **置けなくても推論は落とさない**（W3-18。配ることのほうが大事で、ログは作り直せる）。
-- だから成否は `inference_log.detail.forecast_log` にしか出ない——**そして誰も見ない**。
--
-- **これは R19 と同じ形である**：「ジョブの失敗と Parquet の欠落を誰も見ていない」。
-- 天気（R16）ほどではないが、ここにも**期限がある**：作り直しには `status_snapshots`
-- （60 日）と `weather_hourly`（**30 日**）が要り、1 日ぶんで約 1.5 CPU 時かかる。
-- **数日気づかなければ、気づいた頃には作り直しも高くつく。**
--
-- 0031 が「閉じられなかった行」を同じ関数に足したのと同じ形にする（検査の数は 6 のまま）。
-- **通知は 6 時間に 1 回**（`inference_stuck` と同じ）：5 分毎に鳴らす種類ではない。
create or replace function public.check_inference()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row     record;
  v_limit   integer := public.config_int('infer_alert_s', 900);
  v_alerts  integer := 0;
  v_checked integer := 0;
  v_oldest  integer := null;
  v_age     integer;
  v_stuck   integer;
  v_systems text;
  v_unlogged integer;
  v_reason   text;
begin
  for v_row in
    select sy.system_id,
           f.last_generated_at,
           f.last_base_observed_at,
           fs.last_observed_at as feed_observed_at,
           (select l.status from public.inference_log l
             where l.system_id = sy.system_id order by l.id desc limit 1) as last_status,
           (select l.error from public.inference_log l
             where l.system_id = sy.system_id order by l.id desc limit 1) as last_error
      from public.systems sy
      left join public.feed_state fs on fs.system_id = sy.system_id
      left join (
        select system_id,
               max(generated_at)      as last_generated_at,
               max(base_observed_at)  as last_base_observed_at
          from public.station_forecasts group by system_id
      ) f on f.system_id = sy.system_id
     where sy.is_active
  loop
    v_checked := v_checked + 1;
    v_age := case
               when v_row.last_generated_at is null then null
               else floor(extract(epoch from now() - v_row.last_generated_at))::integer
             end;
    if v_age is not null and (v_oldest is null or v_age > v_oldest) then
      v_oldest := v_age;
    end if;

    -- **1 度も動いていない**か、**閾値より古い**。前者は null なので比較では拾えない
    if v_row.last_generated_at is null or v_age > v_limit then
      if public.send_alert('inference_stale:' || v_row.system_id, jsonb_build_object(
           'message', v_row.system_id || ' の予測が '
                      || coalesce(round(v_age / 60.0, 1)::text || ' 分', '一度も出ていません')
                      || ' 古くなっています',
           -- **原因の当たりを付ける材料を一緒に送る。**
           --   feed_observed_at が古い     → 収集の持ち場（monitor_feeds）
           --   feed_observed_at だけ新しい → 推論の持ち場
           'last_generated_at', v_row.last_generated_at,
           'last_base_observed_at', v_row.last_base_observed_at,
           'feed_observed_at', v_row.feed_observed_at,
           'last_status', v_row.last_status,
           'last_error', v_row.last_error,
           'threshold_s', v_limit), interval '1 hour') then
        v_alerts := v_alerts + 1;
      end if;
    end if;
  end loop;

  -- **閉じられなかった行**（§12 の 111）。推論は 5 分周期で maxDuration が 240 秒なので、
  -- 閾値を過ぎて running のままなら、正常な実行中ではあり得ない
  select count(*), string_agg(distinct system_id, ',' order by system_id)
    into v_stuck, v_systems
    from public.inference_log
   where status = 'running' and generated_at < now() - make_interval(secs => v_limit);

  if v_stuck > 0 then
    if public.send_alert('inference_stuck', jsonb_build_object(
         'message', '推論の記録が ' || v_stuck || ' 件、running のまま閉じられていません',
         'systems', v_systems,
         'threshold_s', v_limit), interval '6 hours') then
      v_alerts := v_alerts + 1;
    end if;
  end if;

  -- **予測ログが置けていない**（0042）。**配信は無事なので `status` は `ok` のまま**で、
  -- 失敗は `detail.forecast_log` にしか出ない。**理由まで載せる**（W4 の PR K と同じ）
  select count(*), min(detail->>'forecast_log')
    into v_unlogged, v_reason
    from public.inference_log
   where status = 'ok'
     and generated_at > now() - interval '3 hours'
     and coalesce(detail->>'forecast_log', 'ok') <> 'ok';

  if v_unlogged > 0 then
    if public.send_alert('forecast_log_failed', jsonb_build_object(
         'message', '予測ログが直近 3 時間に ' || v_unlogged
                    || ' 回置けていません。配信は無事ですが、'
                    || 'その時刻の予測は作り直すしかありません'
                    || '（天気は 30 日で消えるので、それを過ぎると作り直せません）',
         'last_error', v_reason,
         'window', '3 hours'), interval '6 hours') then
      v_alerts := v_alerts + 1;
    end if;
  end if;

  return jsonb_build_object('checked', v_checked, 'alerts', v_alerts,
                            'oldest_age_s', v_oldest, 'stuck', v_stuck,
                            'unlogged', v_unlogged, 'threshold_s', v_limit);
end;
$$;

comment on function public.check_inference() is
  'station_forecasts の鮮度・閉じられずに running のまま残った行・予測ログを置けなかった回を通知する。';

revoke all on function public.check_inference() from public, anon, authenticated;
