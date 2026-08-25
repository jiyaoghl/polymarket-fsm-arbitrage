import os
import sys
import io
import sqlite3
from typing import List

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from polymarket.config import DB_PATH

def clear_all_history():
    print("==================================================")
    print("           Polymarket 交易历史清理工具            ")
    print("==================================================")

    # 寻找所有可能存在的 db 文件
    db_candidates = [
        DB_PATH,
        os.path.join(PROJECT_ROOT, "tmp", "trading.db"),
        os.path.join(PROJECT_ROOT, "trading.db"),
        os.path.join(PROJECT_ROOT, "data", "trading.db"),
    ]

    cleared_dbs = set()

    for db_file in db_candidates:
        if not os.path.exists(db_file):
            continue
        
        abs_path = os.path.abspath(db_file)
        if abs_path in cleared_dbs:
            continue

        print(f"\n[清理] 正在清理数据库: {abs_path}")
        try:
            conn = sqlite3.connect(abs_path, timeout=10)
            cur = conn.cursor()

            # 获取所有存在的表
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cur.fetchall()]

            target_tables = [
                "historical_trades",
                "active_trades_cache",
                "orders",
                "positions",
                "processed_markets",
                "ev_candidates"
            ]

            total_deleted = 0
            for t in target_tables:
                if t in tables:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    count = cur.fetchone()[0]
                    cur.execute(f"DELETE FROM {t}")
                    print(f"   - 数据表 [{t}]: 已清空 {count} 条记录")
                    total_deleted += count

            conn.commit()
            conn.execute("VACUUM")
            conn.close()
            cleared_dbs.add(abs_path)
            print(f"[完成] 数据库清理完毕！共清空 {total_deleted} 条记录。")

        except Exception as e:
            print(f"[异常] 清理数据库 {abs_path} 失败: {e}")

    print("\n[成功] 所有历史订单、归档交易及缓存记录已全量清空！")

if __name__ == "__main__":
    clear_all_history()
