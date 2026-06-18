import os
import time
import hmac
import base64
import hashlib
import requests

# Manual .env parser
def load_dotenv():
    # Use path of current script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dotenv_path = os.path.join(script_dir, ".env")
    if os.path.exists(dotenv_path):
        print("Loading environment variables from local .env file...")
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
IS_DEMO = os.getenv("OKX_ENVIRONMENT", "demo") == "demo"

BASE_URL = "https://www.okx.com"

def get_header(request_path, method="GET", body=""):
    # ISO format in UTC
    import datetime
    timestamp = datetime.datetime.utcnow().isoformat()[:-3] + 'Z'
    
    message = timestamp + method + request_path + body
    mac = hmac.new(bytes(SECRET_KEY, encoding='utf-8'), bytes(message, encoding='utf-8'), digestmod=hashlib.sha256)
    signature = base64.b64encode(mac.digest()).decode('utf-8')
    
    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
    }
    if IS_DEMO:
        headers["x-simulated-only"] = "1"
    return headers

def fetch_data(path):
    url = BASE_URL + path
    headers = get_header(path)
    try:
        res = requests.get(url, headers=headers, timeout=10)
        return res.json()
    except Exception as e:
        return {"error": str(e)}

def main():
    print("Fetching OKX Demo Account Data...")
    print(f"API_KEY: {API_KEY[:6]}...{API_KEY[-4:] if API_KEY else ''}")
    print(f"IS_DEMO: {IS_DEMO}")
    
    # 1. Fetch Account Balance
    balance_resp = fetch_data("/api/v5/account/balance")
    print("\n--- ACCOUNT BALANCE ---")
    if balance_resp.get("code") == "0":
        details = balance_resp["data"][0]["details"]
        for d in details:
            eq = float(d.get("eq", 0))
            if eq > 0:
                print(f"Asset: {d['ccy']}, Total Equity: {eq}, Available: {d.get('availBal', 0)}")
    else:
        print("Error fetching balance:", balance_resp)

    # 2. Fetch Positions
    pos_resp = fetch_data("/api/v5/account/positions")
    print("\n--- POSITIONS ---")
    if pos_resp.get("code") == "0":
        positions = pos_resp["data"]
        if not positions:
            print("No active positions.")
        for p in positions:
            print(f"Symbol: {p['instId']}, Side: {p['posSide']}, Leverage: {p['lever']}x, Pos size: {p['pos']}, Avg Price: {p['avgPx']}, Mark Price: {p['markPx']}, UnPnL: {p['upl']}")
    else:
        print("Error fetching positions:", pos_resp)

    # 3. Fetch Grid Strategies
    print("\n--- ACTIVE GRID STRATEGIES ---")
    for grid_type in ["grid", "contract_grid"]:
        grid_resp = fetch_data(f"/api/v5/tradingBot/grid/orders-algo-pending?algoOrdType={grid_type}")
        if grid_resp.get("code") == "0":
            algos = grid_resp["data"]
            if algos:
                print(f"Type: {grid_type}")
            else:
                print(f"Type {grid_type}: No active grid strategies.")
            for a in algos:
                print(f"  AlgoID: {a['algoId']}, Symbol: {a['instId']}, Min: {a['minPx']}, Max: {a['maxPx']}, Grids: {a['gridNum']}, Profit: {a.get('gridProfit', 0)}, FloatPnL: {a.get('floatProfit', 0)}")
        else:
            print(f"Error fetching {grid_type} grid:", grid_resp)

if __name__ == "__main__":
    main()
