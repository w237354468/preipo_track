import os
import sys
import time
import requests
import pandas as pd
import numpy as np

# We use the manual env loader just in case we need credentials, but market data is public
def load_dotenv():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dotenv_path = os.path.join(script_dir, ".env")
    if os.path.exists(dotenv_path):
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    val = val.strip().strip("'").strip('"')
                    os.environ[key.strip()] = val.strip()

load_dotenv()

BASE_URL = "https://www.okx.com"

def fetch_historical_candles(inst_id, bar='1H', limit=500):
    """
    Fetch up to 'limit' historical candles by paginating through OKX API
    """
    candles = []
    after = ""  # pagination anchor
    
    # We fetch in chunks of 100 (OKX max limit per request)
    chunks = (limit + 99) // 100
    print(f"Fetching {limit} candles for {inst_id} in {chunks} requests...")
    
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
                # The 'after' parameter is the timestamp of the oldest candle in the current response
                after = data[-1][0]
            else:
                print(f"Error fetching chunk {i}: {res}")
                break
        except Exception as e:
            print(f"Request failed: {e}")
            break
        time.sleep(0.1) # rate limit helper
        
    if not candles:
        return None
        
    # Format to DataFrame
    df = pd.DataFrame(candles, columns=[
        'ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
    ])
    
    # Convert types
    for col in ['open', 'high', 'low', 'close', 'vol']:
        df[col] = df[col].astype(float)
    df['ts'] = df['ts'].astype(int)
    
    # Reverse to chronological order (oldest first)
    df = df.iloc[::-1].reset_index(drop=True)
    return df

def calculate_bollinger_bands(df, period=20, std_dev=2):
    df['MB'] = df['close'].rolling(window=period).mean()
    df['std'] = df['close'].rolling(window=period).std()
    df['UP'] = df['MB'] + (df['std'] * std_dev)
    df['DN'] = df['MB'] - (df['std'] * std_dev)
    df['bandwidth'] = (df['UP'] - df['DN']) / df['MB']
    return df

def run_backtest(df, symbol, initial_balance=1000.0, fee_rate=0.01):
    """
    Backtest Bollinger Band Squeeze Breakout Strategy
    - Entry Long: Price closes above UP, Bandwidth is low (below 15% quantile of past 100 periods)
    - Entry Short: Price closes below DN, Bandwidth is low
    - Exit: Price crosses Middle Band (MB)
    """
    balance = initial_balance
    position = 0.0      # 1 = Long, -1 = Short, 0 = Flat
    entry_price = 0.0
    trades = []
    
    # Quantile threshold for bandwidth squeeze
    df['bw_low_threshold'] = df['bandwidth'].rolling(100).quantile(0.15)
    
    for i in range(1, len(df)):
        current = df.iloc[i]
        prev = df.iloc[i-1]
        
        # Skip if indicator not fully populated
        if pd.isna(current['UP']) or pd.isna(current['bw_low_threshold']):
            continue
            
        # Current conditions
        close = current['close']
        prev_close = prev['close']
        
        # Check signal if we are flat
        if position == 0:
            # Squeeze is active if bandwidth is below the low threshold
            is_squeezed = current['bandwidth'] < current['bw_low_threshold']
            
            # 1. Buy Breakout (Long)
            if close > current['UP'] and prev_close <= prev['UP'] and is_squeezed:
                position = 1.0
                entry_price = close
                # Apply entry fee
                fee = balance * fee_rate
                balance -= fee
                trades.append({
                    'type': 'BUY',
                    'price': entry_price,
                    'time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(current['ts']/1000)),
                    'balance_before': balance + fee,
                    'fee': fee
                })
                
            # 2. Sell Breakout (Short)
            elif close < current['DN'] and prev_close >= prev['DN'] and is_squeezed:
                position = -1.0
                entry_price = close
                # Apply entry fee
                fee = balance * fee_rate
                balance -= fee
                trades.append({
                    'type': 'SHORT',
                    'price': entry_price,
                    'time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(current['ts']/1000)),
                    'balance_before': balance + fee,
                    'fee': fee
                })
                
        # If we have an active position, check exit
        elif position == 1.0: # Long
            # Exit Long: Price crosses below MB
            if close < current['MB']:
                # Calculate return
                ret = (close - entry_price) / entry_price
                gross_pnl = balance * ret
                # Apply exit fee
                fee = (balance + gross_pnl) * fee_rate
                balance += gross_pnl - fee
                position = 0.0
                trades[-1]['exit_price'] = close
                trades[-1]['exit_time'] = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(current['ts']/1000))
                trades[-1]['pnl'] = gross_pnl - fee - trades[-1]['fee']
                trades[-1]['balance_after'] = balance
                
        elif position == -1.0: # Short
            # Exit Short: Price crosses above MB
            if close > current['MB']:
                # Calculate return
                ret = (entry_price - close) / entry_price
                gross_pnl = balance * ret
                # Apply exit fee
                fee = (balance + gross_pnl) * fee_rate
                balance += gross_pnl - fee
                position = 0.0
                trades[-1]['exit_price'] = close
                trades[-1]['exit_time'] = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(current['ts']/1000))
                trades[-1]['pnl'] = gross_pnl - fee - trades[-1]['fee']
                trades[-1]['balance_after'] = balance

    # Analyze trades
    completed_trades = [t for t in trades if 'exit_price' in t]
    total_trades = len(completed_trades)
    winning_trades = sum(1 for t in completed_trades if t['pnl'] > 0)
    win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
    total_pnl = balance - initial_balance
    
    # Max Drawdown calculation based on trade balance curve
    balance_curve = [initial_balance] + [t['balance_after'] for t in completed_trades]
    peaks = np.maximum.accumulate(balance_curve)
    drawdowns = (peaks - balance_curve) / peaks
    max_dd = np.max(drawdowns) if len(drawdowns) > 0 else 0.0
    
    return {
        'symbol': symbol,
        'initial_balance': initial_balance,
        'final_balance': balance,
        'total_pnl': total_pnl,
        'total_trades': total_trades,
        'win_rate': win_rate,
        'max_drawdown': max_dd,
        'trades': completed_trades
    }

