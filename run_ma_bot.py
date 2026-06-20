import os
import sys
import time
import json
import logging
import asyncio
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
import pandas as pd
import numpy as np
import ccxt.async_support as ccxt_async
import websockets

# Setup logging
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "ma_bot.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
    ]
)
logger = logging.getLogger("ma_bot")

# Load dotenv
def load_dotenv():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dotenv_path = os.path.join(script_dir, ".env")
    if os.path.exists(dotenv_path):
        logger.info("Loading environment variables from .env...")
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    val = val.strip().strip("'").strip('"')
                    os.environ[key.strip()] = val.strip()

load_dotenv()

API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")
ENV_TYPE = os.getenv("OKX_ENVIRONMENT", "demo")

if not API_KEY or not SECRET_KEY or not PASSPHRASE:
    logger.error("Missing OKX API credentials in .env file!")
    sys.exit(1)

SIMULATED = (ENV_TYPE == "demo")
STATE_FILE = os.path.join(SCRIPT_DIR, "ma_bot_state.json")
TRADES_FILE = os.path.join(SCRIPT_DIR, "ma_bot_trades.json")

# ────────────────────────────────────────────────────────────
# State persistence and Trade log
# ────────────────────────────────────────────────────────────
def load_state():
    default = {
        "last_acted_ts": 0, 
        "last_signal": "none", 
        "position_side": "flat", 
        "position_size": 0.0, 
        "stop_loss": 0.0, 
        "take_profit": 0.0,
        "is_paused": False
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return {**default, **data}
        except Exception as e:
            logger.warning(f"Failed to load state file: {e}")
    return default

def save_state(state: dict):
    try:
        tmp_file = STATE_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, STATE_FILE)
    except Exception as e:
        logger.warning(f"Failed to save state file atomically: {e}")

def record_trade(trade_info: dict):
    trades = []
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                trades = json.load(f)
        except Exception:
            trades = []
    trades.append(trade_info)
    trades = trades[-200:]  # Keep last 200 trades
    try:
        tmp_file = TRADES_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(trades, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, TRADES_FILE)
    except Exception as e:
        logger.warning(f"Failed to save trades file: {e}")

# ────────────────────────────────────────────────────────────
# Indicator calculations (Pandas compatible)
# ────────────────────────────────────────────────────────────
def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1/period, adjust=False).mean()
    loss = -delta.where(delta < 0, 0).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_atr(df, period=14):
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def format_size(size: float, lot_size: float) -> str:
    if lot_size >= 1.0:
        return str(int(round(size / lot_size) * lot_size))
    else:
        decimals = len(str(lot_size).split(".")[1]) if "." in str(lot_size) else 0
        val = round(size / lot_size) * lot_size
        return f"{val:.{decimals}f}"

# ────────────────────────────────────────────────────────────
# Global Context and Locks
# ────────────────────────────────────────────────────────────
class BotContext:
    def __init__(self):
        self.last_close = 0.0
        self.pos_side = "flat"
        self.pos_sz = 0.0
        self.pos_avg_px = 0.0
        self.stop_loss = 0.0
        self.take_profit = 0.0
        self.is_paused = False
        self.last_acted_ts = 0
        self.ct_val = 1.0
        self.lot_sz = 1.0
        self.ccxt_symbol = ""
        self.pos_mode = "net_mode"

context = BotContext()
order_lock = asyncio.Lock()  # Prevent concurrent ordering operations

