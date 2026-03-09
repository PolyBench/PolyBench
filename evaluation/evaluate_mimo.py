import sqlite3
import json
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from collections import defaultdict

DB_PATH = config.DB_PATH
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mimo_evaluation_report.md")


def simulate_order_book_fill(order_book_json, pred_side, budget):
    """Simulate filling a market buy against the ask side. Returns (spent, shares, vwap, ob_exists)."""
    if not order_book_json or budget <= 0:
        return 0.0, 0.0, None, False
    try:
        ob_data = json.loads(order_book_json)
    except (json.JSONDecodeError, TypeError):
        return 0.0, 0.0, None, False
    if not ob_data or not isinstance(ob_data, dict):
        return 0.0, 0.0, None, False

    outcome_keys = list(ob_data.keys())
    target_ob = None
    for key in outcome_keys:
        if key.strip().upper() == pred_side.strip().upper():
            target_ob = ob_data[key]
            break
    if target_ob is None:
        if pred_side == 'YES':
            target_ob = ob_data.get('Yes') or ob_data.get('yes') or ob_data.get('YES')
            if target_ob is None and len(outcome_keys) >= 1:
                target_ob = ob_data[outcome_keys[0]]
        elif pred_side == 'NO':
            target_ob = ob_data.get('No') or ob_data.get('no') or ob_data.get('NO')
            if target_ob is None and len(outcome_keys) >= 2:
                target_ob = ob_data[outcome_keys[1]]
    if not target_ob:
        return 0.0, 0.0, None, True
    asks = target_ob.get('asks', [])
    if not asks:
        return 0.0, 0.0, None, True

    asks_sorted = sorted(asks, key=lambda x: float(x['price']))
    remaining, total_shares, total_spent = budget, 0.0, 0.0
    for level in asks_sorted:
        p, s = float(level['price']), float(level['size'])
        if p <= 0.0 or p >= 1.0:
            continue
        buy = min(remaining / p, s)
        cost = buy * p
        total_shares += buy
        total_spent += cost
        remaining -= cost
        if remaining <= 0.001:
            break
    if total_shares <= 0:
        return 0.0, 0.0, None, True
    return total_spent, total_shares, total_spent / total_shares, True

def calculate_ece(confidences, accuracies, bins=10):
    if not confidences:
        return 0.0
    bin_boundaries = [i/bins for i in range(bins+1)]
    ece = 0.0
    for i in range(bins):
        bin_lower = bin_boundaries[i]
        bin_upper = bin_boundaries[i + 1]
        
        in_bin = [j for j, conf in enumerate(confidences) if bin_lower <= conf <= bin_upper]
        if not in_bin:
            continue
            
        bin_acc = sum([accuracies[j] for j in in_bin]) / len(in_bin)
        bin_conf = sum([confidences[j] for j in in_bin]) / len(in_bin)
        bin_weight = len(in_bin) / len(confidences)
        
        ece += bin_weight * abs(bin_acc - bin_conf)
    return ece

def f1_score(y_true, y_pred):
    tp = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 1 and yp == 1)
    fp = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 0 and yp == 1)
    fn = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 1 and yp == 0)
    
    if tp + fp == 0:
        precision = 0.0
    else:
        precision = tp / (tp + fp)
        
    if tp + fn == 0:
        recall = 0.0
    else:
        recall = tp / (tp + fn)
        
    if precision + recall == 0:
        return 0.0
    return 2 * (precision * recall) / (precision + recall)

def mean(arr):
    if not arr: return 0.0
    return sum(arr)/len(arr)

def std_dev(arr):
    if len(arr) < 2: return 0.0
    m = mean(arr)
    var = sum((x - m) ** 2 for x in arr) / (len(arr) - 1)
    return var ** 0.5

def sharpe_ratio(apys):
    if len(apys) < 2: return 0.0
    # Annualized Sharpe ratio assuming risk-free rate ~ 0%
    s = std_dev(apys)
    if s == 0: return 0.0
    return mean(apys) / s

