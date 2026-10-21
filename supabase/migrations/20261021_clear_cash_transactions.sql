begin;

drop policy if exists "Users can delete their own cash transactions"
    on public.cash_transactions;
create policy "Users can delete their own cash transactions"
    on public.cash_transactions
    for delete
    to authenticated
    using ((select auth.uid()) = user_id);

grant delete on public.cash_transactions to authenticated;

create or replace function public.clear_cash_transactions()
returns void
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_user_id uuid := auth.uid();
begin
    if v_user_id is null then
        raise exception 'Authentication is required to clear cash transactions.';
    end if;

    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(v_user_id::text, 0)
    );

    delete from public.cash_transactions where user_id = v_user_id;
end;
$function$;

revoke all on function public.clear_cash_transactions() from public, anon, authenticated;
grant execute on function public.clear_cash_transactions() to authenticated;

notify pgrst, 'reload schema';

commit;
