import sqlite3
import json
import os
import sys
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

DB_PATH = config.DB_PATH

def plot_portfolio():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT p.model_name, p.side, p.timestamp as pred_time,
               r.winning_outcome, r.resolved_at, m.outcome_prices
        FROM predictions p
        INNER JOIN resolutions r ON p.market_id = r.market_id
        INNER JOIN markets m ON p.market_id = m.id
        WHERE p.decision = 'BUY'
    ''')
    rows = c.fetchall()

    data = []
    
    for row in rows:
        d = dict(row)
        model_name = d['model_name'] or "Unknown Model"
        winning = d['winning_outcome']
        if not winning:
            continue
            
        winning = winning.strip().upper()
        pred_norm = d['side'].strip().upper() if d['side'] else ""
        
        is_correct = (winning == pred_norm)
        
        price = 0.5
        try:
            if d['outcome_prices']:
                prices = json.loads(d['outcome_prices'])
                if isinstance(prices, list) and len(prices) >= 2:
                    price = float(prices[0]) if pred_norm == 'YES' else float(prices[1])
                    if price <= 0.0 or price >= 1.0:
                        price = 0.5
        except:
            pass
            
        # Assuming a flat 1-unit bet size per trade
        # If correct, you win your stake multiplied by the inverse of the probability minus the stake.
        if is_correct:
            pnl = (1.0 / price) - 1.0
        else:
            pnl = -1.0
            
        try:
            res_date = datetime.strptime(d['resolved_at'][:19], '%Y-%m-%d %H:%M:%S')
            data.append({
                'model_name': model_name,
                'resolved_at': res_date,
                'pnl': pnl
            })
        except Exception as e:
            pass

    if not data:
        print("No BUY predictions found mapped to resolution dates.")
        return

    df = pd.DataFrame(data)
    df = df.sort_values('resolved_at')
    
    STARTING_CAPITAL = 1_000_000
    
    # Try different plot styles to make it look premium
    try:
        plt.style.use('seaborn-v0_8-darkgrid')
    except:
        plt.style.use('ggplot')
        
    plt.figure(figsize=(14, 8))
    
    # Track the final PnL for drawing labels
    for model_name, group in df.groupby('model_name'):
        group = group.copy()
        
        # Calculate exactly how much of the $1M gets bet on each prediction for this model
        stake_per_trade = STARTING_CAPITAL / len(group)
        
        # Convert raw unit PnL into actual dollar PnL based on the stake
        group['dollar_pnl'] = group['pnl'] * stake_per_trade
        
        # Calculate cumulative portfolio value starting from $1,000,000
        group['cumulative_portfolio'] = STARTING_CAPITAL + group['dollar_pnl'].cumsum()
        
        # Draw step plot representing portfolio jumps on resolution dates
        plt.step(group['resolved_at'], group['cumulative_portfolio'], where='post', label=f"{model_name}", linewidth=2.5, alpha=0.85)
        
        # Add a subtle scatter point for each resolution event
        plt.scatter(group['resolved_at'], group['cumulative_portfolio'], s=15, alpha=0.5)

    plt.title('Polymarket AI Backtest: Portfolio Value\n($1,000,000 Starting Capital Distributed Equally Across All Bets)', fontsize=15, fontweight='bold')
    plt.xlabel('Market Resolution Date', fontsize=12)
    plt.ylabel('Total Portfolio Value ($)', fontsize=12)
    
    # Improve the legend
    plt.legend(title='AI Models', loc='upper left', fontsize=10, title_fontsize=12, framealpha=0.9)
    plt.grid(True, linestyle='--', alpha=0.6)
    
    # The breakeven line at exactly $1,000,000
    plt.axhline(STARTING_CAPITAL, color='black', linewidth=1.5, zorder=1)
    
    # Format Y axis labels to display as standard dollar amounts
    plt.gca().yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))
    
    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio_performance.png")
    plt.tight_layout()
    plt.savefig(out_file, dpi=300, bbox_inches='tight')
    print(f"Chart successfully saved to {out_file}")

if __name__ == '__main__':
    plot_portfolio()
