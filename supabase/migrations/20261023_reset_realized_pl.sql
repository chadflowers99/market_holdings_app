begin;

create table if not exists public.realized_pl_baselines (
    user_id uuid primary key references auth.users(id) on delete cascade,
    baseline numeric not null default 0
);

alter table public.realized_pl_baselines enable row level security;

drop policy if exists "Users can read their own P/L baseline"
    on public.realized_pl_baselines;
create policy "Users can read their own P/L baseline"
    on public.realized_pl_baselines for select to authenticated
    using ((select auth.uid()) = user_id);

drop policy if exists "Users can insert their own P/L baseline"
    on public.realized_pl_baselines;
create policy "Users can insert their own P/L baseline"
    on public.realized_pl_baselines for insert to authenticated
    with check ((select auth.uid()) = user_id);

drop policy if exists "Users can update their own P/L baseline"
    on public.realized_pl_baselines;
create policy "Users can update their own P/L baseline"
    on public.realized_pl_baselines for update to authenticated
    using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);

revoke all on public.realized_pl_baselines from anon, authenticated;
grant select, insert, update on public.realized_pl_baselines to authenticated;

create or replace function public.reset_realized_pl_total()
returns void
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_user_id uuid := auth.uid();
begin
    if v_user_id is null then
        raise exception 'Authentication is required to reset P/L.';
    end if;

    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(v_user_id::text, 0)
    );

    insert into public.realized_pl_baselines (user_id, baseline)
        select v_user_id, coalesce(sum(realized_pl::numeric), 0)
        from public.permanent_ledger where user_id = v_user_id
    on conflict (user_id) do update set baseline = excluded.baseline;
end;
$function$;

revoke all on function public.reset_realized_pl_total() from public, anon, authenticated;
grant execute on function public.reset_realized_pl_total() to authenticated;

notify pgrst, 'reload schema';
commit;
