from decimal import Decimal, ROUND_DOWN, InvalidOperation
import json

def _calculate_contracts(price, amount_usd, precision, send_telegram_func, trade_type="LONG"):
    try:
        min_contract_size = Decimal('1').scaleb(-precision) if isinstance(precision, int) and precision > 0 else Decimal('1')
    except InvalidOperation as e:
        send_telegram_func(f"❗ Futures {trade_type} min_contract_size error: {e}")
        return None

    try:
        contracts = (amount_usd / price).quantize(min_contract_size, rounding=ROUND_DOWN)
    except Exception as ex:
        send_telegram_func(f"⚠️ Fallback to integer contracts: {ex}")
        contracts = Decimal(int(amount_usd / price))

    if contracts < 1:
        send_telegram_func(f"🚫 Skipped Futures {trade_type} — Below 1 contract ({contracts}). Forcing to 1 contract for testing.")
        contracts = Decimal('1')

    return int(contracts)

def _handle_error(e, send_telegram_func, trade_type="LONG"):
    try:
        parsed = json.loads(str(e))
        reason = parsed.get('error_response', {}).get('preview_failure_reason', '')
        if 'INSUFFICIENT_FUNDS_FOR_FUTURES' in reason:
            send_telegram_func(f"🚫 Trade Blocked: Insufficient Coinbase Futures Wallet funds.")
        else:
            send_telegram_func(f"❌ Futures {trade_type} Error: `{reason or e}`")
    except Exception:
        send_telegram_func(f"❌ Futures {trade_type} Unexpected Error: {e}")

# ✅ Long Futures Trade Function
def futures_trade(exchange, market_symbol, amount_usd, send_telegram_func, stop=None, target=None):
    try:
        balance = exchange.fetch_balance()
        futures_usdc = balance['total'].get('USDC', 0)
        if futures_usdc < amount_usd:
            send_telegram_func(f"🚫 Skipped Futures LONG — Not enough USDC.\nAvailable: ${futures_usdc}\nNeeded: ${amount_usd}")
            return None

        price_raw = exchange.fetch_ticker(market_symbol)['last']
        if price_raw is None:
            send_telegram_func(f"⚠️ Futures price not available for {market_symbol}. Skipping.")
            return None

        price = Decimal(str(price_raw))
        amount_usd = Decimal(str(amount_usd))
        market = exchange.market(market_symbol)
        precision = market.get('precision', {}).get('amount', 0)

        int_contracts = _calculate_contracts(price, amount_usd, precision, send_telegram_func, "LONG")
        if int_contracts is None:
            return None

        send_telegram_func(f"🚀 Executing Futures LONG — {market_symbol}\nContracts: {int_contracts}\nEntry Price: ${price}")

        exchange.create_market_buy_order(symbol=market_symbol, amount=int_contracts, params={"leverage": "5"})

        send_telegram_func(f"✅ Futures LONG Executed at ${price}")

        if stop and target:
            send_telegram_func(f"📊 Reference SL: ${stop} | TP: ${target}")

        return int_contracts

    except Exception as e:
        _handle_error(e, send_telegram_func, "LONG")
        return None

# ✅ Short Futures Trade Function
def futures_short_trade(exchange, market_symbol, amount_usd, send_telegram_func, stop=None, target=None):
    try:
        balance = exchange.fetch_balance()
        futures_usdc = balance['total'].get('USDC', 0)
        if futures_usdc < amount_usd:
            send_telegram_func(f"🚫 Skipped Futures SHORT — Not enough USDC.\nAvailable: ${futures_usdc}\nNeeded: ${amount_usd}")
            return None

        price_raw = exchange.fetch_ticker(market_symbol)['last']
        if price_raw is None:
            send_telegram_func(f"⚠️ Futures price not available for {market_symbol}. Skipping SHORT.")
            return None

        price = Decimal(str(price_raw))
        amount_usd = Decimal(str(amount_usd))
        market = exchange.market(market_symbol)
        precision = market.get('precision', {}).get('amount', 0)

        int_contracts = _calculate_contracts(price, amount_usd, precision, send_telegram_func, "SHORT")
        if int_contracts is None:
            return None

        send_telegram_func(f"🚀 Executing Futures SHORT — {market_symbol}\nContracts: {int_contracts}\nEntry Price: ${price}")

        exchange.create_market_sell_order(symbol=market_symbol, amount=int_contracts, params={"leverage": "5"})

        send_telegram_func(f"✅ Futures SHORT Executed at ${price}")

        if stop and target:
            send_telegram_func(f"📊 Reference SL: ${stop} | TP: ${target}")

        return int_contracts

    except Exception as e:
        _handle_error(e, send_telegram_func, "SHORT")
        return None