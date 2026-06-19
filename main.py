import hmac
import base64
import os
import sys
import logging
import json
import time
import hashlib
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse, FileResponse
from pydantic import BaseModel
import requests
import websockets as okx_ws

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("okx_portfolio")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Manual .env parser
def load_dotenv():
    dotenv_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(dotenv_path):
        logger.info("Loading environment variables from local .env file...")
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    val = val.strip().strip("'").strip('"')
                    os.environ[key.strip()] = val.strip()

load_dotenv()

app = FastAPI(title="OKX Real-Time Portfolio & Positions Tracker")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_okx_headers(api_key: str, secret_key: str, passphrase: str, method: str, request_path: str, body: str = "", simulated: bool = False) -> dict:
    now = datetime.utcnow()
    timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    
    prehash = timestamp + method + request_path + body
    
    mac = hmac.new(bytes(secret_key, encoding='utf8'), bytes(prehash, encoding='utf8'), digestmod='sha256')
    signature = base64.b64encode(mac.digest()).decode('utf-8')
    
    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
    }
    
    if simulated:
        headers["x-simulated-only"] = "1"
        
    return headers

@app.get("/api/okx/config")
async def get_okx_config():
    load_dotenv()
    api_key = os.getenv("OKX_API_KEY")
    secret_key = os.getenv("OKX_SECRET_KEY")
    passphrase = os.getenv("OKX_PASSPHRASE")
    env_type = os.getenv("OKX_ENVIRONMENT", "demo")
    
    has_keys = bool(api_key and secret_key and passphrase)
    return {
        "has_keys": has_keys,
        "environment": env_type
    }

instrument_cache = {}

async def get_instrument_detail(inst_id: str):
    if inst_id in instrument_cache:
        return instrument_cache[inst_id]
        
    # Determine inst_type from inst_id
    if "-SWAP" in inst_id:
        inst_type = "SWAP"
    elif len(inst_id.split("-")) >= 3:
        inst_type = "FUTURES"
    else:
        inst_type = "MARGIN"
        
    # Query OKX REST
    try:
        loop = asyncio.get_event_loop()
        base_url = "https://openapi.okx.com"
        path = f"/api/v5/public/instruments?instType={inst_type}&instId={inst_id}"
        
        def fetch():
            return requests.get(base_url + path, timeout=5).json()
            
        res = await loop.run_in_executor(None, fetch)
        if res.get("code") == "0" and res.get("data"):
            data = res["data"][0]
            instrument_cache[inst_id] = {
                "ctVal": float(data.get("ctVal", 1.0)),
                "ctType": data.get("ctType", "linear"),  # linear or inverse
                "settleCcy": data.get("settleCcy", "USDT")
            }
            logger.info(f"Cached instrument info for {inst_id}: {instrument_cache[inst_id]}")
            return instrument_cache[inst_id]
    except Exception as e:
        logger.warning(f"Failed to fetch instrument details for {inst_id}: {e}")
        
    # Fallback defaults
    fallback = {"ctVal": 1.0, "ctType": "linear", "settleCcy": "USDT"}
    if "BTC" in inst_id:
        if "-USD-" in inst_id:
            fallback = {"ctVal": 100.0, "ctType": "inverse", "settleCcy": "BTC"}
        else:
            fallback = {"ctVal": 0.01, "ctType": "linear", "settleCcy": "USDT"}
    elif "ETH" in inst_id:
        if "-USD-" in inst_id:
            fallback = {"ctVal": 10.0, "ctType": "inverse", "settleCcy": "ETH"}
        else:
            fallback = {"ctVal": 0.1, "ctType": "linear", "settleCcy": "USDT"}
    return fallback

