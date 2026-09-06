-- 0008 通知の抑制状態と、収集周期の設定（W1 プラン §6.7、§5 の 20）
--
-- 同じ事象を繰り返し通知しないための状態を持つ。Webhook が未設定でもここには記録し、
-- ジョブは失敗させない。「通知が届かなかった」と「何も起きていなかった」を区別できる。

create table public.alert_state (
  alert_key     text primary key,
  first_seen_at timestamptz not null default now(),
  last_sent_at  timestamptz,
  last_value    jsonb
);

comment on table public.alert_state is
  '通知の抑制状態。Webhook が無くてもここには残るので、後から「何が起きていたか」を追える。';
comment on column public.alert_state.alert_key is
  '事象の識別子。同じ key は抑制間隔の内側では 1 回しか送らない。例: feed_stalled:hellocycling';
comment on column public.alert_state.last_sent_at is '最後に Webhook へ送った時刻。null なら一度も送っていない。';
comment on column public.alert_state.last_value is '最後に検知した内容。送らなかった場合も更新する。';

alter table public.alert_state enable row level security;

-- ────────────────────────────────────────────────────────────────
-- 収集周期を設定値にする（W1-29）
-- ────────────────────────────────────────────────────────────────
-- 監視の閾値は「収集がどれくらいの間隔で走っているか」に依存する。E1 の間は `*/5` で、
-- E2 で毎分になる。ここを設定値にしておくと、**E2 は vercel.json と この 1 行を
-- 変えるだけ**で、ウォッチドッグの閾値もフィード停滞の閾値も一緒に追随する。
--
-- 特に効くのがドコモ。停滞の閾値は「期待周期の 3 倍」だが、収集が 5 分間隔の E1 では
-- 4 分を必ず超えるため、素朴に書くと誤報が鳴り続ける。閾値を
-- `max(expected_cadence_s * 3, collect_interval_s * 3)` にすると、
-- E1 では 15 分・E2 では 4 分となり、**手で無効化しなくても誤報が消える**。
insert into public.app_config (key, value)
values
  ('collect_interval_s', '300'),
  ('alert_webhook_kind', 'discord')
on conflict (key) do nothing;

comment on table public.app_config is
  '秘密でない設定値。秘密は Vault に置く。collect_interval_s は監視の閾値の基準になる（W1-29）。';
