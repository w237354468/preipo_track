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
    
    # We fetch in chunks of 100
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
        time.sleep(0.05) # rate limit helper
        
    if not candles:
        return None
        
    df = pd.DataFrame(candles, columns=[
        'ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
    ])
    
    for col in ['open', 'high', 'low', 'close', 'vol']:
        df[col] = df[col].astype(float)
    df['ts'] = df['ts'].astype(int)
    
    # Reverse to chronological order (oldest first)
    df = df.iloc[::-1].reset_index(drop=True)
    return df

def run_backtest(df, period, std_dev, squeeze_quantile, fee_rate=0.0001):
    # Copy dataframe to avoid overwriting original
    df = df.copy()
    
    # Calculate indicators
    df['MB'] = df['close'].rolling(window=period).mean()
    df['std'] = df['close'].rolling(window=period).std()
    df['UP'] = df['MB'] + (df['std'] * std_dev)
    df['DN'] = df['MB'] - (df['std'] * std_dev)
    df['bandwidth'] = (df['UP'] - df['DN']) / df['MB']
    
    if squeeze_quantile < 1.0:
        df['bw_low_threshold'] = df['bandwidth'].rolling(100).quantile(squeeze_quantile)
    else:
        df['bw_low_threshold'] = 999.0 # always squeezed
        
    balance = 1000.0
    initial_balance = 1000.0
    position = 0.0      # 1 = Long, -1 = Short, 0 = Flat
    entry_price = 0.0
    trades = []
    
    for i in range(1, len(df)):
        current = df.iloc[i]
        prev = df.iloc[i-1]
        
        if pd.isna(current['UP']) or (squeeze_quantile < 1.0 and pd.isna(current['bw_low_threshold'])):
            continue
            
        close = current['close']
        prev_close = prev['close']
        
        # Check signal if we are flat
        if position == 0:
            is_squeezed = current['bandwidth'] < current['bw_low_threshold']
            
            # Buy Breakout (Long)
            if close > current['UP'] and prev_close <= prev['UP'] and is_squeezed:
                position = 1.0
                entry_price = close
                fee = balance * fee_rate
                balance -= fee
                trades.append({'type': 'BUY', 'price': entry_price, 'fee': fee})
                
            # Sell Breakout (Short)
            elif close < current['DN'] and prev_close >= prev['DN'] and is_squeezed:
                position = -1.0
                entry_price = close
                fee = balance * fee_rate
                balance -= fee
                trades.append({'type': 'SHORT', 'price': entry_price, 'fee': fee})
                
        # If we have an active position, check exit (cross mid band)
        elif position == 1.0:
            if close < current['MB']:
                ret = (close - entry_price) / entry_price
                gross_pnl = balance * ret
                fee = (balance + gross_pnl) * fee_rate
                balance += gross_pnl - fee
                position = 0.0
                trades[-1]['pnl'] = gross_pnl - fee - trades[-1]['fee']
                trades[-1]['balance_after'] = balance
                
        elif position == -1.0:
            if close > current['MB']:
                ret = (entry_price - close) / entry_price
                gross_pnl = balance * ret
                fee = (balance + gross_pnl) * fee_rate
                balance += gross_pnl - fee
                position = 0.0
                trades[-1]['pnl'] = gross_pnl - fee - trades[-1]['fee']
                trades[-1]['balance_after'] = balance

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
    
    # Cache data to avoid querying OKX too frequently during grid search
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
                # We fetch 1500 candles for a robust test
                df = fetch_historical_candles(symbol, bar=tf, limit=1500)
                if df is not None and len(df) > 100:
                    data_cache[symbol][tf] = df
                    if not os.path.exists(data_dir):
                        os.makedirs(data_dir)
                    df.to_csv(csv_path, index=False)
                    print(f"Saved {symbol} ({tf}) to local CSV. Time range: {pd.to_datetime(df['ts'].iloc[0], unit='ms')} to {pd.to_datetime(df['ts'].iloc[-1], unit='ms')}")
                else:
                    print(f"Failed to load candles for {symbol} ({tf})")

    # Grid search parameters
    periods = [20, 30, 50]
    std_devs = [1.5, 2.0, 2.5]
    squeeze_quantiles = [0.15, 0.30, 0.50, 1.0] # 1.0 means no squeeze filter
    
    # Fee is 0.01% (万一 = 0.0001) as requested by user
    fee_rate = 0.0001
    
    results = {}
    
    for symbol in symbols:
        results[symbol] = []
        for tf in timeframes:
            df = data_cache[symbol].get(tf)
            if df is None:
                continue
                
            for p in periods:
                for sd in std_devs:
                    for sq in squeeze_quantiles:
                        pnl, num_trades, win, dd = run_backtest(df, p, sd, sq, fee_rate=fee_rate)
                        results[symbol].append({
                            'timeframe': tf,
                            'period': p,
                            'std_dev': sd,
                            'squeeze_quantile': sq,
                            'pnl': pnl,
                            'pnl_pct': (pnl / 1000.0) * 100,
                            'trades': num_trades,
                            'win_rate': win,
                            'max_drawdown': dd
                        })
                        
        # Sort results by PnL descending
        results[symbol] = sorted(results[symbol], key=lambda x: x['pnl'], reverse=True)

    # Output report
    report_lines = []
    report_lines.append("# Bollinger Band Strategy Parameter Optimization Report")
    report_lines.append(f"Tested Fee Rate: **0.01% (万一 / 0.0001)**\n")
    
    for symbol in symbols:
        report_lines.append(f"## Top 5 Configurations for **{symbol}**")
        report_lines.append("| Rank | Timeframe | BB Period | Std Dev | Squeeze Quantile | Net PnL (USDT) | Net Return | Trades | Win Rate | Max Drawdown |")
        report_lines.append("| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        
        top_configs = results[symbol][:5]
        for i, config in enumerate(top_configs):
            sq_str = f"{config['squeeze_quantile']*100:.0f}%" if config['squeeze_quantile'] < 1.0 else "None"
            report_lines.append(
                f"| {i+1} | {config['timeframe']} | {config['period']} | {config['std_dev']} | {sq_str} | "
                f"{config['pnl']:.2f} | {config['pnl_pct']:.2f}% | {config['trades']} | {config['win_rate']*100:.2f}% | {config['max_drawdown']*100:.2f}% |"
            )
        report_lines.append("")
        
    # Write to local markdown file
    report_content = "\n".join(report_lines)
    # Write to the artifacts directory as a markdown report
    output_path = "/Users/zhiqiangwei/.gemini/antigravity-cli/brain/21f071c6-d60a-4284-ba8f-cd7379511f27/optimization_results.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_content)
        
    print(f"\nOptimization complete! Results written to: {output_path}")

    # Also print the best result to console
    for symbol in symbols:
        if results[symbol]:
            best = results[symbol][0]
            sq_str = f"{best['squeeze_quantile']*100:.0f}%" if best['squeeze_quantile'] < 1.0 else "None"
            print(f"\\nBest Config for {symbol}:")
            print(f"  Timeframe: {best['timeframe']}, BB Period: {best['period']}, Std Dev: {best['std_dev']}, Squeeze: {sq_str}")
            print(f"  Net PnL: {best['pnl']:.2f} USDT ({best['pnl_pct']:.2f}%) | Trades: {best['trades']} | Win Rate: {best['win_rate']*100:.2f}% | Max DD: {best['max_drawdown']*100:.2f}%")

if __name__ == "__main__":
    main()
