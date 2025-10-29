from decimal import Decimal, ROUND_DOWN
import json

def futures_trade(exchange, market_symbol, amount_usd, send_telegram_func, stop=None, target=None):
    try:
        balance = exchange.fetch_balance()
        futures_usdc = balance['total'].get('USDC', 0)
        if futures_usdc < amount_usd:
            msg = (
                f"🚫 Skipped FUTURES trade: Not enough USDC in Futures Wallet.\n"
                f"Available: ${futures_usdc}\nNeeded: ${amount_usd}"
            )
            print(f"[SKIPPED] {msg}")
            send_telegram_func(msg)
            return None

        market = exchange.market(market_symbol)
        price_raw = exchange.fetch_ticker(market_symbol)['last']
        if price_raw is None:
            send_telegram_func(f"⚠️ FUTURES price not available for {market_symbol}. Skipping trade.")
            return None

        print(f"[DEBUG] Price Raw: {price_raw} | Amount USD Raw: {amount_usd}")

        price = Decimal(str(price_raw))
        amount_usd = Decimal(str(amount_usd))
        print(f"[DEBUG] FUTURES validated price: {price}, amount_usd: {amount_usd}")

        precision = market.get('precision', {}).get('amount')
        if not isinstance(precision, int) or precision < 0:
            precision = 0
        quant_str = f'1e-{precision}' if precision > 0 else '1.0'
        min_contract_size = Decimal(quant_str)

        contracts = (amount_usd / price).quantize(min_contract_size, rounding=ROUND_DOWN)
        if contracts < min_contract_size:
            msg = (
                f"[SKIPPED] ⚠️ FUTURES trade too small for {market_symbol}\n"
                f"Price: ${price} | USD: ${amount_usd} | Contracts: {contracts}\n"
                f"🔎 Try a higher `amount_usd` or cheaper asset."
            )
            print(msg)
            send_telegram_func(msg)
            return None

        int_contracts = int(contracts)
        send_telegram_func(f"⚡ LIVE FUTURES TRADE\nSymbol: {market_symbol}\nContracts: {int_contracts}\nPrice: ${price}")

        order = exchange.create_market_buy_order(
            symbol=market_symbol,
            amount=int_contracts,
            params={
                "leverage": "5"
            }
        )

        send_telegram_func(f"✅ Futures Trade Executed at ${price}")

        if stop and target:
            send_telegram_func(f"📊 SL: ${stop}, TP: ${target}")

        return order

    except Exception as e:
        print(f"[DEBUG] Raw Exception: {e}")
        try:
            parsed = json.loads(str(e))
            reason = parsed.get('error_response', {}).get('preview_failure_reason', '')
            if 'INSUFFICIENT_FUNDS_FOR_FUTURES' in reason:
                send_telegram_func(
                    "🚫 *Trade Blocked*: _Insufficient funds in Coinbase Futures Wallet_\n"
                    "💡 Make sure to transfer USDC manually if needed."
                )
            else:
                send_telegram_func(f"❌ Coinbase Futures Error: `{reason}`")
        except Exception:
            send_telegram_func(f"❌ LIVE FUTURES TRADE ERROR: {e}")
        return None