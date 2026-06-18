import os
import sys
import time
import requests
import pandas as pd

BASE_URL = "https://www.okx.com"

def fetch_historical_candles(inst_id, bar='1H', limit=1500):
    candles = []
    after = ""
    chunks = (limit + 99) // 100
    print(f"Fetching {limit} candles for {inst_id} ({bar})...")
    
    for i in range(chunks):
        url = f"{BASE_URL}/api/v5/market/history-candles?instId={inst_id}&bar={bar}&limit=100"
        if after:
            url += f"&after={after}"
            
        try:
            res = requests.get(url, timeout=10).json()
            if res.get("code") == "0" and res.get("data"):
                data = res["data"]
                candles.extend(data)
                if len(data) < 100:
                    break
                after = data[-1][0]
            else:
                print(f"Error fetching chunk {i}: {res}")
                break
        except Exception as e:
            print(f"Request failed: {e}")
            break
        time.sleep(0.05)
        
    if not candles:
        return None
        
    df = pd.DataFrame(candles, columns=[
        'ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
    ])
    for col in ['open', 'high', 'low', 'close', 'vol']:
        df[col] = df[col].astype(float)
    df['ts'] = df['ts'].astype(int)
    df = df.iloc[::-1].reset_index(drop=True)
    return df

def main():
    symbols = ['OPENAI-USDT-SWAP', 'ANTHROPIC-USDT-SWAP']
    timeframes = ['15m', '30m', '1H']
    
    # Create data directory if not exists
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
        print(f"Created directory: {data_dir}")
        
    for symbol in symbols:
        for tf in timeframes:
            csv_path = os.path.join(data_dir, f"{symbol}_{tf}.csv")
            
            # Fetch and save
            df = fetch_historical_candles(symbol, bar=tf, limit=1500)
            if df is not None and len(df) > 0:
                df.to_csv(csv_path, index=False)
                print(f"Saved {len(df)} candles to {csv_path}")
            else:
                print(f"Failed to fetch data for {symbol} ({tf})")

if __name__ == "__main__":
    main()
