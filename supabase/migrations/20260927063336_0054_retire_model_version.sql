-- ────────────────────────────────────────────────────────────────
-- 0054 — shadow を下ろす口 `retire_model_version`（W6 の PR E、W6-07、開発プランの D-36）
-- ────────────────────────────────────────────────────────────────
-- 0038 の `promote_model_version(p_version, p_status)` は、行き先が `active` か `shadow` だけ
-- である。**代わりを立てずに shadow を retired にする方法が無い**（W6 プランの所見 190）。
-- 予行演習（10/7）の後にも、合成器を active にした後（10/13）にも要る——後者を忘れると、
-- **LightGBM の shadow が全行を歩き続け、森の費用が 2 倍になる**。
--
-- 決まりは 3 つ（W6 の契約 39）。
--   * **active は下ろせない。** 下ろしたいなら、別の版を `promote_model_version` で active に
--     する（前の active はそちらが retired にする）。配る版が 0 個になる瞬間を作らない
--   * **shadow と candidate を retired にする。** retired をもう 1 度下ろしても何も変わらない
--   * **誰にも grant しない。** 昇格と同じく、所有者が psql から呼ぶ（CLAUDE.md §6）。
--     `ci.yml` の見張り（契約 22）が、呼ぶ形をコードとワークフローに作らせない
--
-- **行を `for update` で掴んでから見る。** 同じ版を昇格と退役が同時に動かしても、
-- 後から来たほうは先のほうが決めた状態を見る（active になった直後の版を下ろさない）。
-- ────────────────────────────────────────────────────────────────

create or replace function public.retire_model_version(p_version text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_previous text;
begin
  select status into v_previous
    from public.model_versions
   where model_version = p_version
     for update;
  if not found then
    raise exception '登録されていない版です: %', p_version using errcode = '22023';
  end if;
  if v_previous = 'active' then
    raise exception 'active は下ろせません（別の版を promote_model_version で active にする）: %',
      p_version using errcode = '55006';
  end if;

  update public.model_versions set status = 'retired' where model_version = p_version;
  return jsonb_build_object(
    'model_version', p_version, 'status', 'retired', 'previous', v_previous);
end;
$$;

comment on function public.retire_model_version(text) is
  'shadow・candidate を retired にする。active は拒む。**誰にも grant しない**（所有者が psql から呼ぶ。CLAUDE.md §6、W6 の契約 39）。';

-- **誰にも渡さない。** ローカルは既定権限で `service_role` に EXECUTE が付くので、明示的に剥がす
-- （0038 の `promote_model_version` と同じ。環境によって権限が違う状態を残さない）
revoke all on function public.retire_model_version(text)
  from public, anon, authenticated, service_role;

notify pgrst, 'reload schema';
