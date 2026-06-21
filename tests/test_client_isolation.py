import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from werkzeug.security import generate_password_hash


TEST_DIR = tempfile.mkdtemp(prefix="trustdelivery-tests-")
os.environ["DATABASE_PATH"] = os.path.join(TEST_DIR, "test.db")

from app import app  # noqa: E402
from db import get_db  # noqa: E402
from routes.shop_routes import normalize_order_payload  # noqa: E402
from integrations import send_order_notification, sync_shop_status  # noqa: E402


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
        cls.courier_id = conn.execute(
            "SELECT id FROM users WHERE role='livreur' ORDER BY id LIMIT 1"
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
        cls.webhook_token = "integration-test-token"
        cur = conn.execute(
            "INSERT INTO shop_connections (client_id, platform, shop_name, webhook_token, default_zone_id) "
            "VALUES (?,?,?,?,?)",
            (cls.first_client_id, "custom", "Boutique Test", cls.webhook_token, cls.zone_id),
        )
        cls.connection_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO shop_connections (client_id, platform, shop_name, webhook_token, default_zone_id, store_url, api_key, api_secret) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cls.first_client_id, "woocommerce", "Woo Test", "woo-test-token", cls.zone_id, "https://shop.example", "ck_test", "cs_test"),
        )
        cls.woo_connection_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO orders (order_number, client_id, status, zone_id, recipient_name, recipient_phone, delivery_address, "
            "shop_connection_id, external_order_id, total_amount) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("CMD-WOO-SYNC", cls.first_client_id, "en_livraison", cls.zone_id, "Acheteur Woo", "+224 620 00 00 04", "Kaloum", cls.woo_connection_id, "991", 10000),
        )
        cls.woo_order_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO orders (order_number, client_id, status, zone_id, recipient_name, recipient_phone, delivery_address) "
            "VALUES (?,?,?,?,?,?,?)",
            ("CMD-NOTIFY", cls.first_client_id, "confirmee", cls.zone_id, "Acheteur SMS", "+224 620 00 00 05", "Kaloum"),
        )
        cls.notify_order_id = cur.lastrowid
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

    def test_store_webhook_creates_order_once(self):
        client = app.test_client()
        payload = {
            "external_order_id": "ONLINE-1001",
            "reference": "WEB-1001",
            "recipient_name": "Acheteur Boutique",
            "recipient_phone": "620000002",
            "shipping_address": {"address1": "Centre-ville", "city": "Conakry"},
            "items": [{"sku": "TEST-ALPHA", "quantity": 2}],
        }
        response = client.post(f"/boutiques/webhook/{self.webhook_token}", json=payload)
        self.assertEqual(response.status_code, 201)
        created_id = response.get_json()["order_id"]

        duplicate = client.post(f"/boutiques/webhook/{self.webhook_token}", json=payload)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.get_json()["duplicate"])
        self.assertEqual(duplicate.get_json()["order_id"], created_id)

        conn = get_db()
        count = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE shop_connection_id=? AND external_order_id='ONLINE-1001'",
            (self.connection_id,),
        ).fetchone()["c"]
        conn.close()
        self.assertEqual(count, 1)

    def test_store_webhook_rejects_unknown_sku(self):
        response = app.test_client().post(
            f"/boutiques/webhook/{self.webhook_token}",
            json={"external_order_id": "ONLINE-BAD", "items": [{"sku": "INCONNU", "quantity": 1}]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("SKU absent", response.get_json()["error"])

    def test_meta_webhook_verification(self):
        response = app.test_client().get(
            f"/boutiques/webhook/{self.webhook_token}",
            query_string={"hub.challenge": "challenge-ok", "hub.verify_token": self.webhook_token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "challenge-ok")

    def test_store_configuration_page_does_not_leak_tab_session_in_webhook_url(self):
        client = self.logged_client(self.first_client_id)
        response = client.get("/boutiques/?_tab=test-tab")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"/boutiques/webhook/{self.webhook_token}".encode(), response.data)
        self.assertNotIn(f"/boutiques/webhook/{self.webhook_token}?_tab=".encode(), response.data)

    def test_supported_store_payloads_are_normalized(self):
        examples = {
            "shopify": {
                "id": 101,
                "name": "#101",
                "shipping_address": {"first_name": "A", "last_name": "B", "phone": "1", "city": "Conakry"},
                "line_items": [{"sku": "TEST-ALPHA", "quantity": 1}],
            },
            "woocommerce": {
                "id": 102,
                "number": "102",
                "shipping": {"first_name": "A", "last_name": "B", "city": "Conakry"},
                "line_items": [{"sku": "TEST-ALPHA", "quantity": 1}],
            },
            "prestashop": {
                "id_order": 103,
                "reference": "PS103",
                "address_delivery": {"first_name": "A", "last_name": "B", "city": "Conakry"},
                "associations": {"order_rows": [{"product_reference": "TEST-ALPHA", "product_quantity": 1}]},
            },
            "facebook": {
                "external_order_id": "FB104",
                "recipient_name": "A B",
                "items": [{"sku": "TEST-ALPHA", "quantity": 1}],
            },
            "whatsapp": {
                "entry": [{"changes": [{"value": {
                    "contacts": [{"profile": {"name": "A B"}, "wa_id": "224620000003"}],
                    "messages": [{"id": "WA105", "from": "224620000003", "type": "order", "order": {
                        "product_items": [{"product_retailer_id": "TEST-ALPHA", "quantity": "1"}]
                    }}],
                }}]}],
            },
        }
        for platform, payload in examples.items():
            with self.subTest(platform=platform):
                normalized = normalize_order_payload(platform, payload)
                self.assertTrue(normalized["external_order_id"])
                self.assertEqual(normalized["items"], [{"sku": "TEST-ALPHA", "quantity": 1}])

    def test_dispatch_sends_notification_automatically(self):
        client = self.logged_client(self.admin_id)
        with patch("routes.orders_routes.send_order_notification", return_value=(True, "envoyée")) as notify:
            response = client.post(
                f"/commandes/{self.notify_order_id}/affecter?_tab=test-tab",
                data={"livreur_id": self.courier_id},
            )
        self.assertEqual(response.status_code, 302)
        notify.assert_called_once_with(self.notify_order_id, "assigned", unittest.mock.ANY)

    def test_notification_webhook_receives_recipient_and_message(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"{}"
        with patch.dict(os.environ, {"NOTIFICATION_WEBHOOK_URL": "https://notify.example/send"}), patch(
            "integrations.urlopen", return_value=response
        ) as request_mock:
            success, _message = send_order_notification(self.woo_order_id, "assigned", "Livreur Test")
        self.assertTrue(success)
        sent_request = request_mock.call_args.args[0]
        sent_payload = sent_request.data.decode("utf-8")
        self.assertIn("224620000004", sent_payload)
        self.assertIn("Livreur Test", sent_payload)

    def test_woocommerce_status_is_updated_to_completed(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"status":"completed"}'
        with patch("integrations.urlopen", return_value=response) as request_mock:
            success, message = sync_shop_status(self.woo_order_id, "livree")
        self.assertTrue(success)
        self.assertIn("completed", message)
        sent_request = request_mock.call_args.args[0]
        self.assertEqual(sent_request.method, "PUT")
        self.assertEqual(sent_request.full_url, "https://shop.example/wp-json/wc/v3/orders/991")
        self.assertEqual(sent_request.data, b'{"status": "completed"}')

    def test_delivery_triggers_store_status_sync(self):
        conn = get_db()
        conn.execute("UPDATE orders SET livreur_id=? WHERE id=?", (self.courier_id, self.woo_order_id))
        conn.commit()
        conn.close()
        client = self.logged_client(self.courier_id)
        with patch("routes.orders_routes.send_order_notification", return_value=(True, "envoyée")), patch(
            "routes.orders_routes.sync_shop_status", return_value=(True, "synchronisée")
        ) as sync:
            response = client.post(
                f"/commandes/{self.woo_order_id}/statut-livraison?_tab=test-tab",
                data={"new_status": "livree"},
            )
        self.assertEqual(response.status_code, 302)
        sync.assert_called_once_with(self.woo_order_id, "livree")


if __name__ == "__main__":
    unittest.main()
