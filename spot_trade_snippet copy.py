from decimal import Decimal
import json

def spot_trade(exchange, market_symbol, amount_usd, send_telegram_func):
    try:
        balance = exchange.fetch_balance()
        usdc_balance = balance['total'].get('USDC', 0)
        if usdc_balance < amount_usd:
            msg = (
                f"🚫 Skipped Spot Trade: Not enough USDC.\n"
                f"Available: ${usdc_balance}\nNeeded: ${amount_usd}"
            )
            print(f"[SKIPPED] {msg}")
            send_telegram_func(msg)
            return None

        price_raw = exchange.fetch_ticker(market_symbol)['last']
        if price_raw is None:
            send_telegram_func(f"⚠️ SPOT price not available for {market_symbol}. Skipping trade.")
            return None

        print(f"[DEBUG] Price Raw: {price_raw} | Amount USD Raw: {amount_usd}")

        price = Decimal(str(price_raw))
        amount_usd = Decimal(str(amount_usd))

        print(f"[DEBUG] SPOT validated price: {price}, amount_usd: {amount_usd}")

        send_telegram_func(
            f"⚡️ Preparing SPOT trade on {market_symbol} for ${amount_usd}...\n"
            f"Market Price: ${price}"
        )

        # ✅ Pass cost param, Coinbase will use this instead of amount
        order = exchange.create_order(
            symbol=market_symbol,
            type='market',
            side='buy',
            amount=None,
            params={
                "cost": float(amount_usd)
            }
        )

        send_telegram_func(
            f"✅ *SPOT TRADE EXECUTED*\n"
            f"Symbol: {market_symbol}\n"
            f"USD Spent: ${amount_usd}\n"
            f"Market Price at Execution: ${price}"
        )

        return order

    except Exception as e:
        print(f"[DEBUG] Spot Trade Exception: {e}")
        try:
            parsed = json.loads(str(e))
            reason = parsed.get('error_response', {}).get('preview_failure_reason', '')
            send_telegram_func(f"❌ SPOT Trade Error: `{reason or e}`")
        except Exception:
            send_telegram_func(f"❌ SPOT TRADE ERROR: {e}")
        return None