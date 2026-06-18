import os
import sys
import pandas as pd
import numpy as np

def calculate_atr(df, period=14):
    df = df.copy()
    high = df['high']
    low = df['low']
    close = df['close']
    
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean() # Simple moving average of TR
    return atr

def run_supertrend_backtest(df, atr_period=10, multiplier=3.0, fee_rate=0.0001):
    df = df.copy()
    
    # Calculate ATR
    df['atr'] = calculate_atr(df, period=atr_period)
    
    # Supertrend calculation
    hl2 = (df['high'] + df['low']) / 2
    basic_upper = hl2 + (multiplier * df['atr'])
    basic_lower = hl2 - (multiplier * df['atr'])
    
    in_uptrend = [True] * len(df)
    final_upper = [0.0] * len(df)
    final_lower = [0.0] * len(df)
    
    if len(df) > 0:
        final_upper[0] = basic_upper.iloc[0]
        final_lower[0] = basic_lower.iloc[0]
        
    for i in range(1, len(df)):
        # Calculate final bands
        if in_uptrend[i-1]:
            final_lower[i] = max(basic_lower.iloc[i], final_lower[i-1])
            final_upper[i] = basic_upper.iloc[i]
        else:
            final_upper[i] = min(basic_upper.iloc[i], final_upper[i-1])
            final_lower[i] = basic_lower.iloc[i]
            
        # Determine trend
        if in_uptrend[i-1]:
            if df.loc[i, 'close'] < final_lower[i]:
                in_uptrend[i] = False
                final_upper[i] = basic_upper.iloc[i] # Reset upper band
            else:
                in_uptrend[i] = True
        else:
            if df.loc[i, 'close'] > final_upper[i]:
                in_uptrend[i] = True
                final_lower[i] = basic_lower.iloc[i] # Reset lower band
            else:
                in_uptrend[i] = False
                
    df['upper_band_final'] = final_upper
    df['lower_band_final'] = final_lower
    df['in_uptrend'] = in_uptrend
    
    # Simulate trades
    balance = 1000.0
    initial_balance = 1000.0
    position = 0.0 # 1 = Long, -1 = Short, 0 = Flat
    entry_price = 0.0
    trades = []
    
    for i in range(1, len(df)):
        current = df.iloc[i]
        prev = df.iloc[i-1]
        
        if pd.isna(current['atr']):
            continue
            
        close = current['close']
        
        # Entry / Flip signals
        trend_changed_to_up = not prev['in_uptrend'] and current['in_uptrend']
        trend_changed_to_down = prev['in_uptrend'] and not current['in_uptrend']
        
        if position == 0:
            if trend_changed_to_up:
                position = 1.0
                entry_price = close
                fee = balance * fee_rate
                balance -= fee
                trades.append({'type': 'BUY', 'price': entry_price, 'fee': fee})
            elif trend_changed_to_down:
                position = -1.0
                entry_price = close
                fee = balance * fee_rate
                balance -= fee
                trades.append({'type': 'SHORT', 'price': entry_price, 'fee': fee})
                
        elif position == 1.0: # Long
            if trend_changed_to_down:
                # Close Long
                ret = (close - entry_price) / entry_price
                gross_pnl = balance * ret
                fee = (balance + gross_pnl) * fee_rate
                balance += gross_pnl - fee
                trades[-1]['pnl'] = gross_pnl - fee - trades[-1]['fee']
                trades[-1]['balance_after'] = balance
                
                # Flip Short
                position = -1.0
                entry_price = close
                entry_fee = balance * fee_rate
                balance -= entry_fee
                trades.append({'type': 'SHORT', 'price': entry_price, 'fee': entry_fee})
                
        elif position == -1.0: # Short
            if trend_changed_to_up:
                # Close Short
                ret = (entry_price - close) / entry_price
                gross_pnl = balance * ret
                fee = (balance + gross_pnl) * fee_rate
                balance += gross_pnl - fee
                trades[-1]['pnl'] = gross_pnl - fee - trades[-1]['fee']
                trades[-1]['balance_after'] = balance
                
                # Flip Long
                position = 1.0
                entry_price = close
                entry_fee = balance * fee_rate
                balance -= entry_fee
                trades.append({'type': 'BUY', 'price': entry_price, 'fee': entry_fee})
                
    # Close last trade if still open
    if position != 0 and trades and 'pnl' not in trades[-1]:
        last_t = trades.pop()
        balance += last_t['fee']
        
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
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(script_dir, "data")
    
    results = {}
    fee_rate = 0.0001 # 0.01%
    
    # Grid search parameters
    atr_periods = [10, 14, 20]
    multipliers = [1.5, 2.0, 2.5, 3.0, 3.5]
    
    for symbol in symbols:
        results[symbol] = []
        for tf in timeframes:
            csv_path = os.path.join(data_dir, f"{symbol}_{tf}.csv")
            if not os.path.exists(csv_path):
                continue
            df = pd.read_csv(csv_path)
            
            for ap in atr_periods:
                for mult in multipliers:
                    pnl, num_trades, win, dd = run_supertrend_backtest(df, atr_period=ap, multiplier=mult, fee_rate=fee_rate)
                    results[symbol].append({
                        'timeframe': tf,
                        'atr_period': ap,
                        'multiplier': mult,
                        'pnl': pnl,
                        'pnl_pct': (pnl / 1000.0) * 100,
                        'trades': num_trades,
                        'win_rate': win,
                        'max_drawdown': dd
                    })
                    
        results[symbol] = sorted(results[symbol], key=lambda x: x['pnl'], reverse=True)
        
    for symbol in symbols:
        print(f"\n=========================================")
        print(f" TOP SUPERTREND CONFIGURATIONS FOR {symbol}")
        print(f"=========================================")
        for i, res in enumerate(results[symbol][:5]):
            print(f"#{i+1}: {res['timeframe']} Supertrend(ATR:{res['atr_period']}, Mult:{res['multiplier']:.1f}) | "
                  f"PnL: {res['pnl']:.2f} USDT ({res['pnl_pct']:.2f}%) | "
                  f"Trades: {res['trades']} | Win Rate: {res['win_rate']*100:.2f}% | Max DD: {res['max_drawdown']*100:.2f}%")

if __name__ == "__main__":
    main()