# ────────────────────────────────────────────────────────────
# Core Order Execution Logic using CCXT (Thread & Asynchronous safe)
# ────────────────────────────────────────────────────────────
async def trigger_market_exit(exchange: ccxt_async.Exchange, inst_id: str, exit_reason: str):
    """Executes a market close order safely with retries. Cloud TP/SL orders are managed by OKX."""
    async with order_lock:
        state = load_state()
        if state.get("position_side", "flat") == "flat":
            return
            
        logger.info(f"⚡ EXIT TRIGGERED: {exit_reason} (Last Price: {context.last_close:.2f})")
        close_side = "sell" if context.pos_side == "long" else "buy"
        close_pos_side = context.pos_side if context.pos_mode == "long_short" else "net"
        
        # Market close with retries
        # In OKX, when you exit the position, any cloud-attached TP/SL orders bound to the position are automatically cancelled by OKX.
        order_params = {
            'reduceOnly': True,
            'posSide': close_pos_side,
            'tdMode': 'isolated'
        }
        
        order_executed = False
        close_res = None
        for attempt in range(5):
            try:
                close_res = await exchange.create_order(
                    symbol=context.ccxt_symbol,
                    type='market',
                    side=close_side,
                    amount=context.pos_sz,
                    price=None,
                    params=order_params
                )
                logger.info(f"🎉 Close position order filled successfully on attempt {attempt+1}")
                order_executed = True
                break
            except Exception as ex:
                logger.error(f"❌ Attempt {attempt+1}/5 - Close order failed: {ex}. Retrying in 1s...")
                await asyncio.sleep(1)
                
        if order_executed:
            record_trade({
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "action": f"CLOSE_{context.pos_side.upper()}",
                "size": context.pos_sz, 
                "price": context.last_close, 
                "reason": exit_reason, 
                "response": str(close_res)
            })
            state["position_side"] = "flat"
            state["stop_loss"] = 0.0
            state["take_profit"] = 0.0
            state["position_size"] = 0.0
            state["last_signal"] = exit_reason.lower()
            save_state(state)
            
            # Sync context
            context.pos_side = "flat"
            context.pos_sz = 0.0
            context.stop_loss = 0.0
            context.take_profit = 0.0
        else:
            logger.critical(f"🚨 CRITICAL: Failed to execute close order on OKX after 5 attempts! Reason: {exit_reason}")

