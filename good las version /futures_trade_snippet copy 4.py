from decimal import Decimal, ROUND_DOWN, InvalidOperation
import json

# ✅ Long Futures Trade Function
def futures_trade(exchange, market_symbol, amount_usd, send_telegram_func, stop=None, target=None):
    try:
        balance = exchange.fetch_balance()
        futures_usdc = balance['total'].get('USDC', 0)
        if futures_usdc < amount_usd:
            send_telegram_func(f"🚫 Skipped FUTURES: Not enough USDC. Available: ${futures_usdc}, Needed: ${amount_usd}")
            return None

        price_raw = exchange.fetch_ticker(market_symbol)['last']
        if price_raw is None:
            send_telegram_func(f"⚠️ FUTURES price not available for {market_symbol}. Skipping.")
            return None

        price = Decimal(str(price_raw))
        amount_usd = Decimal(str(amount_usd))

        market = exchange.market(market_symbol)
        precision = market.get('precision', {}).get('amount', 0)

        try:
            min_contract_size = Decimal('1').scaleb(-precision) if isinstance(precision, int) and precision > 0 else Decimal('1')
        except InvalidOperation as e:
            send_telegram_func(f"❗ Futures min_contract_size error: {e}")
            return None

        try:
            contracts = (amount_usd / price).quantize(min_contract_size, rounding=ROUND_DOWN)
        except Exception as ex:
            send_telegram_func(f"⚠️ Fallback to integer contracts: {ex}")
            contracts = Decimal(int(amount_usd / price))

        if contracts < min_contract_size:
            send_telegram_func(f"[SKIPPED] FUTURES trade too small: {contracts}. Increase amount_usd.")
            return None

        int_contracts = int(contracts)
        send_telegram_func(f"⚡ LIVE FUTURES TRADE — {market_symbol} | Contracts: {int_contracts} | Entry: ${price}")

        exchange.create_market_buy_order(
            symbol=market_symbol,
            amount=int_contracts,
            params={"leverage": "5"}
        )

        send_telegram_func(f"✅ Futures Trade Executed at ${price}")

        if stop and target:
            send_telegram_func(f"📊 For Reference Only — SL: ${stop}, TP: ${target}")

        return int_contracts

    except Exception as e:
        try:
            parsed = json.loads(str(e))
            reason = parsed.get('error_response', {}).get('preview_failure_reason', '')
            if 'INSUFFICIENT_FUNDS_FOR_FUTURES' in reason:
                send_telegram_func("🚫 *Trade Blocked*: Insufficient funds in Coinbase Futures Wallet.")
            else:
                send_telegram_func(f"❌ Coinbase Futures Error: `{reason or e}`")
        except Exception:
            send_telegram_func(f"❌ LIVE FUTURES ERROR: {e}")
        return None


# ✅ Short Futures Trade Function
def futures_short_trade(exchange, market_symbol, amount_usd, send_telegram_func, stop=None, target=None):
    try:
        balance = exchange.fetch_balance()
        futures_usdc = balance['total'].get('USDC', 0)
        if futures_usdc < amount_usd:
            send_telegram_func(f"🚫 Skipped SHORT FUTURES: Not enough USDC. Available: ${futures_usdc}, Needed: ${amount_usd}")
            return None

        price_raw = exchange.fetch_ticker(market_symbol)['last']
        if price_raw is None:
            send_telegram_func(f"⚠️ FUTURES price not available for {market_symbol}. Skipping SHORT.")
            return None

        price = Decimal(str(price_raw))
        amount_usd = Decimal(str(amount_usd))

        market = exchange.market(market_symbol)
        precision = market.get('precision', {}).get('amount', 0)

        try:
            min_contract_size = Decimal('1').scaleb(-precision) if isinstance(precision, int) and precision > 0 else Decimal('1')
        except InvalidOperation as e:
            send_telegram_func(f"❗ Futures Short min_contract_size error: {e}")
            return None

        try:
            contracts = (amount_usd / price).quantize(min_contract_size, rounding=ROUND_DOWN)
        except Exception as ex:
            send_telegram_func(f"⚠️ Short fallback to integer contracts: {ex}")
            contracts = Decimal(int(amount_usd / price))

        if contracts < min_contract_size:
            send_telegram_func(f"[SKIPPED] SHORT trade too small: {contracts}. Increase amount_usd.")
            return None

        int_contracts = int(contracts)
        send_telegram_func(f"⚡ LIVE SHORT FUTURES TRADE — {market_symbol} | Contracts: {int_contracts} | Entry: ${price}")

        exchange.create_market_sell_order(
            symbol=market_symbol,
            amount=int_contracts,
            params={"leverage": "5"}
        )

        send_telegram_func(f"✅ SHORT Futures Trade Executed at ${price}")

        if stop and target:
            send_telegram_func(f"📊 For Reference Only — SL: ${stop}, TP: ${target}")

        return int_contracts

    except Exception as e:
        try:
            parsed = json.loads(str(e))
            reason = parsed.get('error_response', {}).get('preview_failure_reason', '')
            if 'INSUFFICIENT_FUNDS_FOR_FUTURES' in reason:
                send_telegram_func("🚫 *SHORT Trade Blocked*: Insufficient funds in Coinbase Futures Wallet.")
            else:
                send_telegram_func(f"❌ Coinbase SHORT Futures Error: `{reason or e}`")
        except Exception:
            send_telegram_func(f"❌ LIVE SHORT FUTURES ERROR: {e}")
        return None