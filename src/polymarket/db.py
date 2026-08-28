

import os
import shutil
import time
import sqlite3
from contextlib import contextmanager
from typing import Optional
from polymarket.config import DB_PATH as _DB_PATH
from polymarket.logger import logger

def _recover_corrupted_db(path: str) -> None:
    """当 SQLite 数据库损坏时，自动备份并重建干净的数据库。"""
    try:
        if os.path.exists(path):
            bak_path = f"{path}.corrupted_{int(time.time())}.bak"
            shutil.move(path, bak_path)
            for ext in ["-wal", "-shm"]:
                wal_f = f"{path}{ext}"
                if os.path.exists(wal_f):
                    try:
                        os.remove(wal_f)
                    except Exception:
                        pass
            logger.warning(f"[DB] 检测到损坏的 SQLite 数据库，已自动备份至 {bak_path} 并重新初始化。")
    except Exception as e:
        logger.error(f"[DB] 自动恢复损坏数据库失败: {e}")

# ================= DB 基础操作 =================
@contextmanager
def get_conn(path: str = _DB_PATH):
    try:
        conn = sqlite3.connect(path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError as e:
        if "malformed" in str(e).lower() or "corrupt" in str(e).lower():
            _recover_corrupted_db(path)
            init_db(path)
            conn = sqlite3.connect(path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
        else:
            raise e

    try:
        yield conn
    except sqlite3.DatabaseError as e:
        if "malformed" in str(e).lower() or "corrupt" in str(e).lower():
            try:
                conn.close()
            except Exception:
                pass
            _recover_corrupted_db(path)
            init_db(path)
        raise e
    finally:
        try:
            conn.close()
        except Exception:
            pass


def init_db(path: str = _DB_PATH) -> None:
    """初始化 SQLite 数据库表结构。"""
    with get_conn(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_markets (
                market_id TEXT,
                strategy_id TEXT DEFAULT 'default',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (market_id, strategy_id)
            )
        """)
        # 兼容旧表升级：检查并自动添加 strategy_id 字段
        try:
            conn.execute("ALTER TABLE processed_markets ADD COLUMN strategy_id TEXT DEFAULT 'default'")
        except sqlite3.OperationalError:
            pass  # 字段已存在，忽略错误
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                market_id TEXT,
                token_id TEXT,
                side TEXT,
                price REAL,
                amount REAL,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                token_id TEXT PRIMARY KEY,
                market_id TEXT,
                side TEXT,
                amount REAL,
                cost_basis REAL,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ev_candidates (
                market_id TEXT PRIMARY KEY,
                slug TEXT,
                question TEXT,
                ev_score REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_trades_cache (
                market_id TEXT,
                strategy_id TEXT,
                trade_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (market_id, strategy_id)
            )
        """)
        conn.commit()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS historical_trades (
                market_id TEXT,
                strategy_id TEXT,
                trade_json TEXT,
                ev REAL,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (market_id, strategy_id)
            )
        """)
        conn.commit()

# ================= active_trades_cache 操作 =================

def upsert_trade_cache(market_id: str, strategy_id: str, trade_json: str, path: str = _DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("""
            INSERT INTO active_trades_cache (market_id, strategy_id, trade_json, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(market_id, strategy_id) DO UPDATE SET
                trade_json=excluded.trade_json,
                updated_at=CURRENT_TIMESTAMP
        """, (market_id, strategy_id, trade_json))
        conn.commit()

def delete_trade_cache(market_id: str, strategy_id: str, path: str = _DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("DELETE FROM active_trades_cache WHERE market_id=? AND strategy_id=?", (market_id, strategy_id))
        conn.commit()

def get_all_trade_caches(strategy_id: str, path: str = _DB_PATH) -> list:
    with get_conn(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT market_id, trade_json FROM active_trades_cache WHERE strategy_id=?", (strategy_id,)).fetchall()
        return [dict(r) for r in rows]

# ================= historical_trades 操作 =================

def archive_trade(market_id: str, strategy_id: str, trade_json: str, ev: float = 0.0, path: str = _DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO historical_trades (market_id, strategy_id, trade_json, ev, archived_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (market_id, strategy_id, trade_json, ev))
        conn.commit()

def get_historical_trades(strategy_id: str, limit: int = 10, path: str = _DB_PATH) -> list:
    with get_conn(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT market_id, strategy_id, trade_json, ev, archived_at FROM historical_trades WHERE strategy_id=? ORDER BY archived_at DESC LIMIT ?", 
            (strategy_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

# ================= processed_markets 操作 =================

def mark_market_processed(market_id: str, strategy_id: str = "default",
                          path: str = _DB_PATH) -> None:
    """持久化标记市场已处理，防止重启后重复入场。"""
    with get_conn(path) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO processed_markets (market_id, strategy_id)
            VALUES (?, ?)
        """, (market_id, strategy_id))
        conn.commit()


def is_market_processed(market_id: str, strategy_id: str = "default",
                        path: str = _DB_PATH) -> bool:
    """查询市场是否已被该策略处理。"""
    with get_conn(path) as conn:
        row = conn.execute("""
            SELECT 1 FROM processed_markets WHERE market_id=? AND strategy_id=?
        """, (market_id, strategy_id)).fetchone()
    return row is not None


# ================= ev_candidates 操作 =================

def upsert_candidate(c: dict, path: str = _DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("""
            INSERT INTO ev_candidates (market_id, slug, question, ev_score)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                slug=excluded.slug, question=excluded.question, ev_score=excluded.ev_score
        """, (c.get("market_id"), c.get("slug"), c.get("question"), c.get("ev_raw", 0.0)))
        conn.commit()


def get_fresh_candidates(min_ev: float = 0.0, max_age: int = 60, path: str = _DB_PATH) -> list:
    with get_conn(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT market_id, slug, question, ev_score FROM ev_candidates
            WHERE ev_score >= ?
        """, (min_ev,)).fetchall()
        return [dict(r) for r in rows]


# ================= orders 操作 =================

def push_order(order: dict, path: str = _DB_PATH) -> int:
    with get_conn(path) as conn:
        cursor = conn.execute("""
            INSERT INTO orders (market_id, token_id, side, price, amount, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (order.get("market_id"), order.get("token_id"), order.get("side"),
              order.get("entry_price", 0.0), order.get("size_usdc", 0.0), "pending"))
        conn.commit()
        return cursor.lastrowid


def pop_pending_orders(path: str = _DB_PATH) -> list:
    with get_conn(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT order_id, market_id, token_id, side, price, amount, 'FAST' as lane FROM orders
            WHERE status='pending'
        """).fetchall()
        return [dict(r) for r in rows]


def update_order_status(order_id: int, status: str, path: str = _DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("""
            UPDATE orders SET status=? WHERE rowid=? OR order_id=?
        """, (status, order_id, str(order_id)))
        conn.commit()


# ================= positions 操作 =================

def upsert_position(pos: dict, path: str = _DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("""
            INSERT INTO positions (token_id, market_id, side, amount, cost_basis, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_id) DO UPDATE SET
                amount=excluded.amount, cost_basis=excluded.cost_basis, status=excluded.status
        """, (pos.get("token_id") or pos.get("market_id"), pos.get("market_id"), pos.get("side"),
              pos.get("size_usdc", 0.0), pos.get("entry_price", 0.0), pos.get("status", "leg1_only")))
        conn.commit()


def count_open_positions(path: str = _DB_PATH) -> int:
    with get_conn(path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM positions WHERE status != 'settled'").fetchone()
        return row[0] if row else 0


def sum_open_positions_usdc(path: str = _DB_PATH) -> float:
    with get_conn(path) as conn:
        row = conn.execute("SELECT SUM(amount) FROM positions WHERE status != 'settled'").fetchone()
        return float(row[0]) if row and row[0] else 0.0


def mark_position_settled(market_id: str, pnl_usdc: float = 0.0, path: str = _DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("UPDATE positions SET status='settled' WHERE market_id=?", (market_id,))
        conn.commit()


def get_expired_unsettled(path: str = _DB_PATH) -> list:
    with get_conn(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT token_id, market_id, side, amount FROM positions WHERE status != 'settled'").fetchall()
        return [dict(r) for r in rows]


# ================= 历史数据彻底清理操作 =================

def clean_all_historical_trades(path: str = _DB_PATH) -> dict:
    """清空 SQLite 中所有历史交易、缓存、订单与已处理市场记录，使系统重置为零历史状态。"""
    counts = {}
    with get_conn(path) as conn:
        for tbl in ["active_trades_cache", "historical_trades", "orders", "positions", "processed_markets", "ev_candidates"]:
            try:
                cur = conn.execute(f"DELETE FROM {tbl}")
                counts[tbl] = cur.rowcount
            except Exception:
                counts[tbl] = 0
        conn.commit()
    logger.info(f"[DB] 已清空全部历史交易数据与缓存: {counts}")
    return counts


def get_all_historical_pnl_summary(path: str = _DB_PATH) -> dict:
    """聚合查询所有策略的历史已结算盈亏、手续费、胜率与订单总数，供 Web 与 Discord 统一使用。"""
    import json
    with get_conn(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT strategy_id, ev, trade_json FROM historical_trades").fetchall()

        strat_pnl = {}
        strat_counts = {}
        total_ev = 0.0
        total_fee = 0.0
        win_count = 0
        closed_count = 0

        for r in rows:
            sid = r["strategy_id"]
            ev = float(r["ev"] or 0.0)
            strat_pnl[sid] = strat_pnl.get(sid, 0.0) + ev
            strat_counts[sid] = strat_counts.get(sid, 0) + 1
            total_ev += ev

            try:
                t = json.loads(r["trade_json"]) if r["trade_json"] else {}
                fee = float(t.get("fee_usdc", 0.0) or 0.0)
                total_fee += fee
                st = t.get("status") or ""
                if st in ("locked", "settled"):
                    closed_count += 1
                    if ev > 0:
                        win_count += 1
            except Exception:
                pass

        win_rate = (win_count / closed_count * 100) if closed_count > 0 else 0.0

        return {
            "strategies_pnl": strat_pnl,
            "strategies_count": strat_counts,
            "total_net_ev": total_ev,
            "total_fee": total_fee,
            "total_trades": len(rows),
            "win_rate": win_rate
        }


