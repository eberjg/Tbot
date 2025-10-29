from decimal import Decimal
import json

def place_spot_tp(exchange, symbol, amount, target_price, send_telegram_func):
    try:
        exchange.create_limit_sell_order(symbol, amount, target_price)
        send_telegram_func(f"✅ Spot TP Order Placed — {symbol} at ${target_price} for {amount} units")
    except Exception as e:
        send_telegram_func(f"❌ Spot TP placement failed: {e}")

def spot_trade(exchange, market_symbol, amount_usd, send_telegram_func, target_price=None):
    try:
        balance = exchange.fetch_balance()
        usdc_balance = balance['total'].get('USDC', 0)
        if usdc_balance < amount_usd:
            send_telegram_func(
                f"🚫 Skipped Spot Trade: Not enough USDC.\nAvailable: ${usdc_balance}\nNeeded: ${amount_usd}"
            )
            return None

        price_raw = exchange.fetch_ticker(market_symbol)['last']
        if price_raw is None:
            send_telegram_func(f"⚠️ SPOT price not available for {market_symbol}. Skipping trade.")
            return None

        price = Decimal(str(price_raw))
        amount_usd = Decimal(str(amount_usd))

        send_telegram_func(
            f"⚡️ Executing SPOT trade on {market_symbol} for ${amount_usd}...\nMarket Price: ${price}"
        )

        # ✅ Execute market buy order with cost param
        exchange.create_order(
            symbol=market_symbol,
            type='market',
            side='buy',
            amount=None,
            params={"cost": float(amount_usd)}
        )

        send_telegram_func(
            f"✅ *SPOT TRADE EXECUTED*\nSymbol: {market_symbol}\nUSD Spent: ${amount_usd}\nExecution Price: ${price}"
        )

        # ✅ Place TP order if provided
        if target_price:
            amount_bought = float(amount_usd / price)
            place_spot_tp(exchange, market_symbol, amount_bought, target_price, send_telegram_func)

        return float(amount_usd)

    except Exception as e:
        try:
            parsed = json.loads(str(e))
            reason = parsed.get('error_response', {}).get('preview_failure_reason', '')
            send_telegram_func(f"❌ SPOT Trade Error: `{reason or e}`")
        except Exception:
            send_telegram_func(f"❌ SPOT TRADE ERROR: {e}")
        return None