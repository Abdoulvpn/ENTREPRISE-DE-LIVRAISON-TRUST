import os
import shutil
import tempfile
import unittest

from werkzeug.security import generate_password_hash


TEST_DIR = tempfile.mkdtemp(prefix="trustdelivery-tests-")
os.environ["DATABASE_PATH"] = os.path.join(TEST_DIR, "test.db")

from app import app  # noqa: E402
from db import get_db  # noqa: E402


class ClientIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True)
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, is_active) VALUES (?,?,?,?,1)",
            ("Second Client", "second@example.com", generate_password_hash("test"), "client"),
        )
        cls.second_client_id = cur.lastrowid
        cls.first_client_id = conn.execute(
            "SELECT id FROM users WHERE email='client@trustdelivery.com'"
        ).fetchone()["id"]
        cls.admin_id = conn.execute(
            "SELECT id FROM users WHERE role='super_admin' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        cls.zone_id = conn.execute("SELECT id FROM zones ORDER BY id LIMIT 1").fetchone()["id"]
        cls.warehouse_id = conn.execute("SELECT id FROM warehouses ORDER BY id LIMIT 1").fetchone()["id"]

        cls.first_product_id = cls._insert_product(conn, "Stock Client Alpha", "TEST-ALPHA", cls.first_client_id)
        cls.second_product_id = cls._insert_product(conn, "Stock Client Beta", "TEST-BETA", cls.second_client_id)
        cur = conn.execute(
            "INSERT INTO orders (order_number, client_id, zone_id, recipient_name, recipient_phone, delivery_address) "
            "VALUES (?,?,?,?,?,?)",
            ("CMD-OTHER-CLIENT", cls.second_client_id, cls.zone_id, "Destinataire", "620000000", "Conakry"),
        )
        cls.second_order_id = cur.lastrowid
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_DIR, ignore_errors=True)

    @classmethod
    def _insert_product(cls, conn, name, sku, client_id):
        client = conn.execute("SELECT full_name FROM users WHERE id=?", (client_id,)).fetchone()
        cur = conn.execute(
            "INSERT INTO products (name, sku, supplier, supplier_client_id, price, is_validated) VALUES (?,?,?,?,?,1)",
            (name, sku, client["full_name"], client_id, 10000),
        )
        product_id = cur.lastrowid
        conn.execute(
            "INSERT INTO stock (product_id, warehouse_id, quantity, alert_threshold) VALUES (?,?,?,?)",
            (product_id, cls.warehouse_id, 20, 5),
        )
        return product_id

    def logged_client(self, user_id):
        client = app.test_client()
        with client.session_transaction() as session:
            session["tab_sessions"] = {"test-tab": user_id}
        return client

    def test_client_only_sees_own_orders(self):
        client = self.logged_client(self.first_client_id)
        response = client.get("/commandes/?_tab=test-tab")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"CMD-OTHER-CLIENT", response.data)

        response = client.get(f"/commandes/{self.second_order_id}?_tab=test-tab")
        self.assertEqual(response.status_code, 302)

    def test_client_only_sees_own_stock(self):
        client = self.logged_client(self.first_client_id)
        response = client.get("/produits/?_tab=test-tab")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Stock Client Alpha", response.data)
        self.assertNotIn(b"Stock Client Beta", response.data)

    def test_admin_can_filter_stock_by_client(self):
        client = self.logged_client(self.admin_id)
        response = client.get(f"/produits/?client_id={self.second_client_id}&_tab=test-tab")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Stock Client Beta", response.data)
        self.assertNotIn(b"Stock Client Alpha", response.data)

    def test_order_rejects_another_clients_product(self):
        client = self.logged_client(self.first_client_id)
        response = client.post(
            "/commandes/nouvelle?_tab=test-tab",
            data={
                "zone_id": self.zone_id,
                "recipient_name": "Client Test",
                "recipient_phone": "620000001",
                "delivery_address": "Kaloum",
                "product_id": [self.second_product_id],
                "quantity": [1],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("appartenir au stock du client".encode(), response.data)

    def test_shop_fields_are_optional(self):
        client = self.logged_client(self.first_client_id)
        response = client.post(
            "/commandes/nouvelle?_tab=test-tab",
            data={
                "zone_id": self.zone_id,
                "recipient_name": "Client Test",
                "recipient_phone": "620000001",
                "delivery_address": "Kaloum",
                "product_id": [self.first_product_id],
                "quantity": [1],
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = get_db()
        order = conn.execute(
            "SELECT shop_platform, shop_name, shop_order_ref, shop_order_url FROM orders "
            "WHERE client_id=? ORDER BY id DESC LIMIT 1",
            (self.first_client_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(order), ("", "", "", ""))


if __name__ == "__main__":
    unittest.main()
