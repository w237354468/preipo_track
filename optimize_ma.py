import os
import sys
import time
import requests
import pandas as pd
import numpy as np

BASE_URL = "https://www.okx.com"

def fetch_historical_candles(inst_id, bar='1H', limit=1500):
    """
    Fetch up to 'limit' historical candles by paginating through OKX API
    """
    candles = []
    after = ""  # pagination anchor
    chunks = (limit + 99) // 100
    print(f"Fetching {limit} candles for {inst_id} ({bar}) in {chunks} requests...")
    
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

def run_ma_backtest(df, fast_p, slow_p, ma_type='EMA', fee_rate=0.0001):
    df = df.copy()
    
    if ma_type == 'SMA':
        df['fast'] = df['close'].rolling(window=fast_p).mean()
        df['slow'] = df['close'].rolling(window=slow_p).mean()
    else: # EMA
        df['fast'] = df['close'].ewm(span=fast_p, adjust=False).mean()
        df['slow'] = df['close'].ewm(span=slow_p, adjust=False).mean()
        
    balance = 1000.0
    initial_balance = 1000.0
    position = 0.0      # 1 = Long, -1 = Short, 0 = Flat
    entry_price = 0.0
    trades = []
    
    for i in range(1, len(df)):
        current = df.iloc[i]
        prev = df.iloc[i-1]
        
        if pd.isna(current['slow']) or pd.isna(prev['slow']):
            continue
            
        close = current['close']
        
        # Golden Cross (Fast crosses above Slow)
        is_golden_cross = (prev['fast'] <= prev['slow']) and (current['fast'] > current['slow'])
        # Death Cross (Fast crosses below Slow)
        is_death_cross = (prev['fast'] >= prev['slow']) and (current['fast'] < current['slow'])
        
        if position == 0:
            if is_golden_cross:
                position = 1.0
                entry_price = close
                fee = balance * fee_rate
                balance -= fee
                trades.append({'type': 'BUY', 'price': entry_price, 'fee': fee})
            elif is_death_cross:
                position = -1.0
                entry_price = close
                fee = balance * fee_rate
                balance -= fee
                trades.append({'type': 'SHORT', 'price': entry_price, 'fee': fee})
                
        elif position == 1.0: # In Long position
            # Exit long on Death Cross, and optionally flip short
            if is_death_cross:
                ret = (close - entry_price) / entry_price
                gross_pnl = balance * ret
                fee = (balance + gross_pnl) * fee_rate
                balance += gross_pnl - fee
                trades[-1]['pnl'] = gross_pnl - fee - trades[-1]['fee']
                trades[-1]['balance_after'] = balance
                
                # Flip Short immediately
                position = -1.0
                entry_price = close
                entry_fee = balance * fee_rate
                balance -= entry_fee
                trades.append({'type': 'SHORT', 'price': entry_price, 'fee': entry_fee})
                
        elif position == -1.0: # In Short position
            # Exit short on Golden Cross, and optionally flip long
            if is_golden_cross:
                ret = (entry_price - close) / entry_price
                gross_pnl = balance * ret
                fee = (balance + gross_pnl) * fee_rate
                balance += gross_pnl - fee
                trades[-1]['pnl'] = gross_pnl - fee - trades[-1]['fee']
                trades[-1]['balance_after'] = balance
                
                # Flip Long immediately
                position = 1.0
                entry_price = close
                entry_fee = balance * fee_rate
                balance -= entry_fee
                trades.append({'type': 'BUY', 'price': entry_price, 'fee': entry_fee})

    # Close last trade if still open at end of data
    if position != 0 and trades and 'pnl' not in trades[-1]:
        last_t = trades.pop()
        balance += last_t['fee'] # refund entry fee for incomplete trade
        
    completed_trades = [t for t in trades if 'pnl' in t]
    total_trades = len(completed_trades)
    winning_trades = sum(1 for t in completed_trades if t['pnl'] > 0)
    win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
    total_pnl = balance - initial_balance
    
    # Max Drawdown
    balance_curve = [initial_balance] + [t['balance_after'] for t in completed_trades]
    peaks = np.maximum.accumulate(balance_curve)
    drawdowns = (peaks - balance_curve) / peaks
    max_dd = np.max(drawdowns) if len(drawdowns) > 0 else 0.0
    
    return total_pnl, total_trades, win_rate, max_dd

def main():
    symbols = ['OPENAI-USDT-SWAP', 'ANTHROPIC-USDT-SWAP']
    timeframes = ['15m', '30m', '1H']
    
    # Load data
    data_cache = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(script_dir, "data")
    
    for symbol in symbols:
        data_cache[symbol] = {}
        for tf in timeframes:
            csv_path = os.path.join(data_dir, f"{symbol}_{tf}.csv")
            if os.path.exists(csv_path):
                print(f"Loading {symbol} ({tf}) from local CSV...")
                df = pd.read_csv(csv_path)
                data_cache[symbol][tf] = df
                print(f"Loaded {len(df)} candles for {symbol} ({tf}) from CSV.")
            else:
                df = fetch_historical_candles(symbol, bar=tf, limit=1500)
                if df is not None and len(df) > 100:
                    data_cache[symbol][tf] = df
                    if not os.path.exists(data_dir):
                        os.makedirs(data_dir)
                    df.to_csv(csv_path, index=False)
                    print(f"Saved {symbol} ({tf}) to local CSV.")
                
    # Parameter combinations for MA Crossover
    ma_types = ['EMA', 'SMA']
    fast_periods = [5, 10, 15, 20]
    slow_periods = [20, 30, 50, 100]
    
    results = {}
    fee_rate = 0.0001 # 0.01%
    
    for symbol in symbols:
        results[symbol] = []
        for tf in timeframes:
            df = data_cache[symbol].get(tf)
            if df is None:
                continue
                
            for mt in ma_types:
                for fp in fast_periods:
                    for sp in slow_periods:
                        if sp <= fp:
                            continue
                            
                        pnl, num_trades, win, dd = run_ma_backtest(df, fp, sp, ma_type=mt, fee_rate=fee_rate)
                        results[symbol].append({
                            'timeframe': tf,
                            'ma_type': mt,
                            'fast_period': fp,
                            'slow_period': sp,
                            'pnl': pnl,
                            'pnl_pct': (pnl / 1000.0) * 100,
                            'trades': num_trades,
                            'win_rate': win,
                            'max_drawdown': dd
                        })
                        
        results[symbol] = sorted(results[symbol], key=lambda x: x['pnl'], reverse=True)
        
    # Print results
    for symbol in symbols:
        print(f"\n=========================================")
        print(f" TOP MA CONFIGURATIONS FOR {symbol}")
        print(f"=========================================")
        for i, res in enumerate(results[symbol][:5]):
            print(f"#{i+1}: {res['timeframe']} {res['ma_type']} ({res['fast_period']}/{res['slow_period']}) | "
                  f"PnL: {res['pnl']:.2f} USDT ({res['pnl_pct']:.2f}%) | "
                  f"Trades: {res['trades']} | Win Rate: {res['win_rate']*100:.2f}% | Max DD: {res['max_drawdown']*100:.2f}%")

if __name__ == "__main__":
    main()
