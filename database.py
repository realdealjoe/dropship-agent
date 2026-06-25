import sqlite3
import json
import os
from contextlib import contextmanager
from typing import Optional

# DATA_DIR is /data on Railway (persistent volume), local dir otherwise
_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_DATA_DIR, "dropship.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                shopify_product_id TEXT PRIMARY KEY,
                shopify_variant_id TEXT,
                supplier TEXT NOT NULL,
                supplier_product_id TEXT NOT NULL,
                supplier_sku TEXT,
                cost_price REAL NOT NULL,
                last_updated TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                shopify_order_id TEXT PRIMARY KEY,
                shopify_order_number TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                supplier TEXT,
                supplier_order_id TEXT,
                tracking_number TEXT,
                tracking_url TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shopify_order_id TEXT,
                customer_email TEXT,
                customer_name TEXT,
                messages TEXT NOT NULL DEFAULT '[]',
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS niches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                active INTEGER DEFAULT 1
            );
        """)


def upsert_product(shopify_product_id: str, shopify_variant_id: str,
                   supplier: str, supplier_product_id: str,
                   supplier_sku: str, cost_price: float):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(shopify_product_id) DO UPDATE SET
                supplier=excluded.supplier,
                supplier_product_id=excluded.supplier_product_id,
                supplier_sku=excluded.supplier_sku,
                cost_price=excluded.cost_price,
                last_updated=datetime('now')
        """, (shopify_product_id, shopify_variant_id, supplier,
               supplier_product_id, supplier_sku, cost_price))


def get_product(shopify_product_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE shopify_product_id = ?",
            (shopify_product_id,)
        ).fetchone()
    return dict(row) if row else None


def get_all_products() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM products").fetchall()
    return [dict(r) for r in rows]


def upsert_order(shopify_order_id: str, **kwargs):
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT shopify_order_id FROM orders WHERE shopify_order_id = ?",
            (shopify_order_id,)
        ).fetchone()
        if existing:
            fields = ", ".join(f"{k}=?" for k in kwargs)
            conn.execute(
                f"UPDATE orders SET {fields}, updated_at=datetime('now') WHERE shopify_order_id=?",
                list(kwargs.values()) + [shopify_order_id]
            )
        else:
            kwargs["shopify_order_id"] = shopify_order_id
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" * len(kwargs))
            conn.execute(f"INSERT INTO orders ({cols}) VALUES ({placeholders})", list(kwargs.values()))


def get_order(shopify_order_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE shopify_order_id = ?",
            (shopify_order_id,)
        ).fetchone()
    return dict(row) if row else None


def save_conversation(shopify_order_id: str, customer_email: str,
                      customer_name: str, messages: list) -> int:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM conversations WHERE shopify_order_id = ? AND status = 'open'",
            (shopify_order_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE conversations SET messages=?, updated_at=datetime('now') WHERE id=?",
                (json.dumps(messages), existing["id"])
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO conversations (shopify_order_id, customer_email, customer_name, messages) VALUES (?,?,?,?)",
            (shopify_order_id, customer_email, customer_name, json.dumps(messages))
        )
        return cur.lastrowid


def get_active_niches() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT keyword FROM niches WHERE active=1").fetchall()
    return [r["keyword"] for r in rows]


def add_niche(keyword: str):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO niches (keyword) VALUES (?)", (keyword,))