def analyze(lot_size=10.0):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('''
        SELECT p.id, p.market_id, p.event_id, p.model_name, p.decision, p.side, p.confidence, p.reasoning, p.raw_response, p.timestamp as pred_time,
               r.winning_outcome, r.resolved_at, m.outcome_prices, m.outcomes,
               ms.order_book_snapshot
        FROM predictions p
        LEFT JOIN resolutions r ON p.market_id = r.market_id
        LEFT JOIN markets m ON p.market_id = m.id
        LEFT JOIN market_snapshots ms ON p.market_id = ms.market_id
    ''')
    rows = c.fetchall()
    
    all_stats = defaultdict(lambda: defaultdict(lambda: {
        'total': 0, 'correct': 0, 'y_true': [], 'y_pred': [], 'confidences': [], 'accuracies': [],
        'date_errors': [], 'apys': [], 'wrong_sells': 0, 'errors': 0, 'skips': 0
    }))
    
    for row in rows:
        d = dict(row)
        decision = d['decision']
        model_name = d['model_name'] or "Unknown Model"
        model_stats = all_stats[model_name]
        
        if decision == 'ERROR':
            model_stats['overall']['errors'] += 1
            continue
        if decision == 'SKIP':
            model_stats['overall']['skips'] += 1
            continue
            
        raw = d['raw_response']
        decision_data = {}
        try:
            parsed = json.loads(raw)
            if 'decisions' in parsed:
                for item in parsed['decisions']:
                    if item.get('side') == d['side'] and abs(item.get('confidence', 0) - d['confidence']) < 0.05:
                        decision_data = item
                        break
                if not decision_data and parsed['decisions']:
                    decision_data = parsed['decisions'][0]
            else:
                decision_data = parsed
        except:
            pass
            
        strategies = decision_data.get('strategy', ['unknown'])
        if isinstance(strategies, str):
            strategies = [strategies]
            
        est_res_date_str = decision_data.get('est_resolution_date')
        
        winning = d['winning_outcome']
        if not winning:
            continue
            
        win_norm = winning.strip().upper()
        pred_norm = d['side'].strip().upper() if d['side'] else ''
        
        # Directly compare the strings since prompts now just output the exact outcome directly
        mapped_pred = pred_norm

        if decision == 'BUY':
            is_correct = (win_norm == mapped_pred)
        elif decision == 'SELL':
            is_correct = (win_norm != mapped_pred)
        else:
            is_correct = False

        if decision == 'SELL':
            for s in strategies + ['overall']:
                model_stats[s]['wrong_sells'] += 1
                
        date_error_days = None
        if est_res_date_str and d['resolved_at']:
            try:
                est_d = datetime.strptime(est_res_date_str[:10], '%Y-%m-%d')
                res_d = datetime.strptime(d['resolved_at'][:10], '%Y-%m-%d')
                date_error_days = abs((res_d - est_d).days)
            except:
                pass
                
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
            
        conf = float(d['confidence']) if d['confidence'] else 0.5
        y_t = 1 if is_correct else 0
        y_p = 1 if decision == 'BUY' else 0
        
        unit_investment = 0.0
        unit_profit = 0.0
        cwr_investment = 0.0
        cwr_profit = 0.0
        raw_ret = None
        apy = None
        
        if decision == 'BUY':
            unit_budget = lot_size
            cwr_budget = lot_size * conf
            
            # 1. Non-CWR Unit Fill
            spent_u, shares_u, vwap_u, ob_exists_u = simulate_order_book_fill(
                d.get('order_book_snapshot'), pred_norm, unit_budget
            )
            if ob_exists_u:
                unit_investment = spent_u
                if shares_u > 0:
                    unit_profit = (shares_u * 1.0 - spent_u) if is_correct else -spent_u
                else:
                    unit_profit = 0.0
            else:
                unit_investment = unit_budget
                unit_profit = unit_investment * ((1.0 / price) - 1.0) if is_correct else -unit_investment
                
            # 2. CWR Fill
            spent_c, shares_c, vwap_c, ob_exists_c = simulate_order_book_fill(
                d.get('order_book_snapshot'), pred_norm, cwr_budget
            )
            if ob_exists_c:
                cwr_investment = spent_c
                if shares_c > 0:
                    cwr_profit = (shares_c * 1.0 - spent_c) if is_correct else -spent_c
                else:
                    cwr_profit = 0.0
            else:
                cwr_investment = cwr_budget
                cwr_profit = cwr_investment * ((1.0 / price) - 1.0) if is_correct else -cwr_investment
                
            if unit_investment > 0:
                raw_ret = unit_profit / unit_investment
                try:
                    p_time = datetime.strptime(d['pred_time'][:19], '%Y-%m-%d %H:%M:%S')
                    r_time = datetime.strptime(d['resolved_at'][:19], '%Y-%m-%d %H:%M:%S')
                    days_held = max(1, (r_time - p_time).days)
                    apy = raw_ret * (365.0 / days_held)
                except:
                    pass

        # The loop tracks apys and other metrics for all strategies and 'overall'
        for s in strategies + ['overall']:
            model_stats[s]['total'] += 1
            if is_correct:
                model_stats[s]['correct'] += 1
            model_stats[s]['y_true'].append(y_t)
            model_stats[s]['y_pred'].append(y_p)
            model_stats[s]['confidences'].append(conf)
            model_stats[s]['accuracies'].append(1 if is_correct else 0)
            if date_error_days is not None:
                model_stats[s]['date_errors'].append(date_error_days)
            if apy is not None:
                model_stats[s]['apys'].append(apy)
                
            # Track raw returns (applicable to BUYs only for accurate profit tracking in this dataset context)
            if 'returns' not in model_stats[s]:
                model_stats[s]['returns'] = []
            if raw_ret is not None:
                model_stats[s]['returns'].append(raw_ret)
                
            if 'cwr_profits' not in model_stats[s]:
                model_stats[s]['cwr_profits'] = 0.0
                model_stats[s]['cwr_investments'] = 0.0
            model_stats[s]['cwr_profits'] += cwr_profit
            model_stats[s]['cwr_investments'] += cwr_investment

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("# OpenRouter Evaluation Report\n\n")
        
        for model_name, stats in all_stats.items():
            f.write(f"## Model: `{model_name}`\n\n")
            overall = stats['overall']
            
            c.execute("SELECT COUNT(*) FROM predictions WHERE model_name = ? AND decision = 'ERROR'", (model_name,))
            total_db_errors = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM predictions WHERE model_name = ? AND decision = 'SKIP'", (model_name,))
            total_db_skips = c.fetchone()[0]
            
            f.write(f"**Total Evaluated (Resolved & Traded):** {overall['total']}\n\n")
            f.write(f"**Total Errors in DB:** {total_db_errors}\n\n")
            f.write(f"**Total Skips in DB:** {total_db_skips}\n\n")
            f.write(f"**Total Wrong SELLs (Hallucination?):** {overall['wrong_sells']}\n\n")
            
            f.write("### Strategy Metrics\n\n")
            
            for s, st in stats.items():
                if s == 'overall' and st['total'] == 0:
                    continue
                if st['total'] == 0:
                    continue
                acc = st['correct'] / st['total']
                f1 = f1_score(st['y_true'], st['y_pred'])
                ece = calculate_ece(st['confidences'], st['accuracies'])
                avg_date_err = mean(st['date_errors'])
                avg_apy = mean(st['apys'])
                
                cwr_inv = st.get('cwr_investments', 0.0)
                cwr_return = (st.get('cwr_profits', 0.0) / cwr_inv) if cwr_inv > 0 else 0.0
                
                avg_non_cwr = mean(st.get('returns', []))
                sharpe = sharpe_ratio(st['apys'])
                avg_conf = mean(st['confidences'])
                
                f.write(f"#### Strategy: `{s.upper()}` (N={st['total']})\n")
                f.write(f"- **Accuracy:** {acc:.1%}\n")
                f.write(f"- **F1 Score:** {f1:.3f}\n")
                f.write(f"- **Avg Confidence:** {avg_conf:.3f}\n")
                f.write(f"- **ECE:** {ece:.3f}\n")
                f.write(f"- **Avg Date Error:** {avg_date_err:.1f} days\n")
                f.write(f"- **Avg APY:** {avg_apy:.1%}\n")
                f.write(f"- **Sharpe Ratio (APY):** {sharpe:.2f}\n")
                f.write(f"- **Non-CWR Return:** {avg_non_cwr:.1%}\n")
                f.write(f"- **CWR Return:** {cwr_return:.1%} (Total invested: ${cwr_inv:.2f})\n\n")
                
            f.write("### ERROR Log Analysis\n\n")
            c.execute("""
                SELECT reasoning, COUNT(*) as count 
                FROM predictions 
                WHERE model_name = ? AND decision = 'ERROR'
                GROUP BY reasoning
                ORDER BY count DESC
                LIMIT 10
            """, (model_name,))
            for row in c.fetchall():
                reason = row['reasoning'][:150].replace('\n', ' ') if row['reasoning'] else 'Unknown'
                f.write(f"- **[{row['count']}x]** {reason}\n")
    
            f.write("\n### SKIP Reasons Analysis (Top 10)\n\n")
            c.execute("""
                SELECT reasoning, COUNT(*) as count 
                FROM predictions 
                WHERE model_name = ? AND decision = 'SKIP'
                GROUP BY reasoning
                ORDER BY count DESC
                LIMIT 10
            """, (model_name,))
            for row in c.fetchall():
                reason = row['reasoning'][:200].replace('\n', ' ') if row['reasoning'] else 'Unknown'
                f.write(f"- **[{row['count']}x]** {reason}\n")
                
            f.write("\n---\n\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate model predictions.")
    parser.add_argument("--lot-size", type=float, default=10.0,
                        help="Base investment per trade in USD (default: 10)")
    args = parser.parse_args()
    analyze(lot_size=args.lot_size)
