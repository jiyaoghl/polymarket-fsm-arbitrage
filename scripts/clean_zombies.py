import sqlite3
import time
from pathlib import Path
import sys

# 保证能引用 src 下的内容
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root / "src") not in sys.path:
    sys.path.insert(0, str(repo_root / "src"))

from polymarket.config import DB_PATH

def cleanup_zombies(max_age_hours: float = 2.0):
    """
    清理超时（默认超过 2 小时）未发生状态变更的挂单和单腿仓位，
    以释放本地风控对本金的锁定配额。
    """
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始清理数据库 {DB_PATH} 幽灵死单...")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cutoff_time = time.time() - (max_age_hours * 3600)
    # SQLite 的 created_at 通常是 DATETIME 格式，例如 '2026-08-11 14:00:00'
    # 我们可以用 strftime 将 cutoff_time 转换为该格式进行比较
    cutoff_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(cutoff_time))
    
    # 1. 清理幽灵仓位 (positions)
    cursor.execute("""
        SELECT token_id, created_at, status FROM positions 
        WHERE status != 'settled' AND created_at < ?
    """, (cutoff_str,))
    zombie_positions = cursor.fetchall()
    
    if zombie_positions:
        print(f"发现 {len(zombie_positions)} 个超过 {max_age_hours} 小时的幽灵仓位:")
        for z in zombie_positions:
            print(f"  - Token: {z[0]}, Created: {z[1]}, Status: {z[2]}")
            
        cursor.execute("""
            UPDATE positions SET status = 'settled'
            WHERE status != 'settled' AND created_at < ?
        """, (cutoff_str,))
        print(f"已将 {cursor.rowcount} 个幽灵仓位强制标记为 settled。")
    else:
        print("未发现超时的幽灵仓位。")
        
    # 2. 清理挂死的订单 (orders)
    cursor.execute("""
        SELECT order_id, created_at, status FROM orders 
        WHERE status = 'pending' AND created_at < ?
    """, (cutoff_str,))
    zombie_orders = cursor.fetchall()
    
    if zombie_orders:
        print(f"\n发现 {len(zombie_orders)} 个超过 {max_age_hours} 小时的超时待定订单:")
        for o in zombie_orders:
            print(f"  - Order: {o[0]}, Created: {o[1]}, Status: {o[2]}")
            
        cursor.execute("""
            UPDATE orders SET status = 'cancelled'
            WHERE status = 'pending' AND created_at < ?
        """, (cutoff_str,))
        print(f"已将 {cursor.rowcount} 个超时挂单强制标记为 cancelled。")
    else:
        print("未发现超时的挂单。")

    conn.commit()
    conn.close()
    print("\n清理完成！")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Clean up zombie trades.")
    parser.add_argument("--hours", type=float, default=2.0, help="Max age in hours (default: 2.0)")
    args = parser.parse_args()
    
    cleanup_zombies(args.hours)
