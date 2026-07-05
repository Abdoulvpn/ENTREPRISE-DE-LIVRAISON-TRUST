"""
Couche d'accès aux données — TrustDelivery
SQLite est utilisé pour simplifier le déploiement (aucune dépendance externe).
Le schéma respecte la logique métier décrite dans le cahier des charges :
utilisateurs & rôles, produits & stocks, commandes, livraisons, facturation, audit.
"""
import sqlite3
import os
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_database_path():
    """Choisit un emplacement persistant et refuse l'éphémère en production."""
    explicit_path = os.environ.get("DATABASE_PATH", "").strip()
    if explicit_path:
        return os.path.abspath(explicit_path)

    mount_dir = (
        os.environ.get("PERSISTENT_DATA_DIR", "").strip()
        or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        or os.environ.get("RENDER_DISK_MOUNT_PATH", "").strip()
    )
    if mount_dir:
        return os.path.join(os.path.abspath(mount_dir), "trustdelivery.db")
    if os.path.isdir("/var/data"):
        return "/var/data/trustdelivery.db"
    if os.path.isdir("/data") and (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("FLASK_ENV") == "production"):
        return "/data/trustdelivery.db"

    is_hosted_production = bool(
        os.environ.get("RENDER")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
        or os.environ.get("REQUIRE_PERSISTENT_DATABASE") == "1"
    )
    if is_hosted_production:
        raise RuntimeError(
            "Aucun volume persistant détecté. Configurez DATABASE_PATH vers le volume "
            "(ex. /var/data/trustdelivery.db ou /data/trustdelivery.db). Démarrage refusé "
            "pour éviter toute perte de comptes, commandes ou stocks."
        )
    return os.path.join(BASE_DIR, "trustdelivery.db")


default_db_path = resolve_database_path()

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

# Compte administrateur demandé explicitement. Seul le hachage fort est versionné ;
# le mot de passe temporaire doit être changé dès la première connexion.
REQUIRED_ADMIN_ACCOUNTS = (
    (
        "Administrateur TrustDelivery",
        "admin@trustdelivery.com",
        "scrypt:32768:8:1$hFGGWigRFQ1v6YW6$1fb19aae80039abb3583894d609f0e10a9ae02eeb7a38b881512af30b05c93d1c51a0dbf88fa0d60519d27c3a33f7c3f1168d371c5a8245784c630ad9558ef99",
    ),
    (
        "Daouda Bangoura",
        "daoudabangoura@trustdelivery.com",
        "scrypt:32768:8:1$H61pz7oK40UAFzIf$0a3f3bfd35a3d5f9013d914efa7644e2258e28b4a9b207c2c4014dc9f023ad2d2a4ec4681d6f5a3546531ab75c340b75e7b33b40b4f3f89b110809a1cb13c3c4",
    ),
)

ORDER_STATUSES = [
    ("en_attente", "En attente de confirmation", "#f59e0b", 1),
    ("confirmee", "Confirmée", "#3b82f6", 2),
    ("proposee", "Proposée au livreur", "#8b5cf6", 3),
    ("affectee", "Acceptée par le livreur", "#6366f1", 4),
    ("en_livraison", "En cours de livraison", "#0ea5e9", 5),
    ("expediee", "Expédiée", "#0284c7", 6),
    ("livree", "Livrée", "#16a34a", 7),
    ("reportee", "Reportée", "#d97706", 8),
    ("interessee", "Intéressée", "#ca8a04", 9),
    ("pdr", "PDR", "#f97316", 10),
    ("injoignable", "Injoignable", "#a16207", 11),
    ("refusee", "Refusée", "#b91c1c", 12),
    ("annulee", "Annulée", "#dc2626", 13),
    ("retournee", "Retournée", "#ea580c", 14),
]


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def backup_database(force=False):
    """Crée une sauvegarde SQLite cohérente sur le même volume persistant."""
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
        return None
    backup_dir = os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    date_key = datetime.now(timezone.utc).strftime("%Y%m%d")
    if not force:
        daily_path = os.path.join(backup_dir, f"trustdelivery-{date_key}.db")
        if os.path.exists(daily_path):
            return daily_path
        target = daily_path
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        target = os.path.join(backup_dir, f"trustdelivery-{stamp}.db")
    temporary = f"{target}.{os.getpid()}.tmp"
    source = sqlite3.connect(DB_PATH, timeout=10)
    destination = sqlite3.connect(temporary)
    try:
        source.backup(destination)
        destination.close()
        source.close()
        os.replace(temporary, target)
    except Exception:
        destination.close()
        source.close()
        if os.path.exists(temporary):
            os.remove(temporary)
        raise
    backups = sorted(
        (os.path.join(backup_dir, name) for name in os.listdir(backup_dir) if name.endswith(".db")),
        key=os.path.getmtime,
        reverse=True,
    )
    for old_backup in backups[10:]:
        try:
            os.remove(old_backup)
        except OSError:
            pass
    return target


