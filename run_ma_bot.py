import os
import sys
import time
import hmac
import base64
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np

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
        logger.info("Loading environment variables...")
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
BASE_URL = "https://www.okx.com"

STATE_FILE = os.path.join(SCRIPT_DIR, "ma_bot_state.json")
TRADES_FILE = os.path.join(SCRIPT_DIR, "ma_bot_trades.json")

# ────────────────────────────────────────────────────────────
# State persistence: prevents duplicate orders on restart
# ────────────────────────────────────────────────────────────
def load_state():
    """Load bot state from disk. Returns dict with last_acted_ts, last_signal, etc."""
    default = {"last_acted_ts": 0, "last_signal": "none", "position_side": "flat", "peak_upl_ratio": 0.0, "position_size": 0.0, "tp_order_id": ""}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return {**default, **data}
        except Exception as e:
            logger.warning(f"Failed to load state file: {e}")
    return default

def save_state(state: dict):
    """Persist bot state to disk atomically."""
    try:
        tmp_file = STATE_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, STATE_FILE)
    except Exception as e:
        logger.warning(f"Failed to save state file atomically: {e}")

def record_trade(trade_info: dict):
    """Append a trade record to the trades JSON file (atomic write)."""
    trades = []
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                trades = json.load(f)
        except Exception:
            trades = []
    trades.append(trade_info)
    # Keep last 200 trades
    trades = trades[-200:]
    try:
        tmp_file = TRADES_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(trades, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, TRADES_FILE)
    except Exception as e:
        logger.warning(f"Failed to save trades file: {e}")


# ────────────────────────────────────────────────────────────
# OKX API helpers
# ────────────────────────────────────────────────────────────
def get_okx_headers(method: str, request_path: str, body: str = "") -> dict:
    now = datetime.now(timezone.utc)
    timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    prehash = timestamp + method + request_path + body
    mac = hmac.new(bytes(SECRET_KEY, encoding='utf8'), bytes(prehash, encoding='utf8'), digestmod='sha256')
    signature = base64.b64encode(mac.digest()).decode('utf-8')
    
    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
    }
    if SIMULATED:
        headers["x-simulated-only"] = "1"
    return headers

def private_request(method: str, path: str, params: dict = None, json_data: dict = None):
    url = BASE_URL + path
    body = ""
    if json_data:
        body = json.dumps(json_data)
        
    request_path = path
    if method == "GET" and params:
        from urllib.parse import urlencode
        # Sort or encode the params to construct the exact query string OKX expects
        query_string = urlencode(params)
        request_path = f"{path}?{query_string}"
        
    headers = get_okx_headers(method, request_path, body)
    
    try:
        if method == "GET":
            res = requests.get(url, headers=headers, params=params, timeout=10)
        elif method == "POST":
            res = requests.post(url, headers=headers, data=body, timeout=10)
        else:
            return None
        return res.json()
    except Exception as e:
        logger.error(f"HTTP request failed: {e}")
        return None

def get_instrument_details(inst_id: str) -> dict:
    """Fetch ctVal and lotSz from OKX public instruments API."""
    url = f"{BASE_URL}/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("code") == "0" and res.get("data"):
            data = res["data"][0]
            return {
                "ctVal": float(data.get("ctVal", 1.0)),
                "lotSz": float(data.get("lotSz", 1.0))
            }
    except Exception as e:
        logger.error(f"Failed to fetch instrument details for {inst_id}: {e}")
    return {"ctVal": 1.0, "lotSz": 1.0}

def format_size(size: float, lot_size: float) -> str:
    """Format size to match the required lot size precision and remove trailing zeros if integer."""
    if lot_size >= 1.0:
        return str(int(round(size / lot_size) * lot_size))
    else:
        decimals = len(str(lot_size).split(".")[1]) if "." in str(lot_size) else 0
        val = round(size / lot_size) * lot_size
        return f"{val:.{decimals}f}"

