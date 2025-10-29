from decimal import Decimal
import json

def _handle_spot_error(e, send_telegram_func, context="Spot Trade"):
    try:
        parsed = json.loads(str(e))
        reason = parsed.get('error_response', {}).get('preview_failure_reason', '')
        send_telegram_func(f"❌ {context} Error: `{reason or e}`")
    except Exception:
        send_telegram_func(f"❌ {context} Unexpected Error: {e}")

def place_spot_tp(exchange, symbol, amount, target_price, send_telegram_func):
    try:
        exchange.create_limit_sell_order(symbol, amount, target_price)
        send_telegram_func(f"✅ Spot TP Order Placed — {symbol} at ${target_price} for {amount} units")
    except Exception as e:
        _handle_spot_error(e, send_telegram_func, context="Spot TP Placement")

def spot_trade(exchange, market_symbol, amount_usd, send_telegram_func, target_price=None):
    try:
        balance = exchange.fetch_balance()
        usdc_balance = balance['total'].get('USDC', 0)

        if usdc_balance < amount_usd:
            send_telegram_func(f"🚫 Skipped Spot Trade — Not enough USDC.\nAvailable: ${usdc_balance}\nNeeded: ${amount_usd}")
            return None

        price_raw = exchange.fetch_ticker(market_symbol)['last']
        if price_raw is None:
            send_telegram_func(f"⚠️ Spot price not available for {market_symbol}. Skipping trade.")
            return None

        price = Decimal(str(price_raw))
        amount_usd = Decimal(str(amount_usd))

        send_telegram_func(f"🚀 Executing Spot Buy — {market_symbol}\nAmount: ${amount_usd}\nMarket Price: ${price}")

        exchange.create_order(
            symbol=market_symbol,
            type='market',
            side='buy',
            amount=None,
            params={"cost": float(amount_usd)}
        )

        send_telegram_func(f"✅ Spot Buy Executed at ${price}")

        if target_price:
            amount_bought = float(amount_usd / price)
            place_spot_tp(exchange, market_symbol, amount_bought, target_price, send_telegram_func)

        return float(amount_usd)

    except Exception as e:
        _handle_spot_error(e, send_telegram_func)
        return None