def init_db(reset=False):
    if reset:
        if os.environ.get("ALLOW_DATABASE_RESET") != "1":
            raise RuntimeError("Réinitialisation de la base bloquée par sécurité.")
        if os.path.exists(DB_PATH):
            backup_database(force=True)
            os.remove(DB_PATH)

    is_new = not os.path.exists(DB_PATH)
    if not is_new:
        backup_database()
    conn = get_db()
    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        conn.close()
        raise RuntimeError(f"Base SQLite endommagée, démarrage arrêté : {integrity}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
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
            whatsapp_phone TEXT,
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
            supplier_client_id INTEGER REFERENCES users(id),
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
            shop_connection_id INTEGER REFERENCES shop_connections(id),
            external_order_id TEXT,
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

        CREATE TABLE IF NOT EXISTS shop_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES users(id),
            platform TEXT NOT NULL,
            shop_name TEXT NOT NULL,
            webhook_token TEXT UNIQUE NOT NULL,
            default_zone_id INTEGER NOT NULL REFERENCES zones(id),
            store_url TEXT,
            api_key TEXT,
            api_secret TEXT,
            status_callback_url TEXT,
            auto_dispatch INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS shop_sync_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id INTEGER NOT NULL REFERENCES shop_connections(id),
            external_order_id TEXT,
            status TEXT NOT NULL,
            message TEXT,
            order_id INTEGER REFERENCES orders(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS notification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            channel TEXT NOT NULL,
            recipient TEXT,
            status TEXT NOT NULL,
            message TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS courier_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            livreur_id INTEGER NOT NULL REFERENCES users(id),
            order_id INTEGER NOT NULL REFERENCES orders(id),
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            accuracy REAL,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES users(id),
            product_title TEXT NOT NULL,
            ref TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            description TEXT,
            link TEXT,
            photo TEXT,
            shipment_date TEXT NOT NULL,
            validated INTEGER NOT NULL DEFAULT 0,
            product_id INTEGER REFERENCES products(id),
            created_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS courier_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            courier_id INTEGER NOT NULL REFERENCES users(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            order_id INTEGER REFERENCES orders(id),
            quantity_taken INTEGER NOT NULL DEFAULT 0,
            taken_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'pris_en_charge',
            UNIQUE(courier_id, product_id, order_id)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            expense_date TEXT NOT NULL DEFAULT (date('now')),
            created_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS whatsapp_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            phone_number TEXT NOT NULL,
            otp_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            is_verified INTEGER NOT NULL DEFAULT 0,
            verified_at TEXT,
            last_sent_at TEXT NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            link TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            read_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS push_device_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL DEFAULT 'android',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    ensure_schema(conn)

    if is_new:
        seed(conn)

    ensure_required_admins(conn)

    conn.close()


def ensure_required_admins(conn):
    for full_name, email, password_hash in REQUIRED_ADMIN_ACCOUNTS:
        existing = conn.execute("SELECT id FROM users WHERE lower(email)=lower(?)", (email,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET role='super_admin', is_active=1 WHERE id=?", (existing["id"],))
        else:
            conn.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_active) VALUES (?,?,?,'super_admin',1)",
                (full_name, email, password_hash),
            )
    conn.commit()


def ensure_schema(conn):
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "whatsapp_phone" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN whatsapp_phone TEXT")
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
    if "shop_connection_id" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN shop_connection_id INTEGER REFERENCES shop_connections(id)")
    if "external_order_id" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN external_order_id TEXT")
    product_columns = {row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "supplier_client_id" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN supplier_client_id INTEGER REFERENCES users(id)")
    if "link" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN link TEXT")
    if "photo" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN photo TEXT")
    stock_columns = {row["name"] for row in conn.execute("PRAGMA table_info(stock)").fetchall()}
    if "initial_quantity" not in stock_columns:
        conn.execute("ALTER TABLE stock ADD COLUMN initial_quantity INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE stock SET initial_quantity=quantity WHERE initial_quantity=0")
    if "damaged_quantity" not in stock_columns:
        conn.execute("ALTER TABLE stock ADD COLUMN damaged_quantity INTEGER NOT NULL DEFAULT 0")
    if "delivered_quantity" not in stock_columns:
        conn.execute("ALTER TABLE stock ADD COLUMN delivered_quantity INTEGER NOT NULL DEFAULT 0")
    if "note" not in stock_columns:
        conn.execute("ALTER TABLE stock ADD COLUMN note TEXT")
    if "visible_seller" not in stock_columns:
        conn.execute("ALTER TABLE stock ADD COLUMN visible_seller INTEGER NOT NULL DEFAULT 0")
    if "is_validated" not in stock_columns:
        conn.execute("ALTER TABLE stock ADD COLUMN is_validated INTEGER NOT NULL DEFAULT 1")
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "manager_id" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN manager_id INTEGER REFERENCES users(id)")
    if "client_type" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN client_type TEXT NOT NULL DEFAULT 'seller'")
    if "parent_courier_id" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN parent_courier_id INTEGER REFERENCES users(id)")
    order_columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "courier_paid_amount" not in order_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN courier_paid_amount REAL NOT NULL DEFAULT 0")
    connection_columns = {row["name"] for row in conn.execute("PRAGMA table_info(shop_connections)").fetchall()}
    if "store_url" not in connection_columns:
        conn.execute("ALTER TABLE shop_connections ADD COLUMN store_url TEXT")
    if "api_key" not in connection_columns:
        conn.execute("ALTER TABLE shop_connections ADD COLUMN api_key TEXT")
    if "api_secret" not in connection_columns:
        conn.execute("ALTER TABLE shop_connections ADD COLUMN api_secret TEXT")
    if "status_callback_url" not in connection_columns:
        conn.execute("ALTER TABLE shop_connections ADD COLUMN status_callback_url TEXT")
    if "auto_dispatch" not in connection_columns:
        conn.execute("ALTER TABLE shop_connections ADD COLUMN auto_dispatch INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_courier_locations_order_time "
        "ON courier_locations(order_id, recorded_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_notifications_unread "
        "ON user_notifications(user_id, is_read, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_push_device_tokens_user "
        "ON push_device_tokens(user_id, is_active)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_shop_external "
        "ON orders(shop_connection_id, external_order_id) "
        "WHERE shop_connection_id IS NOT NULL AND external_order_id IS NOT NULL"
    )
    proposed_exists = conn.execute(
        "SELECT 1 FROM order_status_config WHERE status_key='proposee'"
    ).fetchone()
    if not proposed_exists:
        conn.execute("UPDATE order_status_config SET sort_order=sort_order+1 WHERE sort_order>=3")
        conn.execute(
            "UPDATE order_status_config SET label='Acceptée par le livreur' "
            "WHERE status_key='affectee' AND label='Affectée à un livreur'"
        )
    for key, label, color, order in ORDER_STATUSES:
        conn.execute(
            "INSERT OR IGNORE INTO order_status_config (status_key, label, color, sort_order) VALUES (?,?,?,?)",
            (key, label, color, order),
        )
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
            """INSERT INTO products (name, sku, description, category, supplier, supplier_client_id, price, is_validated)
               VALUES (?,?,?,?,?,?,?,1)""",
            (name, sku, desc, cat, supplier, 5, price),
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


def create_user_notification(user_id, title, message, link=""):
    """Crée une notification interne sans exposer de données sensibles."""
    safe_link = ""
    if link:
        parsed = urlsplit(link)
        if not parsed.scheme and not parsed.netloc and parsed.path.startswith("/") and not parsed.path.startswith("//"):
            query = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if key != "_tab"])
            safe_link = urlunsplit(("", "", parsed.path, query, parsed.fragment))
    conn = get_db()
    conn.execute(
        "INSERT INTO user_notifications (user_id, title, message, link) VALUES (?,?,?,?)",
        (user_id, title[:120], message[:500], safe_link[:500] if safe_link else None),
    )
    conn.commit()
    conn.close()
