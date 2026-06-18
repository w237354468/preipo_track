import os
import sys
import time
import hmac
import base64
import json
import logging
from datetime import datetime
import requests
import pandas as pd
import numpy as np

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("ma_bot.log", encoding="utf-8")
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "ma_bot_state.json")
TRADES_FILE = os.path.join(SCRIPT_DIR, "ma_bot_trades.json")

# ────────────────────────────────────────────────────────────
# State persistence: prevents duplicate orders on restart
# ────────────────────────────────────────────────────────────
def load_state():
    """Load bot state from disk. Returns dict with last_acted_ts, last_signal, etc."""
    default = {"last_acted_ts": 0, "last_signal": "none", "position_side": "flat", "peak_upl_ratio": 0.0}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return {**default, **data}
        except Exception as e:
            logger.warning(f"Failed to load state file: {e}")
    return default

def save_state(state: dict):
    """Persist bot state to disk."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save state file: {e}")

def record_trade(trade_info: dict):
    """Append a trade record to the trades JSON file."""
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
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save trades file: {e}")


# ────────────────────────────────────────────────────────────
# OKX API helpers
# ────────────────────────────────────────────────────────────
def get_okx_headers(method: str, request_path: str, body: str = "") -> dict:
    now = datetime.utcnow()
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
        headers["x-simulated-auth"] = "1"
        headers["OK-ACCESS-SIMULATED"] = "1"
    return headers

def private_request(method: str, path: str, params: dict = None, json_data: dict = None):
    url = BASE_URL + path
    body = ""
    if json_data:
        body = json.dumps(json_data)
        
    headers = get_okx_headers(method, path, body)
    
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

# Fetch candles
def fetch_candles(inst_id, bar='30m', limit=100):
    url = f"{BASE_URL}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("code") == "0" and res.get("data"):
            df = pd.DataFrame(res["data"], columns=[
                'ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
            ])
            for col in ['open', 'high', 'low', 'close']:
                df[col] = df[col].astype(float)
            df['ts'] = df['ts'].astype(int)
            df = df.iloc[::-1].reset_index(drop=True)
            return df
    except Exception as e:
        logger.error(f"Failed to fetch market candles: {e}")
    return None


def main_loop():
    inst_id = "ANTHROPIC-USDT-SWAP"
    max_capital = 10.0  # Total capital limit: 10 USDT (all-in each trade)
    leverage = 3        # 3x leverage
    
    # ── Risk Management Parameters ──
    STOP_LOSS_PCT = -5.0       # Hard stop-loss: close if margin PnL% <= -5%
    TAKE_PROFIT_PCT = 10.0     # Take-profit: close if margin PnL% >= +10%
    TRAILING_STOP_PCT = 3.0    # Trailing stop: close if profit drops 3% from peak
    
    logger.info(f"Starting MA Crossover Bot for {inst_id} on environment: {ENV_TYPE}")
    
    # Load persisted state
    state = load_state()
    logger.info(f"Loaded state: last_acted_ts={state['last_acted_ts']}, last_signal={state['last_signal']}, position_side={state['position_side']}")
    
    # 1. Check position mode
    config_res = private_request("GET", "/api/v5/account/config")
    pos_mode = "net_mode"
    if config_res and config_res.get("code") == "0" and config_res.get("data"):
        pos_mode = config_res["data"][0].get("posMode", "long_short")
        logger.info(f"Current Position Mode on OKX: {pos_mode}")
    else:
        logger.warning(f"Failed to fetch account config, defaulting to net mode: {config_res}")
        
    # 2. Set Leverage to 3x
    lev_res = private_request("POST", "/api/v5/account/set-leverage", json_data={
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": "cross"
    })
    logger.info(f"Leverage setup response: {lev_res}")
    
    while True:
        try:
            # A. Fetch candles
            df = fetch_candles(inst_id, bar='30m', limit=100)
            if df is None or len(df) < 50:
                logger.warning("Not enough candles fetched, waiting...")
                time.sleep(30)
                continue
                
            # Compute SMA 15 and SMA 20
            # Exclude the current active unconfirmed candle (last row) to avoid signal flickering
            df_completed = df.iloc[:-1].copy()
            df_completed['fast'] = df_completed['close'].rolling(window=15).mean()
            df_completed['slow'] = df_completed['close'].rolling(window=20).mean()
            
            # Crossover check on last completed candle
            current_row = df_completed.iloc[-1]
            prev_row = df_completed.iloc[-2]
            
            if pd.isna(current_row['slow']) or pd.isna(prev_row['slow']):
                logger.warning("Indicators not fully populated yet, waiting...")
                time.sleep(30)
                continue
                
            last_close = current_row['close']
            current_candle_ts = int(current_row['ts'])
            
            # Crossover signals
            golden_cross = (prev_row['fast'] <= prev_row['slow']) and (current_row['fast'] > current_row['slow'])
            death_cross = (prev_row['fast'] >= prev_row['slow']) and (current_row['fast'] < current_row['slow'])
            
            logger.info(f"Price: {last_close:.2f} | Fast MA: {current_row['fast']:.2f} | Slow MA: {current_row['slow']:.2f} | Golden={golden_cross} | Death={death_cross}")
            
            # B. Get current position from OKX
            pos_res = private_request("GET", "/api/v5/account/positions", params={"instId": inst_id})
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
                    if p.get("instId") == inst_id:
                        p_sz = float(p.get("pos", "0"))
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
                            pos_avg_px = float(p.get("avgPx", "0"))
                            pos_upl = float(p.get("upl", "0"))
                            pos_upl_ratio = float(p.get("uplRatio", "0")) * 100
                            pos_margin = float(p.get("margin", "0"))
                            pos_liq_px = float(p.get("liqPx", "0") or "0")
                            
            logger.info(f"Position: side={pos_side} | size={pos_sz} | avgPx={pos_avg_px:.2f} | upl={pos_upl:.4f} | uplRatio={pos_upl_ratio:.2f}%")
            
            # C. Get available balance
            avail_balance = 0.0
            bal_res = private_request("GET", "/api/v5/account/balance", params={"ccy": "USDT"})
            if bal_res and bal_res.get("code") == "0" and bal_res.get("data"):
                for detail in bal_res["data"][0].get("details", []):
                    if detail.get("ccy") == "USDT":
                        avail_balance = float(detail.get("availBal", "0"))
                        break
            
            # Cap at max_capital (10 USDT) total
            usable_margin = min(avail_balance, max_capital)
            target_value = usable_margin * leverage
            target_sz = round(target_value / last_close, 3)
            if target_sz < 0.001:
                target_sz = 0.001
                
            logger.info(f"Balance: available={avail_balance:.2f} USDT | usable(cap {max_capital})={usable_margin:.2f} | target_sz={target_sz}")
            
            # ── RISK MANAGEMENT: Stop-Loss / Take-Profit / Trailing Stop ──
            risk_exit = False
            if pos_side != "flat" and pos_sz > 0:
                # Update peak PnL tracking for trailing stop
                current_peak = state.get("peak_upl_ratio", 0.0)
                if pos_upl_ratio > current_peak:
                    state["peak_upl_ratio"] = pos_upl_ratio
                    save_state(state)
                    current_peak = pos_upl_ratio
                
                # Check stop-loss
                if pos_upl_ratio <= STOP_LOSS_PCT:
                    logger.info(f"🛑 STOP-LOSS triggered! PnL%={pos_upl_ratio:.2f}% <= {STOP_LOSS_PCT}%")
                    risk_exit = True
                    exit_reason = "STOP_LOSS"
                # Check take-profit
                elif pos_upl_ratio >= TAKE_PROFIT_PCT:
                    logger.info(f"🎯 TAKE-PROFIT triggered! PnL%={pos_upl_ratio:.2f}% >= {TAKE_PROFIT_PCT}%")
                    risk_exit = True
                    exit_reason = "TAKE_PROFIT"
                # Check trailing stop (only if we've been in profit)
                elif current_peak >= 2.0 and (current_peak - pos_upl_ratio) >= TRAILING_STOP_PCT:
                    logger.info(f"📉 TRAILING STOP triggered! Peak={current_peak:.2f}% -> Current={pos_upl_ratio:.2f}% (drawdown={current_peak - pos_upl_ratio:.2f}%)")
                    risk_exit = True
                    exit_reason = "TRAILING_STOP"
                else:
                    logger.info(f"Risk check OK: PnL%={pos_upl_ratio:.2f}% | Peak={current_peak:.2f}% | SL={STOP_LOSS_PCT}% | TP={TAKE_PROFIT_PCT}%")
                
                if risk_exit:
                    # Close current position
                    close_side = "sell" if pos_side == "long" else "buy"
                    close_pos_side_val = pos_side if pos_mode == "long_short" else "net"
                    order_data = {
                        "instId": inst_id, "tdMode": "cross",
                        "side": close_side, "posSide": close_pos_side_val,
                        "ordType": "market", "sz": str(pos_sz)
                    }
                    res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                    action_name = f"{exit_reason}_CLOSE_{'LONG' if pos_side == 'long' else 'SHORT'}"
                    logger.info(f"⚡ {exit_reason} close response: {res}")
                    record_trade({
                        "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        "action": action_name, "size": pos_sz,
                        "price": last_close, "pnl_pct": pos_upl_ratio,
                        "reason": exit_reason, "response": str(res)
                    })
                    # Reset state
                    state["position_side"] = "flat"
                    state["peak_upl_ratio"] = 0.0
                    state["last_signal"] = exit_reason.lower()
                    save_state(state)
                    logger.info(f"Position closed by {exit_reason}. Waiting for next crossover signal.")
                    time.sleep(60)  # Wait before looking for new signals
                    continue
            
            # ── DUPLICATE ORDER PREVENTION ──
            already_acted = (current_candle_ts == state.get("last_acted_ts", 0))
            
            # D. Trade execution logic (MA crossover signals)
            if golden_cross and not already_acted:
                logger.info("⚡ Golden Cross triggered!")
                if pos_side == "short":
                    logger.info(f"Closing existing Short position (size={pos_sz})...")
                    close_pos_side = "short" if pos_mode == "long_short" else "net"
                    order_data = {
                        "instId": inst_id, "tdMode": "cross",
                        "side": "buy", "posSide": close_pos_side,
                        "ordType": "market", "sz": str(pos_sz)
                    }
                    res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                    logger.info(f"Close Short response: {res}")
                    record_trade({
                        "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        "action": "CLOSE_SHORT", "size": pos_sz,
                        "price": last_close, "response": str(res)
                    })
                    time.sleep(2)
                    
                if pos_side != "long":
                    logger.info(f"Opening Long position with size {target_sz}...")
                    open_pos_side = "long" if pos_mode == "long_short" else "net"
                    order_data = {
                        "instId": inst_id, "tdMode": "cross",
                        "side": "buy", "posSide": open_pos_side,
                        "ordType": "market", "sz": str(target_sz)
                    }
                    res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                    logger.info(f"⚡ Open Long response: {res}")
                    record_trade({
                        "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        "action": "OPEN_LONG", "size": target_sz,
                        "price": last_close, "response": str(res)
                    })
                    
                # Update state to mark this candle as acted
                state["last_acted_ts"] = current_candle_ts
                state["last_signal"] = "golden_cross"
                state["position_side"] = "long"
                state["peak_upl_ratio"] = 0.0
                save_state(state)
                logger.info(f"State saved: acted on candle ts={current_candle_ts}")
            
            elif death_cross and not already_acted:
                logger.info("⚡ Death Cross triggered!")
                if pos_side == "long":
                    logger.info(f"Closing existing Long position (size={pos_sz})...")
                    close_pos_side = "long" if pos_mode == "long_short" else "net"
                    order_data = {
                        "instId": inst_id, "tdMode": "cross",
                        "side": "sell", "posSide": close_pos_side,
                        "ordType": "market", "sz": str(pos_sz)
                    }
                    res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                    logger.info(f"Close Long response: {res}")
                    record_trade({
                        "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        "action": "CLOSE_LONG", "size": pos_sz,
                        "price": last_close, "response": str(res)
                    })
                    time.sleep(2)
                    
                if pos_side != "short":
                    logger.info(f"Opening Short position with size {target_sz}...")
                    open_pos_side = "short" if pos_mode == "long_short" else "net"
                    order_data = {
                        "instId": inst_id, "tdMode": "cross",
                        "side": "sell", "posSide": open_pos_side,
                        "ordType": "market", "sz": str(target_sz)
                    }
                    res = private_request("POST", "/api/v5/trade/order", json_data=order_data)
                    logger.info(f"⚡ Open Short response: {res}")
                    record_trade({
                        "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        "action": "OPEN_SHORT", "size": target_sz,
                        "price": last_close, "response": str(res)
                    })
                    
                state["last_acted_ts"] = current_candle_ts
                state["last_signal"] = "death_cross"
                state["position_side"] = "short"
                state["peak_upl_ratio"] = 0.0
                save_state(state)
                logger.info(f"State saved: acted on candle ts={current_candle_ts}")
            
            elif (golden_cross or death_cross) and already_acted:
                logger.info(f"⚠️ Crossover signal on candle ts={current_candle_ts} but ALREADY ACTED - skipping (restart-safe)")
            else:
                logger.info("No crossover detected. Keeping current state.")
                
        except Exception as ex:
            logger.error(f"Error in main loop: {ex}", exc_info=True)
            
        # Wait for 1 minute before checking again
        time.sleep(60)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
