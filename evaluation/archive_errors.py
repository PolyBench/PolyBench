import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'model_errors_log.md')

def archive_and_delete_errors():
    """
    Finds all 'ERROR' predictions, groups them by their Model-Provider signature and the Error Reason, 
    exports them to a permanent markdown log, and then purges them from the database.
    """
    import config
    DB_PATH = config.DB_PATH
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Query all unique error structures, ordered by model and frequency
    c.execute("""
        SELECT model_name, reasoning, COUNT(*) as count 
        FROM predictions 
        WHERE decision = 'ERROR' 
        GROUP BY model_name, reasoning 
        ORDER BY model_name, count DESC
    """)
    rows = c.fetchall()

    # 1. Append/Write to Log File
    with open(OUT_PATH, 'a', encoding='utf-8') as f:
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"\n\n# Error Archive Run: {timestamp}\n")
        
        if not rows:
            f.write("No 'ERROR' predictions found in the database during this run.\n")
        else:
            current_model = None
            total_logged = 0
            for r in rows:
                model = r['model_name']
                if model != current_model:
                    f.write(f"\n## {model}\n")
                    current_model = model
                
                reason = r['reasoning'].strip() if r['reasoning'] else 'Unknown Error'
                reason_short = reason.replace('\n', ' ')
                f.write(f"- **[{r['count']}x]** {reason_short}\n")
                total_logged += r['count']
                
            print(f"Archived {total_logged} errors across {len(rows)} unique signatures.")

    # 2. Delete all archived errors from database
    c.execute("DELETE FROM predictions WHERE decision = 'ERROR'")
    deleted_count = c.rowcount
    conn.commit()
    conn.close()
    
    print(f"Successfully purged {deleted_count} error rows from the predictions database.")

if __name__ == "__main__":
    archive_and_delete_errors()