# Fetch candles
def fetch_candles(inst_id, bar='30m', limit=100, max_retries=3):
    url = f"{BASE_URL}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    for attempt in range(1, max_retries + 1):
        try:
            res = requests.get(url, timeout=10).json()
            if res.get("code") == "0" and res.get("data"):
                data = res["data"]
                df = pd.DataFrame(data, columns=[
                    'ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
                ])
                for col in ['open', 'high', 'low', 'close', 'vol']:
                    df[col] = df[col].astype(float)
                df['ts'] = df['ts'].astype(int)
                # Reverse to chronological order (oldest first)
                df = df.iloc[::-1].reset_index(drop=True)
                return df
            else:
                logger.error(f"Attempt {attempt}/{max_retries} - Error fetching candles: {res}")
        except Exception as e:
            logger.error(f"Attempt {attempt}/{max_retries} - Request failed in fetch_candles: {e}")
        
        if attempt < max_retries:
            time.sleep(attempt * 2)
            
    return None

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

def main_loop():
    inst_id = "ANTHROPIC-USDT-SWAP"
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
    
    RSI_OB_EXIT = 75.0  # Overbought exit for long
    RSI_OS_EXIT = 25.0  # Oversold exit for short
    
    logger.info(f"Starting EMA+RSI Pullback Bot (30m) for {inst_id} on environment: {ENV_TYPE}")
    
    details = get_instrument_details(inst_id)
    ct_val = details["ctVal"]
    lot_sz = details["lotSz"]
    logger.info(f"Loaded details for {inst_id}: ctVal={ct_val}, lotSz={lot_sz}")
    
    state = load_state()
    # Initialize stop_loss/take_profit if not present
    if "stop_loss" not in state:
        state["stop_loss"] = 0.0
    if "take_profit" not in state:
        state["take_profit"] = 0.0
    
    # Check position mode
    config_res = private_request("GET", "/api/v5/account/config")
    pos_mode = "net_mode"
    if config_res and config_res.get("code") == "0" and config_res.get("data"):
        pos_mode = config_res["data"][0].get("posMode", "long_short")
        logger.info(f"Current Position Mode on OKX: {pos_mode}")
        
    # Set Leverage to 3x
    lev_res = private_request("POST", "/api/v5/account/set-leverage", json_data={
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": "isolated"
    })
    logger.info(f"Leverage setup response: {lev_res}")
    
    while True:
        try:
            # Check if paused by user
            state = load_state()
            if state.get("is_paused", False):
                logger.info("Bot is PAUSED by user. Sleeping for 10 seconds...")
                time.sleep(10)
                continue

            # A. Fetch candles (at least 600 candles to compute EMA 200 accurately and avoid drift)
            df = fetch_candles(inst_id, bar='30m', limit=600)
            if df is None or len(df) < 220:
                logger.warning("Not enough candles fetched, waiting...")
                time.sleep(30)
                continue
                
            # Compute indicators on all candles
            df['ema'] = calculate_ema(df['close'], period=EMA_PERIOD)
            df['rsi'] = calculate_rsi(df['close'], period=RSI_PERIOD)
            df['atr'] = calculate_atr(df, period=ATR_PERIOD)
            
            # Use last completed candle (index -2) for entry checks to avoid signal flickering
            completed_candle = df.iloc[-2]
            current_candle = df.iloc[-1]
            
            last_close = current_candle['close']
            current_candle_ts = int(completed_candle['ts'])
            
            # Entry Signal Indicators (from completed candle)
            comp_close = completed_candle['close']
            comp_ema = completed_candle['ema']
            comp_rsi = completed_candle['rsi']
            comp_atr = completed_candle['atr']
            
            # Current values (for exit/monitoring)
            curr_rsi = current_candle['rsi']
            curr_ema = current_candle['ema']
            
            # Check for entry signals
            long_entry_signal = (comp_close > comp_ema) and (RSI_LONG_LOWER <= comp_rsi <= RSI_LONG_UPPER)
            short_entry_signal = (comp_close < comp_ema) and (RSI_SHORT_LOWER <= comp_rsi <= RSI_SHORT_UPPER)
            
            logger.info(
                f"Price: {last_close:.2f} | EMA(200): {curr_ema:.2f} | RSI(14): {curr_rsi:.2f} | "
                f"Completed Candle (Close: {comp_close:.2f}, EMA: {comp_ema:.2f}, RSI: {comp_rsi:.2f}, ATR: {comp_atr:.4f}) | "
                f"Signals: Long={long_entry_signal}, Short={short_entry_signal}"
            )
            
            # B. Get current positions
            pos_res = private_request("GET", "/api/v5/account/positions")
            pos_side = "flat"
            pos_sz = 0.0
            pos_avg_px = 0.0
            pos_upl = 0.0
            pos_upl_ratio = 0.0
            pos_margin = 0.0
            pos_liq_px = 0.0
            
            if pos_res and pos_res.get("code") == "0":
                data = pos_res.get("data", [])
                for p in data:
                    if p.get("instId") == inst_id and p.get("mgnMode") == "isolated":
                        p_sz = float(p.get("pos", "0") or "0")
                        p_side = p.get("posSide")
                        
                        if p_side == "long" and p_sz > 0:
                            pos_side = "long"
                            pos_sz = p_sz
                        elif p_side == "short" and p_sz > 0:
                            pos_side = "short"
                            pos_sz = p_sz
                        elif p_side == "net":
                            if p_sz > 0:
                                pos_side = "long"
                                pos_sz = p_sz
                            elif p_sz < 0:
                                pos_side = "short"
                                pos_sz = abs(p_sz)

                        if pos_side != "flat":
                            pos_avg_px = float(p.get("avgPx", "0") or "0")
                            pos_upl = float(p.get("upl", "0") or "0")
                            pos_upl_ratio = float(p.get("uplRatio", "0") or "0") * 100
                            pos_margin = float(p.get("margin", "0") or "0")
                            pos_liq_px = float(p.get("liqPx", "0") or "0")
            else:
                logger.error(f"Failed to fetch positions from OKX: {pos_res}")
                
            # Sync: if exchange says flat but state thinks we have a position, reset state
            if pos_side == "flat" and state.get("position_side", "flat") != "flat":
                logger.warning(f"⚠️ 交易所无持仓，但状态文件记录 position_side={state['position_side']}，同步重置为 flat...")
                
                # Check if it was closed via the resting TP order
                tp_order_id = state.get("tp_order_id", "")
                was_tp_filled = False
                if tp_order_id:
                    logger.info(f"Checking status of resting TP order: {tp_order_id}")
                    order_status_res = private_request("GET", "/api/v5/trade/order", params={"instId": inst_id, "ordId": tp_order_id})
                    if order_status_res and order_status_res.get("code") == "0" and order_status_res.get("data"):
                        ord_state = order_status_res["data"][0].get("state")
                        if ord_state == "filled":
                            logger.info(f"🎉 Confirming resting Limit TP order was filled on exchange!")
                            was_tp_filled = True
                            
                if was_tp_filled:
                    record_trade({
                        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "action": f"CLOSE_{state['position_side'].upper()}",
                        "size": state.get("position_size", 0.0),
                        "price": state.get("take_profit", last_close),
                        "reason": "TAKE_PROFIT_LIMIT",
                        "response": "Limit order filled"
                    })
                    state["last_signal"] = "take_profit_limit"
                else:
                    # Cancel TP order just in case it remains open (e.g. manual close or liquidation)
                    if tp_order_id:
                        logger.info(f"Cancelling stale TP order on exchange: {tp_order_id}")
                        private_request("POST", "/api/v5/trade/cancel-order", json_data={"instId": inst_id, "ordId": tp_order_id})
                    state["last_signal"] = "sync_reset"
                    
                state["position_side"] = "flat"
                state["stop_loss"] = 0.0
                state["take_profit"] = 0.0
                state["position_size"] = 0.0
                state["tp_order_id"] = ""
                save_state(state)
            
            logger.info(
                f"Position: side={pos_side} | size={pos_sz} | avgPx={pos_avg_px:.2f} | "
                f"upl={pos_upl:.4f} | uplRatio={pos_upl_ratio:.2f}% | SL={state.get('stop_loss', 0.0):.2f} | TP={state.get('take_profit', 0.0):.2f}"
            )
            
            # C. Get available balance
            avail_balance = 0.0
            bal_res = private_request("GET", "/api/v5/account/balance")
            if bal_res and bal_res.get("code") == "0" and bal_res.get("data"):
                for detail in bal_res["data"][0].get("details", []):
                    if detail.get("ccy") == "USDT":
                        avail_balance = float(detail.get("availBal", "0"))
                        break
            else:
                logger.error(f"Failed to fetch balance from OKX: {bal_res}")
                
            usable_margin = min(avail_balance, max_capital)
            target_value = usable_margin * leverage
            # Calculate target size in contracts (sz) using ct_val and align with lot_sz
            raw_sz = target_value / (last_close * ct_val)
            target_sz = max(raw_sz, lot_sz)
            target_sz = round(target_sz / lot_sz) * lot_sz
                
            # D. Exit / Risk Check
            exit_triggered = False
            exit_reason = ""
            
            if pos_side != "flat" and pos_sz > 0:
                if pos_side == "long":
                    sl_val = state.get("stop_loss", 0.0)
                    tp_val = state.get("take_profit", 0.0)
                    if sl_val <= 0.0:
                        if pos_avg_px > 0.0:
                            logger.warning("⚠️ Long position is active on OKX but stop_loss state is 0.0 or missing! Re-calculating SL/TP based on entry price and current ATR...")
                            sl = pos_avg_px - SL_ATR_MULT * comp_atr
                            tp = pos_avg_px + TP_ATR_MULT * comp_atr
                            state["stop_loss"] = round(sl, 2)
                            state["take_profit"] = round(tp, 2)
                            state["position_side"] = "long"
                            state["position_size"] = pos_sz
                            save_state(state)
                            sl_val = state["stop_loss"]
                            tp_val = state["take_profit"]
                        else:
                            logger.error("❌ Long position active but avgPx is 0 — cannot re-calculate SL/TP, skipping exit checks")
                        
                    if sl_val > 0.0 and last_close <= sl_val:
                        exit_triggered = True
                        exit_reason = "STOP_LOSS_ATR"
                    elif last_close < curr_ema:
                        exit_triggered = True
                        exit_reason = "EMA_CROSS_EXIT"
                    elif curr_rsi >= RSI_OB_EXIT:
                        exit_triggered = True
                        exit_reason = "RSI_OVERBOUGHT_EXIT"
                elif pos_side == "short":
                    sl_val = state.get("stop_loss", 0.0)
                    tp_val = state.get("take_profit", 0.0)
                    if sl_val <= 0.0:
                        if pos_avg_px > 0.0:
                            logger.warning("⚠️ Short position is active on OKX but stop_loss state is 0.0 or missing! Re-calculating SL/TP based on entry price and current ATR...")
                            sl = pos_avg_px + SL_ATR_MULT * comp_atr
                            tp = pos_avg_px - TP_ATR_MULT * comp_atr
                            state["stop_loss"] = round(sl, 2)
                            state["take_profit"] = round(tp, 2)
                            state["position_side"] = "short"
                            state["position_size"] = pos_sz
                            save_state(state)
                            sl_val = state["stop_loss"]
                            tp_val = state["take_profit"]
                        else:
                            logger.error("❌ Short position active but avgPx is 0 — cannot re-calculate SL/TP, skipping exit checks")
                        
                    if sl_val > 0.0 and last_close >= sl_val:
                        exit_triggered = True
                        exit_reason = "STOP_LOSS_ATR"
                    elif last_close > curr_ema:
                        exit_triggered = True
                        exit_reason = "EMA_CROSS_EXIT"
                    elif curr_rsi <= RSI_OS_EXIT:
                        exit_triggered = True
                        exit_reason = "RSI_OVERSOLD_EXIT"
                        
                if exit_triggered:
                    logger.info(f"⚡ EXIT TRIGGERED: {exit_reason} (Close: {last_close:.2f})")
                    close_side = "sell" if pos_side == "long" else "buy"
                    close_pos_side = pos_side if pos_mode == "long_short" else "net"
                    
                    # Cancel resting TP order if it exists
                    tp_order_id = state.get("tp_order_id")
                    if tp_order_id:
                        logger.info(f"Cancelling resting TP order: {tp_order_id}")
                        cancel_success = False
                        for attempt in range(3):
                            cancel_res = private_request("POST", "/api/v5/trade/cancel-order", json_data={
                                "instId": inst_id, "ordId": tp_order_id
                            })
                            # code "0" indicates success, or "51401" means order does not exist / already canceled
                            if cancel_res and (cancel_res.get("code") == "0" or cancel_res.get("code") == "51401"):
                                logger.info(f"TP order cancelled successfully or already inactive (Response: {cancel_res})")
                                cancel_success = True
                                break
                            else:
                                logger.warning(f"Attempt {attempt+1}/3 to cancel TP order failed: {cancel_res}. Retrying...")
                                time.sleep(0.5)
                        
                        state["tp_order_id"] = ""
                        save_state(state)
                        
                    order_data = {
                        "instId": inst_id, "tdMode": "isolated",
                        "side": close_side, "posSide": close_pos_side,
                        "ordType": "market", "sz": format_size(pos_sz, lot_sz),
                        "reduceOnly": True
                    }
                    
                    # Try to execute close order with retries
                    order_executed = False
                    for attempt in range(5):
                        res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                        logger.info(f"Close attempt {attempt+1} response: {res}")
                        if res and res.get("code") == "0":
                            logger.info(f"🎉 Close position order filled successfully on attempt {attempt+1}")
                            order_executed = True
                            break
                        else:
                            logger.error(f"❌ Attempt {attempt+1}/5 - Close order failed: {res}. Retrying in 1s...")
                            time.sleep(1)
                    
                    if order_executed:
                        record_trade({
                            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "action": f"CLOSE_{pos_side.upper()}",
                            "size": pos_sz, "price": last_close, "reason": exit_reason, "response": str(res)
                        })
                        state["position_side"] = "flat"
                        state["stop_loss"] = 0.0
                        state["take_profit"] = 0.0
                        state["position_size"] = 0.0
                        state["last_signal"] = exit_reason.lower()
                        save_state(state)
                        pos_side = "flat"
                        pos_sz = 0.0
                    else:
                        logger.critical(f"🚨 CRITICAL: Failed to execute close order on OKX after 5 attempts! Reason: {exit_reason}")
            
            # Ensure resting Limit TP order is active on the exchange
            if pos_side != "flat" and pos_sz > 0 and not exit_triggered:
                try:
                    pending_res = private_request("GET", "/api/v5/trade/orders-pending", params={"instId": inst_id})
                    tp_found = False
                    tp_order_id = state.get("tp_order_id", "")
                    
                    if pending_res and pending_res.get("code") == "0":
                        pending_orders = pending_res.get("data", [])
                        for ord in pending_orders:
                            if ord.get("ordId") == tp_order_id:
                                tp_found = True
                                break
                        
                        # If not found by ID, check if there's any limit order that looks like our TP order
                        if not tp_found:
                            target_side = "sell" if pos_side == "long" else "buy"
                            target_pos_side = pos_side if pos_mode == "long_short" else "net"
                            for ord in pending_orders:
                                if (ord.get("ordType") == "limit" and 
                                    ord.get("side") == target_side and 
                                    ord.get("posSide") == target_pos_side):
                                    logger.info(f"Adopting existing pending limit order on exchange as TP: {ord.get('ordId')}")
                                    state["tp_order_id"] = ord.get("ordId")
                                    save_state(state)
                                    tp_found = True
                                    break
                    else:
                        logger.error(f"Failed to fetch pending orders: {pending_res}")
                        tp_found = True  # Avoid placing duplicate orders if API request failed
                    
                    if not tp_found:
                        tp_val = state.get("take_profit", 0.0)
                        if tp_val <= 0.0:
                            tp = pos_avg_px + (TP_ATR_MULT * comp_atr if pos_side == "long" else -TP_ATR_MULT * comp_atr)
                            state["take_profit"] = round(tp, 2)
                            save_state(state)
                            tp_val = state["take_profit"]
                            
                        tp_side = "sell" if pos_side == "long" else "buy"
                        tp_pos_side = pos_side if pos_mode == "long_short" else "net"
                        tp_order_data = {
                            "instId": inst_id, "tdMode": "isolated",
                            "side": tp_side, "posSide": tp_pos_side,
                            "ordType": "limit", "px": str(round(tp_val, 2)),
                            "sz": format_size(pos_sz, lot_sz),
                            "reduceOnly": True
                        }
                        logger.info(f"Resting Limit TP order is missing or cancelled. Placing new one at price {round(tp_val, 2)}...")
                        tp_res = private_request("POST", "/api/v5/trade/order", json_data=tp_order_data)
                        logger.info(f"Place Limit TP response: {tp_res}")
                        if tp_res and tp_res.get("code") == "0":
                            state["tp_order_id"] = tp_res["data"][0]["ordId"]
                            save_state(state)
                        else:
                            logger.error(f"❌ Failed to re-place resting Limit TP order: {tp_res}")
                except Exception as tp_ex:
                    logger.error(f"Error ensuring TP order is active: {tp_ex}")
            
            # E. Entry execution
            already_acted = (current_candle_ts == state.get("last_acted_ts", 0))
            
            if long_entry_signal and not already_acted:
                logger.info("⚡ Long Entry Signal triggered!")
                opposite_closed = True
                if pos_side == "short":
                    logger.info(f"Closing short (size={pos_sz})...")
                    close_pos_side = "short" if pos_mode == "long_short" else "net"
                    order_data = {
                        "instId": inst_id, "tdMode": "isolated",
                        "side": "buy", "posSide": close_pos_side,
                        "ordType": "market", "sz": format_size(pos_sz, lot_sz),
                        "reduceOnly": True
                    }
                    res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                    logger.info(f"Close Short response: {res}")
                    
                    if res and res.get("code") == "0":
                        record_trade({
                            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "action": "CLOSE_SHORT", "size": pos_sz, "price": last_close, "response": str(res)
                        })
                        pos_side = "flat"
                        pos_sz = 0.0
                        time.sleep(2)
                    else:
                        logger.error(f"❌ Failed to close opposite Short position: {res}")
                        opposite_closed = False
                    
                if opposite_closed and pos_side != "long":
                    logger.info(f"Opening Long (size={target_sz})...")
                    open_pos_side = "long" if pos_mode == "long_short" else "net"
                    order_data = {
                        "instId": inst_id, "tdMode": "isolated",
                        "side": "buy", "posSide": open_pos_side,
                        "ordType": "market", "sz": format_size(target_sz, lot_sz)
                    }
                    res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                    logger.info(f"Open Long response: {res}")
                    
                    if res and res.get("code") == "0":
                        sl = last_close - SL_ATR_MULT * comp_atr
                        tp = last_close + TP_ATR_MULT * comp_atr
                        
                        state["last_acted_ts"] = current_candle_ts
                        state["last_signal"] = "long_entry"
                        state["position_side"] = "long"
                        state["position_size"] = target_sz
                        state["stop_loss"] = round(sl, 2)
                        state["take_profit"] = round(tp, 2)
                        state["tp_order_id"] = ""
                        save_state(state)
                        
                        record_trade({
                            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "action": "OPEN_LONG", "size": target_sz, "price": last_close,
                            "stop_loss": state["stop_loss"], "take_profit": state["take_profit"], "response": str(res)
                        })
                        
                        # Place resting limit TP order on OKX
                        tp_side = "sell"
                        tp_pos_side = "long" if pos_mode == "long_short" else "net"
                        tp_order_data = {
                            "instId": inst_id, "tdMode": "isolated",
                            "side": tp_side, "posSide": tp_pos_side,
                            "ordType": "limit", "px": str(round(tp, 2)),
                            "sz": format_size(target_sz, lot_sz),
                            "reduceOnly": True
                        }
                        logger.info(f"Placing resting Limit TP order at price {round(tp, 2)}...")
                        tp_res = private_request("POST", "/api/v5/trade/order", json_data=tp_order_data)
                        logger.info(f"Place Limit TP response: {tp_res}")
                        if tp_res and tp_res.get("code") == "0":
                            state["tp_order_id"] = tp_res["data"][0]["ordId"]
                            save_state(state)
                        else:
                            logger.error(f"❌ Failed to place resting Limit TP order: {tp_res}")
                    else:
                        logger.error(f"❌ Failed to open Long position: {res}")
                    
            elif short_entry_signal and not already_acted:
                logger.info("⚡ Short Entry Signal triggered!")
                opposite_closed = True
                if pos_side == "long":
                    logger.info(f"Closing long (size={pos_sz})...")
                    close_pos_side = "long" if pos_mode == "long_short" else "net"
                    order_data = {
                        "instId": inst_id, "tdMode": "isolated",
                        "side": "sell", "posSide": close_pos_side,
                        "ordType": "market", "sz": format_size(pos_sz, lot_sz),
                        "reduceOnly": True
                    }
                    res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                    logger.info(f"Close Long response: {res}")
                    
                    if res and res.get("code") == "0":
                        record_trade({
                            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "action": "CLOSE_LONG", "size": pos_sz, "price": last_close, "response": str(res)
                        })
                        pos_side = "flat"
                        pos_sz = 0.0
                        time.sleep(2)
                    else:
                        logger.error(f"❌ Failed to close opposite Long position: {res}")
                        opposite_closed = False
                    
                if opposite_closed and pos_side != "short":
                    logger.info(f"Opening Short (size={target_sz})...")
                    open_pos_side = "short" if pos_mode == "long_short" else "net"
                    order_data = {
                        "instId": inst_id, "tdMode": "isolated",
                        "side": "sell", "posSide": open_pos_side,
                        "ordType": "market", "sz": format_size(target_sz, lot_sz)
                    }
                    res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                    logger.info(f"Open Short response: {res}")
                    
                    if res and res.get("code") == "0":
                        sl = last_close + SL_ATR_MULT * comp_atr
                        tp = last_close - TP_ATR_MULT * comp_atr
                        
                        state["last_acted_ts"] = current_candle_ts
                        state["last_signal"] = "short_entry"
                        state["position_side"] = "short"
                        state["position_size"] = target_sz
                        state["stop_loss"] = round(sl, 2)
                        state["take_profit"] = round(tp, 2)
                        state["tp_order_id"] = ""
                        save_state(state)
                        
                        record_trade({
                            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "action": "OPEN_SHORT", "size": target_sz, "price": last_close,
                            "stop_loss": state["stop_loss"], "take_profit": state["take_profit"], "response": str(res)
                        })
                        
                        # Place resting limit TP order on OKX
                        tp_side = "buy"
                        tp_pos_side = "short" if pos_mode == "long_short" else "net"
                        tp_order_data = {
                            "instId": inst_id, "tdMode": "isolated",
                            "side": tp_side, "posSide": tp_pos_side,
                            "ordType": "limit", "px": str(round(tp, 2)),
                            "sz": format_size(target_sz, lot_sz),
                            "reduceOnly": True
                        }
                        logger.info(f"Placing resting Limit TP order at price {round(tp, 2)}...")
                        tp_res = private_request("POST", "/api/v5/trade/order", json_data=tp_order_data)
                        logger.info(f"Place Limit TP response: {tp_res}")
                        if tp_res and tp_res.get("code") == "0":
                            state["tp_order_id"] = tp_res["data"][0]["ordId"]
                            save_state(state)
                        else:
                            logger.error(f"❌ Failed to place resting Limit TP order: {tp_res}")
                    else:
                        logger.error(f"❌ Failed to open Short position: {res}")
            
            elif (long_entry_signal or short_entry_signal) and already_acted:
                logger.info(f"⚠️ Entry signal on candle ts={current_candle_ts} but ALREADY ACTED - skipping")
            else:
                logger.info("No signal detected. Keeping current state.")
                
        except Exception as ex:
            logger.error(f"Error in main loop: {ex}", exc_info=True)
            
        time.sleep(60)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