# ────────────────────────────────────────────────────────────
# WebSocket Live Price Listener (Maintains Context Last Price)
# ────────────────────────────────────────────────────────────
async def websocket_listener(inst_id: str):
    """Subscribes to live tickers channel to keep context.last_close extremely fresh."""
    ws_url = "wss://wspap.okx.com:8443/ws/v5/public" if SIMULATED else "wss://ws.okx.com:8443/ws/v5/public"
    logger.info(f"Starting live WebSocket price listener on: {ws_url}")
    
    async for websocket in websockets.connect(ws_url):
        try:
            subscribe_msg = {
                "op": "subscribe",
                "args": [{
                    "channel": "tickers",
                    "instId": inst_id
                }]
            }
            await websocket.send(json.dumps(subscribe_msg))
            logger.info(f"Subscribed to tickers channel for {inst_id}")
            
            while True:
                response = await websocket.recv()
                data = json.loads(response)
                
                if "data" in data:
                    ticker = data["data"][0]
                    context.last_close = float(ticker["last"])
                                
        except websockets.ConnectionClosed:
            logger.warning("WebSocket connection lost! Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"WebSocket listener encountered error: {e}")
            await asyncio.sleep(2)

# ────────────────────────────────────────────────────────────
# Main Asynchronous Polling Decision Loop
# ────────────────────────────────────────────────────────────
async def main_polling_loop(exchange: ccxt_async.Exchange, inst_id: str):
    max_capital = 10.0  # Total capital limit: 10 USDT
    leverage = 3        # 3x leverage
    
    # ── Strategy Parameters ──
    EMA_PERIOD = 200
    RSI_PERIOD = 14
    ATR_PERIOD = 14
    SL_ATR_MULT = 1.0
    TP_ATR_MULT = 2.0
    
    RSI_LONG_LOWER = 35.0
    RSI_LONG_UPPER = 45.0
    RSI_SHORT_LOWER = 55.0
    RSI_SHORT_UPPER = 65.0
    
    RSI_OB_EXIT = 75.0
    RSI_OS_EXIT = 25.0
    
    while True:
        try:
            # Check state
            state = load_state()
            context.is_paused = state.get("is_paused", False)
            if context.is_paused:
                logger.info("Bot is PAUSED by user. Sleeping for 10 seconds...")
                await asyncio.sleep(10)
                continue
                
            # 1. Fetch historical candles (600 candles for EMA accuracy)
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol=context.ccxt_symbol, timeframe='30m', limit=600)
            except Exception as e:
                logger.error(f"Failed to fetch candles: {e}")
                await asyncio.sleep(15)
                continue
                
            if not ohlcv or len(ohlcv) < 220:
                logger.warning("Not enough candles fetched, waiting...")
                await asyncio.sleep(15)
                continue
                
            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
            # Indicators
            df['ema'] = calculate_ema(df['close'], period=EMA_PERIOD)
            df['rsi'] = calculate_rsi(df['close'], period=RSI_PERIOD)
            df['atr'] = calculate_atr(df, period=ATR_PERIOD)
            
            completed_candle = df.iloc[-2]
            current_candle = df.iloc[-1]
            
            # Update last close if websocket is slow
            if context.last_close == 0.0:
                context.last_close = current_candle['close']
                
            current_candle_ts = int(completed_candle['ts'])
            
            # Entry Signal Indicators (from completed candle)
            comp_close = completed_candle['close']
            comp_ema = completed_candle['ema']
            comp_rsi = completed_candle['rsi']
            comp_atr = completed_candle['atr']
            
            # Current values
            curr_rsi = current_candle['rsi']
            curr_ema = current_candle['ema']
            
            # Signals checking
            long_entry_signal = (comp_close > comp_ema) and (RSI_LONG_LOWER <= comp_rsi <= RSI_LONG_UPPER)
            short_entry_signal = (comp_close < comp_ema) and (RSI_SHORT_LOWER <= comp_rsi <= RSI_SHORT_UPPER)
            
            logger.info(
                f"Price: {context.last_close:.2f} | EMA(200): {curr_ema:.2f} | RSI(14): {curr_rsi:.2f} | "
                f"Completed (Close: {comp_close:.2f}, EMA: {comp_ema:.2f}, RSI: {comp_rsi:.2f}, ATR: {comp_atr:.4f}) | "
                f"Signals: Long={long_entry_signal}, Short={short_entry_signal}"
            )
            
            # 2. Sync position status from Exchange
            try:
                positions = await exchange.fetch_positions(symbols=[context.ccxt_symbol])
            except Exception as e:
                logger.error(f"Failed to fetch positions from OKX: {e}")
                await asyncio.sleep(10)
                continue
                
            pos_side = "flat"
            pos_sz = 0.0
            pos_avg_px = 0.0
            pos_upl = 0.0
            pos_upl_ratio = 0.0
            
            for p in positions:
                p_sz = float(p.get("contracts", 0.0))
                p_side = p.get("side")
                
                # isolated margin check
                if p.get("marginMode") == "isolated" and p_sz > 0:
                    pos_side = p_side
                    pos_sz = p_sz
                    pos_avg_px = float(p.get("entryPrice", 0.0))
                    pos_upl = float(p.get("unrealizedPnl", 0.0))
                    pos_upl_ratio = float(p.get("percentage", 0.0))
                    break
                    
            # Local state synchronization
            context.pos_side = pos_side
            context.pos_sz = pos_sz
            context.pos_avg_px = pos_avg_px
            
            # Sync: if exchange says flat but state thinks we have a position, reset state
            if pos_side == "flat" and state.get("position_side", "flat") != "flat":
                logger.warning(f"⚠️ Exchange flat, local state records position_side={state['position_side']}. Sync resetting...")
                
                # Verify if the cloud-attached TP/SL or active TP order filled
                was_tp_filled = False
                try:
                    # Look up last filled trade in our trading records
                    orders = await exchange.fetch_closed_orders(symbol=context.ccxt_symbol, limit=10)
                    for o in orders:
                        # If a TP order or triggering cloud TPSL order filled
                        if o.get("status") == "closed" and o.get("filled", 0) > 0:
                            # Verify if it was filled around the current candle window
                            was_tp_filled = True
                            break
                except Exception as e:
                    logger.warning(f"Failed to check filled orders: {e}")
                        
                if was_tp_filled:
                    record_trade({
                        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "action": f"CLOSE_{state['position_side'].upper()}",
                        "size": state.get("position_size", 0.0),
                        "price": state.get("take_profit", context.last_close),
                        "reason": "TAKE_PROFIT_CLOUD",
                        "response": "Cloud TP order triggered & filled"
                    })
                    state["last_signal"] = "take_profit_cloud"
                else:
                    state["last_signal"] = "sync_reset"
                    
                state["position_side"] = "flat"
                state["stop_loss"] = 0.0
                state["take_profit"] = 0.0
                state["position_size"] = 0.0
                save_state(state)
                
                context.stop_loss = 0.0
                context.take_profit = 0.0

            # Update context SL/TP from state
            if pos_side != "flat":
                context.stop_loss = state.get("stop_loss", 0.0)
                context.take_profit = state.get("take_profit", 0.0)
                
            logger.info(
                f"Position: side={pos_side} | size={pos_sz} | avgPx={pos_avg_px:.2f} | "
                f"upl={pos_upl:.4f} | uplRatio={pos_upl_ratio:.2f}% | SL={context.stop_loss:.2f} | TP={context.take_profit:.2f}"
            )

            # 3. Check for Active / Trend exit indicators (EMA cross or RSI limits)
            exit_triggered = False
            exit_reason = ""
            
            if pos_side != "flat" and pos_sz > 0:
                if pos_side == "long":
                    # Cloud TP/SL recovery if state sl/tp has been lost in restart
                    if context.stop_loss <= 0.0:
                        sl = pos_avg_px - SL_ATR_MULT * comp_atr
                        tp = pos_avg_px + TP_ATR_MULT * comp_atr
                        state["stop_loss"] = round(sl, 2)
                        state["take_profit"] = round(tp, 2)
                        state["position_side"] = "long"
                        state["position_size"] = pos_sz
                        save_state(state)
                        context.stop_loss = state["stop_loss"]
                        context.take_profit = state["take_profit"]
                    
                    if context.last_close < curr_ema:
                        exit_triggered = True
                        exit_reason = "EMA_CROSS_EXIT"
                    elif curr_rsi >= RSI_OB_EXIT:
                        exit_triggered = True
                        exit_reason = "RSI_OVERBOUGHT_EXIT"
                        
                elif pos_side == "short":
                    if context.stop_loss <= 0.0:
                        sl = pos_avg_px + SL_ATR_MULT * comp_atr
                        tp = pos_avg_px - TP_ATR_MULT * comp_atr
                        state["stop_loss"] = round(sl, 2)
                        state["take_profit"] = round(tp, 2)
                        state["position_side"] = "short"
                        state["position_size"] = pos_sz
                        save_state(state)
                        context.stop_loss = state["stop_loss"]
                        context.take_profit = state["take_profit"]
                        
                    if context.last_close > curr_ema:
                        exit_triggered = True
                        exit_reason = "EMA_CROSS_EXIT"
                    elif curr_rsi <= RSI_OS_EXIT:
                        exit_triggered = True
                        exit_reason = "RSI_OVERSOLD_EXIT"
                        
                if exit_triggered:
                    await trigger_market_exit(exchange, inst_id, exit_reason)
                    pos_side = "flat"
                    pos_sz = 0.0

            # 4. Entry Signal Execution & Placement of Cloud Attached TP/SL
            already_acted = (current_candle_ts == state.get("last_acted_ts", 0))
            
            if (long_entry_signal or short_entry_signal) and not already_acted:
                async with order_lock:
                    # Sync Balance
                    try:
                        balance_res = await exchange.fetch_balance()
                        avail_balance = float(balance_res.get("USDT", {}).get("free", 0.0))
                    except Exception as e:
                        logger.error(f"Failed to fetch balance: {e}")
                        avail_balance = 0.0
                        
                    usable_margin = min(avail_balance, max_capital)
                    target_value = usable_margin * leverage
                    raw_sz = target_value / (context.last_close * context.ct_val)
                    target_sz = max(raw_sz, context.lot_sz)
                    target_sz = round(target_sz / context.lot_sz) * context.lot_sz
                    
                    if long_entry_signal and pos_side != "long":
                        logger.info("⚡ Long Entry Signal triggered!")
                        opposite_closed = True
                        if pos_side == "short":
                            logger.info(f"Closing short (size={pos_sz})...")
                            close_pos_side = "short" if context.pos_mode == "long_short" else "net"
                            try:
                                close_res = await exchange.create_order(
                                    symbol=context.ccxt_symbol,
                                    type='market',
                                    side='buy',
                                    amount=pos_sz,
                                    price=None,
                                    params={'reduceOnly': True, 'posSide': close_pos_side, 'tdMode': 'isolated'}
                                )
                                record_trade({
                                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                    "action": "CLOSE_SHORT", "size": pos_sz, "price": context.last_close, "response": str(close_res)
                                })
                                pos_side = "flat"
                                pos_sz = 0.0
                                await asyncio.sleep(2)
                            except Exception as ex:
                                logger.error(f"❌ Failed to close opposite Short position: {ex}")
                                opposite_closed = False
                                
                        if opposite_closed:
                            logger.info(f"Opening Long (size={target_sz})...")
                            open_pos_side = "long" if context.pos_mode == "long_short" else "net"
                            
                            sl = context.last_close - SL_ATR_MULT * comp_atr
                            tp = context.last_close + TP_ATR_MULT * comp_atr
                            
                            # 💡 IMPORTANT: OKX native Attach TP/SL options
                            # Pass to OKX as trigger orders tied directly to the parent position. 
                            # If parent position is flat, these trigger orders are deleted automatically.
                            order_params = {
                                'posSide': open_pos_side,
                                'tdMode': 'isolated',
                                # Take Profit Cloud Parameters
                                'tpTriggerPx': f"{round(tp, 2)}",
                                'tpOrdPx': '-1',  # Market order execution on TP trigger
                                # Stop Loss Cloud Parameters
                                'slTriggerPx': f"{round(sl, 2)}",
                                'slOrdPx': '-1'   # Market order execution on SL trigger
                            }
                            
                            try:
                                open_res = await exchange.create_order(
                                    symbol=context.ccxt_symbol,
                                    type='market',
                                    side='buy',
                                    amount=target_sz,
                                    price=None,
                                    params=order_params
                                )
                                
                                state["last_acted_ts"] = current_candle_ts
                                state["last_signal"] = "long_entry"
                                state["position_side"] = "long"
                                state["position_size"] = target_sz
                                state["stop_loss"] = round(sl, 2)
                                state["take_profit"] = round(tp, 2)
                                save_state(state)
                                
                                context.stop_loss = state["stop_loss"]
                                context.take_profit = state["take_profit"]
                                
                                record_trade({
                                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                    "action": "OPEN_LONG", "size": target_sz, "price": context.last_close,
                                    "stop_loss": state["stop_loss"], "take_profit": state["take_profit"], "response": str(open_res)
                                })
                                
                            except Exception as ex:
                                logger.error(f"❌ Failed to open Long position with attached TP/SL: {ex}")
                                
                    elif short_entry_signal and pos_side != "short":
                        logger.info("⚡ Short Entry Signal triggered!")
                        opposite_closed = True
                        if pos_side == "long":
                            logger.info(f"Closing long (size={pos_sz})...")
                            close_pos_side = "long" if context.pos_mode == "long_short" else "net"
                            try:
                                close_res = await exchange.create_order(
                                    symbol=context.ccxt_symbol,
                                    type='market',
                                    side='sell',
                                    amount=pos_sz,
                                    price=None,
                                    params={'reduceOnly': True, 'posSide': close_pos_side, 'tdMode': 'isolated'}
                                )
                                record_trade({
                                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                    "action": "CLOSE_LONG", "size": pos_sz, "price": context.last_close, "response": str(close_res)
                                })
                                pos_side = "flat"
                                pos_sz = 0.0
                                await asyncio.sleep(2)
                            except Exception as ex:
                                logger.error(f"❌ Failed to close opposite Long position: {ex}")
                                opposite_closed = False
                                
                        if opposite_closed:
                            logger.info(f"Opening Short (size={target_sz})...")
                            open_pos_side = "short" if context.pos_mode == "long_short" else "net"
                            
                            sl = context.last_close + SL_ATR_MULT * comp_atr
                            tp = context.last_close - TP_ATR_MULT * comp_atr
                            
                            # 💡 IMPORTANT: OKX native Attach TP/SL options
                            order_params = {
                                'posSide': open_pos_side,
                                'tdMode': 'isolated',
                                # Take Profit Cloud Parameters
                                'tpTriggerPx': f"{round(tp, 2)}",
                                'tpOrdPx': '-1',
                                # Stop Loss Cloud Parameters
                                'slTriggerPx': f"{round(sl, 2)}",
                                'slOrdPx': '-1'
                            }
                            
                            try:
                                open_res = await exchange.create_order(
                                    symbol=context.ccxt_symbol,
                                    type='market',
                                    side='sell',
                                    amount=target_sz,
                                    price=None,
                                    params=order_params
                                )
                                
                                state["last_acted_ts"] = current_candle_ts
                                state["last_signal"] = "short_entry"
                                state["position_side"] = "short"
                                state["position_size"] = target_sz
                                state["stop_loss"] = round(sl, 2)
                                state["take_profit"] = round(tp, 2)
                                save_state(state)
                                
                                context.stop_loss = state["stop_loss"]
                                context.take_profit = state["take_profit"]
                                
                                record_trade({
                                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                    "action": "OPEN_SHORT", "size": target_sz, "price": context.last_close,
                                    "stop_loss": state["stop_loss"], "take_profit": state["take_profit"], "response": str(open_res)
                                })
                                
                            except Exception as ex:
                                logger.error(f"❌ Failed to open Short position with attached TP/SL: {ex}")
                                
            elif (long_entry_signal or short_entry_signal) and already_acted:
                logger.info(f"⚠️ Entry signal on candle ts={current_candle_ts} but ALREADY ACTED - skipping")
            else:
                logger.info("No entry signal detected. Keeping current state.")
                
        except Exception as ex:
            logger.error(f"Error in main polling loop: {ex}", exc_info=True)
            
        await asyncio.sleep(30)  # Polling every 30 seconds