async def fetch_grid_strategies(api_key: str, secret_key: str, passphrase: str, simulated: bool) -> List[dict]:
    grid_orders = []
    base_url = "https://openapi.okx.com"
    loop = asyncio.get_event_loop()
    
    for algo_type in ["grid", "contract_grid"]:
        path = f"/api/v5/tradingBot/grid/orders-algo-pending?algoOrdType={algo_type}"
        try:
            headers = get_okx_headers(api_key, secret_key, passphrase, "GET", path, "", simulated)
            
            def fetch():
                return requests.get(base_url + path, headers=headers, timeout=5).json()
                
            res = await loop.run_in_executor(None, fetch)
            if res.get("code") == "0" and res.get("data"):
                grid_orders.extend(res["data"])
        except Exception as e:
            logger.warning(f"Failed to fetch grid strategies for {algo_type}: {e}")
            
    return grid_orders

SECRET_KEY = os.urandom(32)

def create_session_token(username: str) -> str:
    payload = {
        "username": username,
        "expires": time.time() + 86400 * 7  # 7 days
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    signature = hmac.new(SECRET_KEY, payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"

def verify_session_token(token: str) -> bool:
    try:
        payload_b64, signature = token.split(".")
        expected_sig = hmac.new(SECRET_KEY, payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return False
        payload_bytes = base64.urlsafe_b64decode(payload_b64.encode())
        payload = json.loads(payload_bytes.decode())
        if time.time() > payload["expires"]:
            return False
        return True
    except Exception:
        return False

def check_login_credentials(username, password):
    load_dotenv()
    correct_user = os.getenv("APP_USERNAME", "admin")
    correct_pass = os.getenv("APP_PASSWORD", "admin")
    return username == correct_user and password == correct_pass

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/login")
async def api_login(req: LoginRequest):
    if check_login_credentials(req.username, req.password):
        token = create_session_token(req.username)
        response = JSONResponse({"success": True})
        response.set_cookie(
            key="session_token",
            value=token,
            max_age=86400 * 7,
            httponly=True,
            samesite="lax"
        )
        return response
    else:
        return JSONResponse({"success": False, "message": "用户名或密码错误"}, status_code=400)

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

@app.post("/api/change-password")
async def api_change_password(req: ChangePasswordRequest, request: Request):
    token = request.cookies.get("session_token")
    if not token or not verify_session_token(token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期，请重新登录")
    
    # Verify old password
    load_dotenv()
    correct_pass = os.getenv("APP_PASSWORD", "admin")
    if req.old_password != correct_pass:
        return JSONResponse({"success": False, "message": "当前密码不正确"}, status_code=400)
    
    # Save new password to .env
    dotenv_path = os.path.join(BASE_DIR, ".env")
    try:
        lines = []
        replaced = False
        if os.path.exists(dotenv_path):
            with open(dotenv_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("APP_PASSWORD="):
                        lines.append(f"APP_PASSWORD={req.new_password}\n")
                        replaced = True
                    else:
                        lines.append(line)
        if not replaced:
            lines.append(f"\nAPP_PASSWORD={req.new_password}\n")
            
        with open(dotenv_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
            
        # Also reload OS env
        os.environ["APP_PASSWORD"] = req.new_password
        logger.info("Password changed successfully in .env and memory.")
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to update .env: {e}")
        return JSONResponse({"success": False, "message": f"保存新密码失败: {str(e)}"}, status_code=500)

@app.post("/api/logout")
async def api_logout():
    response = JSONResponse({"success": True})
    response.delete_cookie("session_token")
    return response

@app.get("/login", response_class=HTMLResponse)
async def get_login(request: Request):
    token = request.cookies.get("session_token")
    prefix = request.headers.get("x-forwarded-prefix", "")
    if token and verify_session_token(token):
        return RedirectResponse(url=f"{prefix}/")
    return FileResponse(os.path.join(BASE_DIR, "static", "login.html"))

@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    token = request.cookies.get("session_token")
    prefix = request.headers.get("x-forwarded-prefix", "")
    if not token or not verify_session_token(token):
        return RedirectResponse(url=f"{prefix}/login")
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))

@app.get("/app.js")
async def get_app_js():
    return FileResponse(os.path.join(BASE_DIR, "static", "app.js"))

@app.get("/styles.css")
async def get_styles_css():
    return FileResponse(os.path.join(BASE_DIR, "static", "styles.css"))

@app.get("/vue.global.js")
async def get_vue_global_js():
    return FileResponse(os.path.join(BASE_DIR, "static", "vue.global.js"))

@app.get("/naive-ui.js")
async def get_naive_ui_js():
    return FileResponse(os.path.join(BASE_DIR, "static", "naive-ui.js"))

@app.get("/chart.js")
async def get_chart_js():
    return FileResponse(os.path.join(BASE_DIR, "static", "chart.js"))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Verify session cookie
    token = websocket.cookies.get("session_token")
    if not token or not verify_session_token(token):
        await websocket.accept()
        await websocket.send_json({"success": False, "message": "会话已过期，请刷新页面重新登录！"})
        await websocket.close()
        return

    await websocket.accept()
    logger.info("Browser client connected to local WebSocket.")
    
    # Receive credentials config from client
    try:
        config_str = await websocket.receive_text()
        config = json.loads(config_str)
    except Exception as e:
        logger.error(f"Failed to receive config from client: {e}")
        await websocket.close()
        return
        
    use_env = config.get("use_env", False)
    if use_env:
        load_dotenv()
        api_key = os.getenv("OKX_API_KEY")
        secret_key = os.getenv("OKX_SECRET_KEY")
        passphrase = os.getenv("OKX_PASSPHRASE")
        env_type = os.getenv("OKX_ENVIRONMENT", "demo")
        simulated = (env_type.lower() != "live")
    else:
        api_key = config.get("api_key")
        secret_key = config.get("secret_key")
        passphrase = config.get("passphrase")
        simulated = config.get("simulated", False)
        
    if not (api_key and secret_key and passphrase):
        await websocket.send_json({"success": False, "message": "未配置完整的 OKX API 密钥！"})
        await websocket.close()
        return

    # In-memory assets cache
    trading_assets = {}
    funding_assets = {}
    positions = {}
    grid_strategies = []
    last_prices = {}
    total_equity_usd = 0.0
    price_history = {} # inst_id -> list of (timestamp, price)
    
    async def seed_price_history(inst_id: str):
        if inst_id in price_history and len(price_history[inst_id]) > 0:
            return
        price_history[inst_id] = []
        try:
            loop = asyncio.get_event_loop()
            base_url = "https://openapi.okx.com"
            path = f"/api/v5/market/candles?instId={inst_id}&bar=1m&limit=10"
            
            def fetch():
                headers = {"User-Agent": "Mozilla/5.0"}
                return requests.get(base_url + path, headers=headers, timeout=5).json()
                
            res = await loop.run_in_executor(None, fetch)
            if res.get("code") == "0" and res.get("data"):
                # OKX returns newest first. Reverse to oldest first.
                candles = reversed(res["data"])
                history = []
                for c in candles:
                    ts = float(c[0]) / 1000.0
                    close_px = float(c[4])
                    history.append((ts, close_px))
                price_history[inst_id] = history
                logger.info(f"Seeded {len(history)} price history points for {inst_id}")
        except Exception as e:
            logger.warning(f"Failed to seed price history for {inst_id}: {e}")

    
    # One-off fetch funding assets via REST
    try:
        base_url = "https://openapi.okx.com"
        funding_path = "/api/v5/asset/balances"
        funding_headers = get_okx_headers(api_key, secret_key, passphrase, "GET", funding_path, "", simulated)
        funding_res = requests.get(base_url + funding_path, headers=funding_headers, timeout=5)
        funding_data = funding_res.json()
        if funding_data.get("code") == "0":
            for f in funding_data.get("data", []):
                ccy = f.get("ccy")
                bal = float(f.get("bal", 0))
                avail = float(f.get("availBal", 0))
                frozen = float(f.get("frozenBal", 0))
                if bal > 0 or avail > 0 or frozen > 0:
                    funding_assets[ccy] = {
                        "ccy": ccy,
                        "funding_bal": bal,
                        "funding_avail": avail,
                        "funding_frozen": frozen
                    }
    except Exception as e:
        logger.warning(f"Failed to fetch initial funding assets via REST: {e}")

    # Connect to OKX WebSockets (Private and Public)
    ws_domain = "wspap.okx.com:8443" if simulated else "ws.okx.com:8443"
    okx_ws_url = f"wss://{ws_domain}/ws/v5/private"
    pub_ws_url = f"wss://{ws_domain}/ws/v5/public"
    
    try:
        async with okx_ws.connect(okx_ws_url) as ows, okx_ws.connect(pub_ws_url) as pub_ws:
            # Login to OKX private WebSocket
            timestamp = str(int(time.time()))
            sign_text = timestamp + "GET" + "/users/self/verify"
            mac = hmac.new(bytes(secret_key, encoding='utf-8'), bytes(sign_text, encoding='utf-8'), digestmod=hashlib.sha256)
            signature = base64.b64encode(mac.digest()).decode('utf-8')
            
            login_msg = {
                "op": "login",
                "args": [{
                    "apiKey": api_key,
                    "passphrase": passphrase,
                    "timestamp": timestamp,
                    "sign": signature
                }]
            }
            await ows.send(json.dumps(login_msg))
            
            # Wait for login confirmation
            login_resp_str = await ows.recv()
            login_resp = json.loads(login_resp_str)
            if login_resp.get("event") == "error":
                await websocket.send_json({"success": False, "message": f"OKX WebSocket 登录失败 (错误码: {login_resp.get('code')}): {login_resp.get('msg')}"})
                await websocket.close()
                return
                
            logger.info("OKX WebSocket login successful!")
            
            # Subscribe to positions and account channels
            sub_msg = {
                "op": "subscribe",
                "args": [
                    {"channel": "positions", "instType": "ANY"},
                    {"channel": "account"}
                ]
            }
            await ows.send(json.dumps(sub_msg))
            
            # Track subscribed tickers
            subscribed_tickers = set()
            
            async def update_ticker_subscriptions():
                nonlocal subscribed_tickers
                active_inst_ids = {p["instId"] for p in positions.values()} | {g["instId"] for g in grid_strategies}
                
                # Unsubscribe from no longer active tickers
                to_unsub = subscribed_tickers - active_inst_ids
                if to_unsub:
                    unsub_args = [{"channel": "tickers", "instId": inst_id} for inst_id in to_unsub]
                    unsub_msg = {"op": "unsubscribe", "args": unsub_args}
                    try:
                        await pub_ws.send(json.dumps(unsub_msg))
                        subscribed_tickers -= to_unsub
                        logger.info(f"Unsubscribed from tickers: {to_unsub}")
                    except Exception as e:
                        logger.error(f"Failed to unsubscribe: {e}")
                        
                # Subscribe to newly active tickers
                to_sub = active_inst_ids - subscribed_tickers
                if to_sub:
                    sub_args = [{"channel": "tickers", "instId": inst_id} for inst_id in to_sub]
                    sub_msg = {"op": "subscribe", "args": sub_args}
                    try:
                        await pub_ws.send(json.dumps(sub_msg))
                        subscribed_tickers.update(to_sub)
                        logger.info(f"Subscribed to tickers: {to_sub}")
                        for inst_id in to_sub:
                            asyncio.create_task(seed_price_history(inst_id))
                    except Exception as e:
                        logger.error(f"Failed to subscribe: {e}")


            # Package state and push to frontend browser
            async def send_state():
                nonlocal total_equity_usd
                merged_assets = {}
                
                # Merge trading assets
                for ccy, t in trading_assets.items():
                    merged_assets[ccy] = {
                        "ccy": ccy,
                        "trading_eq": t["trading_eq"],
                        "trading_avail": t["trading_avail"],
                        "trading_frozen": t["trading_frozen"],
                        "funding_bal": 0.0,
                        "funding_avail": 0.0,
                        "funding_frozen": 0.0,
                        "total_eq": t["trading_eq"],
                        "valuation_usd": t["valuation_usd"]
                    }
                    
                # Merge funding assets
                for ccy, f in funding_assets.items():
                    if ccy in merged_assets:
                        merged_assets[ccy]["funding_bal"] = f["funding_bal"]
                        merged_assets[ccy]["funding_avail"] = f["funding_avail"]
                        merged_assets[ccy]["funding_frozen"] = f["funding_frozen"]
                        merged_assets[ccy]["total_eq"] += f["funding_bal"]
                    else:
                        merged_assets[ccy] = {
                            "ccy": ccy,
                            "trading_eq": 0.0,
                            "trading_avail": 0.0,
                            "trading_frozen": 0.0,
                            "funding_bal": f["funding_bal"],
                            "funding_avail": f["funding_avail"],
                            "funding_frozen": f["funding_frozen"],
                            "total_eq": f["funding_bal"],
                            "valuation_usd": 0.0
                        }
                        
                sorted_assets = sorted(list(merged_assets.values()), key=lambda x: x["total_eq"], reverse=True)
                
                # Inject latest prices into grid strategies
                for g in grid_strategies:
                    inst_id = g.get("instId")
                    if inst_id in last_prices:
                        g["lastPrice"] = last_prices[inst_id]
                    else:
                        g["lastPrice"] = None
                
                # Format price history to send only the values (flat array of prices) to save bandwidth
                price_history_payload = {}
                for inst_id, history in price_history.items():
                    price_history_payload[inst_id] = [p[1] for p in history]

                sorted_positions = sorted(list(positions.values()), key=lambda x: (x["instId"], x["posSide"]))
                await websocket.send_json({
                    "success": True,
                    "total_equity_usd": total_equity_usd,
                    "assets": sorted_assets,
                    "positions": sorted_positions,
                    "grid_strategies": grid_strategies,
                    "price_history": price_history_payload
                })


            # Throttling helper for send_state
            send_pending = False
            
            async def trigger_send_state():
                nonlocal send_pending
                if send_pending:
                    return
                send_pending = True
                await asyncio.sleep(0.1)  # Throttle to max 10 updates per second
                send_pending = False
                try:
                    await send_state()
                except Exception as e:
                    logger.debug(f"Failed to send throttled state: {e}")

            # OKX Private WebSocket messages listener
            async def okx_listener():
                nonlocal total_equity_usd
                try:
                    while True:
                        msg_str = await ows.recv()
                        if msg_str == "pong":
                            continue
                            
                        msg = json.loads(msg_str)
                        event = msg.get("event")
                        if event == "subscribe":
                            logger.info(f"Subscribed successfully to private: {msg.get('arg')}")
                            continue
                            
                        arg = msg.get("arg", {})
                        channel = arg.get("channel")
                        data_list = msg.get("data", [])
                        
                        if channel == "account" and data_list:
                            acct = data_list[0]
                            total_equity_usd = float(acct.get("totalEq", 0))
                            details = acct.get("details", [])
                            for d in details:
                                ccy = d.get("ccy")
                                eq = float(d.get("eq", 0))
                                avail = float(d.get("availBal", 0))
                                frozen = float(d.get("frozenBal", 0))
                                if eq > 0 or avail > 0 or frozen > 0:
                                    trading_assets[ccy] = {
                                        "trading_eq": eq,
                                        "trading_avail": avail,
                                        "trading_frozen": frozen,
                                        "valuation_usd": float(d.get("eqUsd", 0))
                                    }
                                elif ccy in trading_assets:
                                    del trading_assets[ccy]
                            await send_state()
                            
                        elif channel == "positions" and data_list:
                            for p in data_list:
                                pos_id = p.get("posId") or p.get("instId") + "_" + p.get("posSide")
                                pos_size = float(p.get("pos", 0))
                                if pos_size != 0:
                                    raw_side = p.get("posSide")
                                    if raw_side == "long":
                                        mapped_side = "long"
                                    elif raw_side == "short":
                                        mapped_side = "short"
                                    elif raw_side == "net":
                                        mapped_side = "long" if pos_size > 0 else "short"
                                    else:
                                        mapped_side = raw_side
                                        
                                    positions[pos_id] = {
                                        "instId": p.get("instId"),
                                        "mgnMode": p.get("mgnMode"),
                                        "posSide": mapped_side,
                                        "pos": abs(pos_size),
                                        "avgPx": float(p.get("avgPx", 0)),
                                        "cPx": float(p.get("cPx", 0)),
                                        "upl": float(p.get("upl", 0)),
                                        "uplRatio": float(p.get("uplRatio", 0)) * 100,
                                        "liqPx": float(p.get("liqPx", 0)) if p.get("liqPx") else None,
                                        "lever": p.get("lever"),
                                        "margin": float(p.get("margin", 0)) if p.get("margin") else 0.0
                                    }
                                elif pos_id in positions:
                                    del positions[pos_id]
                            
                            # Check subscriptions update
                            await update_ticker_subscriptions()
                            await send_state()
                            
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Error in OKX Private WS listener: {e}")
                    try:
                        await websocket.send_json({"success": False, "message": f"OKX 实时推送链路断开: {str(e)}"})
                    except Exception:
                        pass

            # OKX Public WebSocket messages listener (for tickers)
            async def pub_listener():
                try:
                    while True:
                        msg_str = await pub_ws.recv()
                        if msg_str == "pong":
                            continue
                            
                        msg = json.loads(msg_str)
                        event = msg.get("event")
                        if event == "subscribe":
                            logger.info(f"Subscribed successfully to public: {msg.get('arg')}")
                            continue
                            
                        arg = msg.get("arg", {})
                        channel = arg.get("channel")
                        data_list = msg.get("data", [])
                        
                        if channel == "tickers" and data_list:
                            ticker = data_list[0]
                            inst_id = ticker.get("instId")
                            last_price = float(ticker.get("last", 0))
                            
                            last_prices[inst_id] = last_price
                            
                            # Update price history
                            now = time.time()
                            if inst_id not in price_history:
                                price_history[inst_id] = []
                            if not price_history[inst_id] or (now - price_history[inst_id][-1][0] >= 2.0):
                                price_history[inst_id].append((now, last_price))
                            else:
                                price_history[inst_id][-1] = (price_history[inst_id][-1][0], last_price)
                            
                            # Keep only the last 5 minutes (300 seconds)
                            price_history[inst_id] = [p for p in price_history[inst_id] if now - p[0] <= 300]
                            
                            # Find all positions matching this instId

                            updated = False
                            for pos_id, p in positions.items():
                                if p["instId"] == inst_id:
                                    p["cPx"] = last_price
                                    
                                    # Recalculate PNL
                                    detail = await get_instrument_detail(inst_id)
                                    ct_val = detail["ctVal"]
                                    ct_type = detail["ctType"]
                                    avg_px = p["avgPx"]
                                    pos = p["pos"]
                                    pos_side = p["posSide"]
                                    
                                    # Calculate PNL (upl)
                                    if ct_type == "linear":
                                        if pos_side == "long":
                                            upl = pos * (last_price - avg_px) * ct_val
                                        else:
                                            upl = pos * (avg_px - last_price) * ct_val
                                    else: # inverse
                                        if last_price > 0 and avg_px > 0:
                                            if pos_side == "long":
                                                upl = pos * ct_val * (1.0 / avg_px - 1.0 / last_price)
                                            else:
                                                upl = pos * ct_val * (1.0 / last_price - 1.0 / avg_px)
                                        else:
                                            upl = 0.0
                                            
                                    p["upl"] = upl
                                    
                                    # Calculate PNL Ratio (uplRatio)
                                    try:
                                        lever = float(p.get("lever", 1))
                                    except Exception:
                                        lever = 1.0
                                        
                                    if avg_px > 0:
                                        if ct_type == "linear":
                                            if pos_side == "long":
                                                upl_ratio = ((last_price - avg_px) / avg_px) * lever * 100
                                            else:
                                                upl_ratio = ((avg_px - last_price) / avg_px) * lever * 100
                                        else: # inverse
                                            if pos_side == "long":
                                                upl_ratio = ((last_price - avg_px) / last_price) * lever * 100
                                            else:
                                                upl_ratio = ((avg_px - last_price) / last_price) * lever * 100
                                    else:
                                        upl_ratio = 0.0
                                        
                                    p["uplRatio"] = upl_ratio
                                    updated = True
                                    
                            grid_updated = any(g.get("instId") == inst_id for g in grid_strategies)
                            if updated or grid_updated:
                                await trigger_send_state()
                                
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Error in OKX Public WS listener: {e}")

            # Heartbeat ping task
            async def heartbeat():
                try:
                    while True:
                        await asyncio.sleep(20)
                        await ows.send("ping")
                        try:
                            await pub_ws.send("ping")
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

            # Loop to periodically fetch grid strategies from REST API
            async def grid_fetch_loop():
                nonlocal grid_strategies
                try:
                    while True:
                        res_grids = await fetch_grid_strategies(api_key, secret_key, passphrase, simulated)
                        grid_strategies = res_grids
                        await update_ticker_subscriptions()
                        await trigger_send_state()
                        await asyncio.sleep(8)  # Fetch every 8 seconds
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Error in grid fetch loop: {e}")

            # Launch background tasks
            listener_task = asyncio.create_task(okx_listener())
            pub_listener_task = asyncio.create_task(pub_listener())
            heartbeat_task = asyncio.create_task(heartbeat())
            grid_task = asyncio.create_task(grid_fetch_loop())
            
            try:
                while True:
                    # Keep browser socket open, listen for disconnects
                    await websocket.receive_text()
            except WebSocketDisconnect:
                logger.info("Browser client closed WebSocket connection.")
            finally:
                listener_task.cancel()
                pub_listener_task.cancel()
                heartbeat_task.cancel()
                grid_task.cancel()
                
    except Exception as e:
        logger.error(f"Failed to connect or maintain OKX WebSocket: {e}")
        try:
            await websocket.send_json({"success": False, "message": f"连接 OKX WebSocket 服务器失败: {str(e)}"})
        except Exception:
            pass

@app.get("/api/ma-bot/status")
async def get_ma_bot_status():
    """
    Returns the running status of the MA Crossover bot, including position data.
    """
    log_path = "/app-mikro/ma_bot.log"
    if not os.path.exists(log_path):
        log_path = os.path.join(BASE_DIR, "ma_bot.log")
        if not os.path.exists(log_path):
            log_path = os.path.join(BASE_DIR, "ma_bot_output.log")
            
    is_running = False
    
    # Check if process is running
    if sys.platform != "win32":
        try:
            import subprocess
            proc = subprocess.run(["pgrep", "-f", "run_ma_bot.py"], capture_output=True, text=True)
            if proc.stdout.strip():
                is_running = True
        except Exception:
            pass
            
    last_log_line = ""
    price = None
    fast_ma = None
    slow_ma = None
    last_time = None
    # Position data
    pos_side = "flat"
    pos_size = 0.0
    pos_avg_px = 0.0
    pos_upl = 0.0
    pos_upl_ratio = 0.0
    # Balance data
    avail_balance = 0.0
    
    if os.path.exists(log_path):
        try:
            import re
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-50:]
                
                # Find latest price/EMA/RSI line
                for line in reversed(lines):
                    if "Price:" in line and "EMA(200):" in line:
                        last_log_line = line.strip()
                        price_match = re.search(r'Price:\s*([\d.]+)', line)
                        fast_match = re.search(r'EMA\(200\):\s*([\d.]+)', line)
                        slow_match = re.search(r'RSI\(14\):\s*([\d.]+)', line)
                        if price_match:
                            price = float(price_match.group(1))
                        if fast_match:
                            fast_ma = float(fast_match.group(1))
                        if slow_match:
                            slow_ma = float(slow_match.group(1))
                        last_time = line.split("[")[0].strip()
                        break
                        
                # Find latest position line
                for line in reversed(lines):
                    if "Position: side=" in line:
                        side_match = re.search(r'side=(\w+)', line)
                        size_match = re.search(r'size=([\d.]+)', line)
                        avgpx_match = re.search(r'avgPx=([\d.]+)', line)
                        upl_match = re.search(r'upl=([-\d.]+)', line)
                        uplr_match = re.search(r'uplRatio=([-\d.]+)', line)
                        if side_match:
                            pos_side = side_match.group(1)
                        if size_match:
                            pos_size = float(size_match.group(1))
                        if avgpx_match:
                            pos_avg_px = float(avgpx_match.group(1))
                        if upl_match:
                            pos_upl = float(upl_match.group(1))
                        if uplr_match:
                            pos_upl_ratio = float(uplr_match.group(1))
                        break
                        
                # Find latest balance line
                for line in reversed(lines):
                    if "Balance: available=" in line:
                        bal_match = re.search(r'available=([\d.]+)', line)
                        if bal_match:
                            avail_balance = float(bal_match.group(1))
                        break
                        
        except Exception as e:
            logger.warning(f"Failed to read MA bot logs: {e}")

    # Read state file for last signal info
    state_data = {}
    state_path = "/app-mikro/ma_bot_state.json"
    if not os.path.exists(state_path):
        state_path = os.path.join(BASE_DIR, "ma_bot_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r") as f:
                state_data = json.load(f)
        except Exception:
            pass
            
    return {
        "is_running": is_running,
        "is_paused": state_data.get("is_paused", False),
        "last_time": last_time,
        "price": price,
        "fast_ma": fast_ma,
        "slow_ma": slow_ma,
        "last_log": last_log_line,
        "position": {
            "side": pos_side,
            "size": pos_size,
            "avgPx": pos_avg_px,
            "upl": pos_upl,
            "uplRatio": pos_upl_ratio,
            "stop_loss": state_data.get("stop_loss", 0.0),
            "take_profit": state_data.get("take_profit", 0.0),
        },
        "balance": avail_balance,
        "last_signal": state_data.get("last_signal", "none"),
    }

@app.post("/api/ma-bot/toggle")
async def toggle_ma_bot_pause():
    """
    Toggles the is_paused state of the MA bot in its state file.
    """
    state_path = "/app-mikro/ma_bot_state.json"
    if not os.path.exists(state_path):
        state_path = os.path.join(BASE_DIR, "ma_bot_state.json")
        
    state_data = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r") as f:
                state_data = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load state file for toggle: {e}")
            
    is_paused = state_data.get("is_paused", False)
    new_is_paused = not is_paused
    state_data["is_paused"] = new_is_paused
    
    try:
        tmp_file = state_path + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(state_data, f, indent=2)
        os.replace(tmp_file, state_path)
    except Exception as e:
        logger.error(f"Failed to save state file for toggle: {e}")
        return {"success": False, "error": f"Failed to save state: {str(e)}"}
        
    return {"success": True, "is_paused": new_is_paused}

@app.get("/api/ma-bot/trades")
async def get_ma_bot_trades():
    """Returns the bot's trade history from ma_bot_trades.json."""
    trades_path = "/app-mikro/ma_bot_trades.json"
    if not os.path.exists(trades_path):
        trades_path = os.path.join(BASE_DIR, "ma_bot_trades.json")
    if not os.path.exists(trades_path):
        return {"trades": []}
    try:
        with open(trades_path, "r", encoding="utf-8") as f:
            trades = json.load(f)
            return {"trades": trades[-50:]}  # last 50 trades
    except Exception as e:
        return {"trades": [], "error": str(e)}

@app.get("/api/ma-bot/logs")
async def get_ma_bot_logs():
    """
    Returns the last 100 lines of MA bot logs.
    """
    log_path = "/app-mikro/ma_bot.log"
    if not os.path.exists(log_path):
        log_path = os.path.join(BASE_DIR, "ma_bot.log")
        if not os.path.exists(log_path):
            log_path = os.path.join(BASE_DIR, "ma_bot_output.log")
            
    if not os.path.exists(log_path):
        return {"logs": ["暂无日志记录。机器人尚未启动或日志路径不正确。"]}
        
    try:
        with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-200:]
                # Filter out noisy DeprecationWarning and raw source code lines
                filtered = []
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if "DeprecationWarning" in stripped:
                        continue
                    if stripped.startswith("now = ") or stripped.startswith("  now = "):
                        continue
                    filtered.append(stripped)
                return {"logs": filtered[-100:]}
    except Exception as e:
        return {"logs": [f"读取日志失败: {str(e)}"]}

# Startup block

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8008)
