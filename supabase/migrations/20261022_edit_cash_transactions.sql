begin;

drop policy if exists "Users can update their own cash transactions"
    on public.cash_transactions;
create policy "Users can update their own cash transactions"
    on public.cash_transactions for update to authenticated
    using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete their own cash transactions"
    on public.cash_transactions;
create policy "Users can delete their own cash transactions"
    on public.cash_transactions for delete to authenticated
    using ((select auth.uid()) = user_id);

grant update (transaction_type, amount_cents, timestamp, note), delete
    on public.cash_transactions to authenticated;

create or replace function public.change_cash_transaction(
    p_id bigint,
    p_delete boolean default false,
    p_transaction_type text default null,
    p_amount_cents bigint default null,
    p_timestamp timestamptz default null,
    p_note text default ''
)
returns void
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_user_id uuid := auth.uid();
    v_old public.cash_transactions%rowtype;
    v_balance numeric;
    v_old_amount numeric;
    v_new_amount numeric := 0;
begin
    if v_user_id is null then
        raise exception 'Authentication is required to change cash transactions.';
    end if;
    if p_delete is null then
        raise exception 'Specify whether to delete or update the transaction.';
    end if;
    if not p_delete then
        if p_transaction_type is null or p_transaction_type not in ('deposit', 'withdrawal') then
            raise exception 'Transaction must be a deposit or withdrawal.';
        end if;
        if p_amount_cents is null or p_amount_cents <= 0 then
            raise exception 'Amount must be greater than zero.';
        end if;
        if p_timestamp is null or not pg_catalog.isfinite(p_timestamp) then
            raise exception 'Enter a valid transaction date.';
        end if;
        v_new_amount := case p_transaction_type
            when 'deposit' then p_amount_cents::numeric else -p_amount_cents::numeric end;
    end if;

    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(v_user_id::text, 0)
    );

    select * into v_old from public.cash_transactions
        where id = p_id and user_id = v_user_id for update;
    if not found then
        raise exception 'Transaction not found. Refresh and try again.';
    end if;
    select coalesce(sum(case transaction_type
        when 'deposit' then amount_cents::numeric else -amount_cents::numeric end), 0)
        into v_balance from public.cash_transactions where user_id = v_user_id;
    v_old_amount := case v_old.transaction_type
        when 'deposit' then v_old.amount_cents::numeric else -v_old.amount_cents::numeric end;
    if v_balance - v_old_amount + v_new_amount < 0 then
        raise exception 'This change would make the available cash balance negative.';
    end if;

    if p_delete then
        delete from public.cash_transactions where id = p_id and user_id = v_user_id;
    else
        update public.cash_transactions set
            transaction_type = p_transaction_type,
            amount_cents = p_amount_cents,
            timestamp = p_timestamp,
            note = coalesce(p_note, '')
        where id = p_id and user_id = v_user_id;
    end if;
end;
$function$;

revoke all on function public.change_cash_transaction(bigint, boolean, text, bigint, timestamptz, text)
    from public, anon, authenticated;
grant execute on function public.change_cash_transaction(bigint, boolean, text, bigint, timestamptz, text)
    to authenticated;

notify pgrst, 'reload schema';
commit;