def print_results(res, fee_name):
    print(f"\n=========================================")
    print(f" BACKTEST RESULTS: {res['symbol']} ({fee_name})")
    print(f"=========================================")
    print(f"Initial Balance : {res['initial_balance']:.2f} USDT")
    print(f"Final Balance   : {res['final_balance']:.2f} USDT")
    print(f"Total Net PnL   : {res['total_pnl']:.2f} USDT ({res['total_pnl']/res['initial_balance']*100:.2f}%)")
    print(f"Total Trades    : {res['total_trades']}")
    print(f"Win Rate        : {res['win_rate']*100:.2f}%")
    print(f"Max Drawdown    : {res['max_drawdown']*100:.2f}%")
    
    if res['trades']:
        print("\n--- Trade History Details ---")
        for i, t in enumerate(res['trades']):
            print(f"Trade #{i+1}: {t['type']} at {t['price']:.2f} on {t['time']} -> Exit at {t['exit_price']:.2f} on {t['exit_time']} | PnL: {t['pnl']:.2f} USDT (Fee paid: {t['fee']:.2f} + Exit Fee)")

def main():
    symbols = ['OPENAI-USDT-SWAP', 'ANTHROPIC-USDT-SWAP']
    
    # Fetch 800 hours (~33 days) of 1-hour candles
    limit = 800
    
    for symbol in symbols:
        df = fetch_historical_candles(symbol, bar='1H', limit=limit)
        if df is None or len(df) < 50:
            print(f"Could not load enough historical data for {symbol}.")
            continue
            
        df = calculate_bollinger_bands(df)
        
        # Run 1: User requested fee rate 0.01 (1%)
        res_high_fee = run_backtest(df, symbol, fee_rate=0.01)
        print_results(res_high_fee, "1.00% High Fee Rate")
        
        # Run 2: Realistic OKX taker fee rate 0.01% (0.0001)
        res_low_fee = run_backtest(df, symbol, fee_rate=0.0001)
        print_results(res_low_fee, "0.01% Standard OKX Fee Rate")

if __name__ == "__main__":
    main()
