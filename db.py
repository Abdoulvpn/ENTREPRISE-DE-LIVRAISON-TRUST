"""
Couche d'accès aux données — TrustDelivery
SQLite est utilisé pour simplifier le déploiement (aucune dépendance externe).
Le schéma respecte la logique métier décrite dans le cahier des charges :
utilisateurs & rôles, produits & stocks, commandes, livraisons, facturation, audit.
"""
import sqlite3
import os
from datetime import datetime
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Allow overriding the database location via env var (Render persistent disk -> /var/data)
default_db_path = os.environ.get("DATABASE_PATH")
if not default_db_path:
    if os.path.isdir("/var/data"):
        default_db_path = os.path.join("/var/data", "trustdelivery.db")
    else:
        default_db_path = os.path.join(BASE_DIR, "trustdelivery.db")

DB_PATH = default_db_path

# Ensure the directory for the DB exists (useful when mounting persistent disk)
db_dir = os.path.dirname(DB_PATH)
if db_dir and not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
    except Exception:
        pass

ROLES = {
    "super_admin": "Super Administrateur",
    "moderateur": "Modérateur",
    "agent_confirmation": "Agent de confirmation",
    "livreur": "Livreur",
    "client": "Client",
}

ORDER_STATUSES = [
    ("en_attente", "En attente de confirmation", "#f59e0b", 1),
    ("confirmee", "Confirmée", "#3b82f6", 2),
    ("affectee", "Affectée à un livreur", "#6366f1", 3),
    ("en_livraison", "En cours de livraison", "#0ea5e9", 4),
    ("livree", "Livrée", "#16a34a", 5),
    ("annulee", "Annulée", "#dc2626", 6),
    ("retournee", "Retournée", "#ea580c", 7),
]


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset=False):
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    is_new = not os.path.exists(DB_PATH)
    conn = get_db()
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            phone TEXT,
            zone TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            location TEXT
        );

        CREATE TABLE IF NOT EXISTS zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            region TEXT,
            delivery_fee REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sku TEXT UNIQUE NOT NULL,
            description TEXT,
            category TEXT,
            supplier TEXT,
            price REAL NOT NULL DEFAULT 0,
            is_validated INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
            quantity INTEGER NOT NULL DEFAULT 0,
            alert_threshold INTEGER NOT NULL DEFAULT 5,
            UNIQUE(product_id, warehouse_id)
        );

        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
            movement_type TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            note TEXT,
            created_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS order_status_config (
            status_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            color TEXT NOT NULL,
            sort_order INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE NOT NULL,
            client_id INTEGER NOT NULL REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'en_attente',
            zone_id INTEGER REFERENCES zones(id),
            recipient_name TEXT,
            recipient_phone TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            shop_platform TEXT,
            shop_name TEXT,
            shop_order_ref TEXT,
            shop_order_url TEXT,
            delivery_address TEXT,
            total_amount REAL NOT NULL DEFAULT 0,
            delivery_fee REAL NOT NULL DEFAULT 0,
            confirmed_by INTEGER REFERENCES users(id),
            confirmed_at TEXT,
            livreur_id INTEGER REFERENCES users(id),
            assigned_at TEXT,
            delivered_at TEXT,
            cancel_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE NOT NULL,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            client_id INTEGER NOT NULL REFERENCES users(id),
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'impayee',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            paid_at TEXT
        );
        """
    )
    conn.commit()
    ensure_schema(conn)

    if is_new:
        seed(conn)

    conn.close()


def ensure_schema(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "recipient_name" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN recipient_name TEXT")
    if "recipient_phone" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN recipient_phone TEXT")
    if "source" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    if "shop_platform" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN shop_platform TEXT")
    if "shop_name" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN shop_name TEXT")
    if "shop_order_ref" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN shop_order_ref TEXT")
    if "shop_order_url" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN shop_order_url TEXT")
    conn.commit()


def seed(conn):
    cur = conn.cursor()

    # --- Statuts de commande (configurables, module Paramètres) ---
    for key, label, color, order in ORDER_STATUSES:
        cur.execute(
            "INSERT OR IGNORE INTO order_status_config (status_key, label, color, sort_order) VALUES (?,?,?,?)",
            (key, label, color, order),
        )

    # --- Compte Super Administrateur : Thierno Abdoul Keita ---
    cur.execute(
        """INSERT INTO users (full_name, email, password_hash, role, phone, zone, is_active)
           VALUES (?,?,?,?,?,?,1)""",
        (
            "Thierno Abdoul Keita",
            "thierno.keita@trustdelivery.com",
            generate_password_hash("TrustDelivery@2026"),
            "super_admin",
            "+224 600 00 00 01",
            "Conakry",
        ),
    )

    # --- Comptes de démonstration (un par rôle, pour pouvoir tester l'app) ---
    demo_users = [
        ("Aïssatou Camara", "moderateur@trustdelivery.com", "moderateur", "+224 600 00 00 02", "Conakry"),
        ("Mamadou Diallo", "agent@trustdelivery.com", "agent_confirmation", "+224 600 00 00 03", "Conakry"),
        ("Ibrahima Sory", "livreur@trustdelivery.com", "livreur", "+224 600 00 00 04", "Kindia"),
        ("Fatoumata Bah", "client@trustdelivery.com", "client", "+224 600 00 00 05", "Conakry"),
    ]
    for full_name, email, role, phone, zone in demo_users:
        cur.execute(
            """INSERT INTO users (full_name, email, password_hash, role, phone, zone, is_active)
               VALUES (?,?,?,?,?,?,1)""",
            (full_name, email, generate_password_hash("Demo@2026"), role, phone, zone),
        )

    # --- Entrepôt par défaut ---
    cur.execute("INSERT INTO warehouses (name, location) VALUES (?,?)", ("Entrepôt Central", "Conakry"))
    warehouse_id = cur.lastrowid

    # --- Zones de livraison ---
    zones = [("Conakry", "Région de Conakry", 15000), ("Kindia", "Région de Kindia", 30000), ("Kankan", "Région de Kankan", 45000)]
    for name, region, fee in zones:
        cur.execute("INSERT INTO zones (name, region, delivery_fee) VALUES (?,?,?)", (name, region, fee))

    # --- Produits de démonstration ---
    products = [
        ("Sac de riz 25kg", "PRD-001", "Riz importé qualité supérieure", "Alimentaire", "Fournisseur Soro", 250000),
        ("Carton huile végétale 12L", "PRD-002", "Huile végétale raffinée", "Alimentaire", "Fournisseur Soro", 180000),
        ("Pack eau minérale (12 bouteilles)", "PRD-003", "Eau minérale naturelle", "Boissons", "AquaPure", 35000),
        ("Sac de ciment 50kg", "PRD-004", "Ciment Portland", "Matériaux", "CimGuinée", 95000),
        ("Carton savon en poudre", "PRD-005", "Lessive en poudre 1kg x10", "Hygiène", "Fournisseur Bah", 60000),
    ]
    product_ids = []
    for name, sku, desc, cat, supplier, price in products:
        cur.execute(
            """INSERT INTO products (name, sku, description, category, supplier, price, is_validated)
               VALUES (?,?,?,?,?,?,1)""",
            (name, sku, desc, cat, supplier, price),
        )
        product_ids.append(cur.lastrowid)

    import random

    for pid in product_ids:
        qty = random.randint(3, 80)
        cur.execute(
            "INSERT INTO stock (product_id, warehouse_id, quantity, alert_threshold) VALUES (?,?,?,?)",
            (pid, warehouse_id, qty, 10),
        )
        cur.execute(
            """INSERT INTO stock_movements (product_id, warehouse_id, movement_type, quantity, note, created_by, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (pid, warehouse_id, "entree", qty, "Stock initial", 1, datetime.now().isoformat()),
        )

    conn.commit()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_action(user, action, details=""):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (user_id, user_name, action, details) VALUES (?,?,?,?)",
        (user["id"] if user else None, user["full_name"] if user else "Système", action, details),
    )
    conn.commit()
    conn.close()