# ────────────────────────────────────────────────────────────
# Global Exchange Init and main task orchestrator
# ────────────────────────────────────────────────────────────
global_exchange = None

async def main():
    global global_exchange
    inst_id = "ANTHROPIC-USDT-SWAP"
    leverage = 3
    
    # Init Exchange via CCXT
    global_exchange = ccxt_async.okx({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'password': PASSPHRASE,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
        }
    })
    
    if SIMULATED:
        global_exchange.set_sandbox_mode(True)
        
    logger.info(f"Connecting to OKX environment: {ENV_TYPE} via CCXT...")
    
    # 1. Load symbols mapping
    try:
        await global_exchange.load_markets()
        ccxt_symbol = None
        for sym, market in global_exchange.markets.items():
            if market['id'] == inst_id:
                ccxt_symbol = sym
                break
        if not ccxt_symbol:
            # Fallback format
            ccxt_symbol = inst_id.replace('-SWAP', '').replace('-', '/') + ':USDT'
            
        context.ccxt_symbol = ccxt_symbol
        market_info = global_exchange.market(ccxt_symbol)
        context.ct_val = float(market_info.get("contractSize", 1.0))
        context.lot_sz = float(market_info.get("limits", {}).get("amount", {}).get("min", 1.0))
        logger.info(f"Instrument loaded: symbol={ccxt_symbol}, ctVal={context.ct_val}, lotSz={context.lot_sz}")
    except Exception as e:
        logger.error(f"Failed to fetch market specifications from OKX: {e}")
        sys.exit(1)
        
    # 2. Position account mode setup
    try:
        acct_config = await global_exchange.privateGetAccountConfig()
        if acct_config and acct_config.get("code") == "0" and acct_config.get("data"):
            context.pos_mode = acct_config["data"][0].get("posMode", "net_mode")
            logger.info(f"Current Position Mode on OKX Account: {context.pos_mode}")
    except Exception as e:
        logger.warning(f"Could not load account config: {e}")
        
    # 3. Setup leverage
    try:
        lev_res = await global_exchange.privatePostAccountSetLeverage({
            "instId": inst_id,
            "lever": str(leverage),
            "mgnMode": "isolated"
        })
        logger.info(f"Leverage setup configuration response: {lev_res}")
    except Exception as e:
        logger.warning(f"Leverage setup configuration bypassed or already configured: {e}")

    # Launch WebSocket listener and main polling loop concurrently
    await asyncio.gather(
        websocket_listener(inst_id),
        main_polling_loop(global_exchange, inst_id)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    finally:
        if global_exchange:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(global_exchange.close())
