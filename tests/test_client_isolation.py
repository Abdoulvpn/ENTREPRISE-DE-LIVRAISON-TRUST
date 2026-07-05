import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from werkzeug.security import generate_password_hash, check_password_hash


TEST_DIR = tempfile.mkdtemp(prefix="trustdelivery-tests-")
os.environ["DATABASE_PATH"] = os.path.join(TEST_DIR, "test.db")

from app import app  # noqa: E402
from db import get_db, backup_database, init_db, maintain_database_backups  # noqa: E402
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
        conn.execute("UPDATE users SET whatsapp_phone='224620000099' WHERE id=?", (cls.courier_id,))
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

    def test_product_archiving_preserves_stock_history(self):
        conn = get_db()
        product_id = self._insert_product(
            conn, "Archive Test", "TEST-ARCHIVE", self.first_client_id
        )
        conn.execute(
            "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, quantity, note, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (product_id, self.warehouse_id, "entree", 20, "Archive test", self.admin_id),
        )
        conn.commit()
        conn.close()

        response = self.logged_client(self.admin_id).post(
            f"/produits/{product_id}/supprimer?_tab=test-tab", follow_redirects=False
        )
        self.assertEqual(response.status_code, 302)
        conn = get_db()
        product = conn.execute(
            "SELECT is_archived, is_validated FROM products WHERE id=?", (product_id,)
        ).fetchone()
        stock_count = conn.execute(
            "SELECT COUNT(*) count FROM stock WHERE product_id=?", (product_id,)
        ).fetchone()["count"]
        movement_count = conn.execute(
            "SELECT COUNT(*) count FROM stock_movements WHERE product_id=?", (product_id,)
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(product["is_archived"], 1)
        self.assertEqual(product["is_validated"], 0)
        self.assertGreater(stock_count, 0)
        self.assertGreater(movement_count, 0)

    def test_database_backup_is_valid_and_reset_is_blocked(self):
        backup_path = backup_database(force=True)
        self.assertTrue(os.path.exists(backup_path))
        backup_conn = __import__("sqlite3").connect(backup_path)
        self.assertEqual(backup_conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        backup_conn.close()
        with self.assertRaises(RuntimeError):
            init_db(reset=True)

        generations = maintain_database_backups()
        self.assertEqual(len(generations), 3)
        self.assertTrue(any("hourly" in path for path in generations))
        self.assertTrue(any("daily" in path for path in generations))
        self.assertTrue(any("monthly" in path for path in generations))

    def test_founder_admins_survive_schema_updates_and_are_protected(self):
        init_db()
        conn = get_db()
        admins = conn.execute(
            "SELECT email, role, is_active, is_protected, credentials_version FROM users "
            "WHERE lower(email) IN (?, ?) ORDER BY email",
            ("thierno.keita@trustdelivery.com", "daoudabangoura@trustdelivery.com"),
        ).fetchall()
        self.assertEqual(len(admins), 2)
        for admin in admins:
            self.assertEqual(admin["role"], "super_admin")
            self.assertEqual(admin["is_active"], 1)
            self.assertEqual(admin["is_protected"], 1)
            self.assertEqual(admin["credentials_version"], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE users SET role='client', is_active=0 WHERE lower(email)=?",
                ("daoudabangoura@trustdelivery.com",),
            )
        conn.rollback()
        conn.close()

    def test_email_login_is_case_insensitive(self):
        conn = get_db()
        conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, is_active) VALUES (?,?,?,?,1)",
            ("Login Test", "login-test@example.com", generate_password_hash("TestLogin#Secure2026"), "client"),
        )
        conn.commit()
        conn.close()
        client = app.test_client()
        response = client.post(
            "/login",
            data={
                "email": "LOGIN-TEST@EXAMPLE.COM",
                "password": "TestLogin#Secure2026",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard?", response.headers["Location"])
        self.assertIn("_tab=", response.headers["Location"])

    def test_founder_admin_cannot_be_deleted_through_the_ui(self):
        conn = get_db()
        daouda = conn.execute(
            "SELECT id FROM users WHERE lower(email)=?", ("daoudabangoura@trustdelivery.com",)
        ).fetchone()
        conn.close()
        response = self.logged_client(self.admin_id).post(
            f"/utilisateurs/{daouda['id']}/supprimer?_tab=test-tab",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        conn = get_db()
        still_active = conn.execute(
            "SELECT is_active FROM users WHERE id=?", (daouda["id"],)
        ).fetchone()
        conn.close()
        self.assertEqual(still_active["is_active"], 1)

    def test_new_accounts_require_a_strong_password(self):
        response = self.logged_client(self.admin_id).post(
            "/utilisateurs/nouveau?_tab=test-tab",
            data={
                "full_name": "Weak Password",
                "email": "weak-password@example.com",
                "role": "client",
                "password": "weak",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        conn = get_db()
        account = conn.execute(
            "SELECT id FROM users WHERE email=?", ("weak-password@example.com",)
        ).fetchone()
        conn.close()
        self.assertIsNone(account)

    def test_super_admin_can_download_database_backup(self):
        client = self.logged_client(self.admin_id)
        response = client.get("/parametres/sauvegarde-base?_tab=test-tab")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
        self.assertTrue(response.data.startswith(b"SQLite format 3"))
        response.close()

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
        with patch("routes.orders_routes.send_courier_notification", return_value=(True, "envoyée", "https://wa.me/test")) as notify:
            response = client.post(
                f"/commandes/{self.notify_order_id}/affecter?_tab=test-tab",
                data={"livreur_id": self.courier_id},
            )
        self.assertEqual(response.status_code, 302)
        notify.assert_called_once_with(self.notify_order_id, self.courier_id)
        conn = get_db()
        order = conn.execute("SELECT status FROM orders WHERE id=?", (self.notify_order_id,)).fetchone()
        conn.close()
        self.assertEqual(order["status"], "proposee")

    def test_dispatch_can_open_local_whatsapp_and_send_android_push(self):
        conn = get_db()
        order_id = conn.execute(
            "INSERT INTO orders (order_number, client_id, status, zone_id, recipient_name, delivery_address) "
            "VALUES (?,?,?,?,?,?)",
            ("CMD-WHATSAPP-LOCAL", self.first_client_id, "confirmee", self.zone_id, "Client local", "Kaloum"),
        ).lastrowid
        conn.commit()
        conn.close()
        client = self.logged_client(self.admin_id)
        with patch(
            "routes.orders_routes.send_courier_notification",
            return_value=(None, "WhatsApp local", "https://wa.me/224620000099?text=Livraison"),
        ), patch("routes.orders_routes.send_push_to_user", return_value=1) as push:
            response = client.post(
                f"/commandes/{order_id}/affecter?_tab=test-tab",
                data={"livreur_id": self.courier_id, "share_whatsapp": "1"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("https://wa.me/"))
        push.assert_called_once()

    def test_android_device_token_is_scoped_to_logged_in_user(self):
        client = self.logged_client(self.first_client_id)
        token = "fcm-test-token-" + ("x" * 40)
        response = client.post(
            "/notifications/appareil?_tab=test-tab", json={"token": token}
        )
        self.assertEqual(response.status_code, 200)
        conn = get_db()
        row = conn.execute("SELECT user_id, is_active FROM push_device_tokens WHERE token=?", (token,)).fetchone()
        conn.close()
        self.assertEqual(row["user_id"], self.first_client_id)
        self.assertEqual(row["is_active"], 1)

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

    def test_shopify_order_is_fulfilled(self):
        conn = get_db()
        connection_id = conn.execute(
            "INSERT INTO shop_connections (client_id, platform, shop_name, webhook_token, default_zone_id, store_url, api_secret) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.first_client_id, "shopify", "Shopify Test", "shopify-status-token", self.zone_id, "https://shopify.example", "shpat_test"),
        ).lastrowid
        order_id = conn.execute(
            "INSERT INTO orders (order_number, client_id, zone_id, shop_connection_id, external_order_id) VALUES (?,?,?,?,?)",
            ("CMD-SHOPIFY-SYNC", self.first_client_id, self.zone_id, connection_id, "7788"),
        ).lastrowid
        conn.commit()
        conn.close()

        get_response = MagicMock()
        get_response.__enter__.return_value = get_response
        get_response.read.return_value = b'{"fulfillment_orders":[{"id":55,"status":"open"}]}'
        post_response = MagicMock()
        post_response.__enter__.return_value = post_response
        post_response.read.return_value = b'{"fulfillment":{"id":66}}'
        with patch("integrations.urlopen", side_effect=[get_response, post_response]) as request_mock:
            success, message = sync_shop_status(order_id, "livree")
        self.assertTrue(success)
        self.assertIn("fulfilled", message)
        self.assertIn("/fulfillment_orders.json", request_mock.call_args_list[0].args[0].full_url)
        self.assertIn("/fulfillments.json", request_mock.call_args_list[1].args[0].full_url)

    def test_other_platforms_receive_universal_delivered_callback(self):
        conn = get_db()
        orders = []
        for index, platform in enumerate(("prestashop", "facebook", "whatsapp", "custom"), start=1):
            connection_id = conn.execute(
                "INSERT INTO shop_connections (client_id, platform, shop_name, webhook_token, default_zone_id, status_callback_url) "
                "VALUES (?,?,?,?,?,?)",
                (self.first_client_id, platform, f"{platform} Test", f"{platform}-status-token", self.zone_id, "https://automation.example/status"),
            ).lastrowid
            order_id = conn.execute(
                "INSERT INTO orders (order_number, client_id, zone_id, shop_connection_id, external_order_id) VALUES (?,?,?,?,?)",
                (f"CMD-STATUS-{index}", self.first_client_id, self.zone_id, connection_id, f"EXT-{index}"),
            ).lastrowid
            orders.append((platform, order_id))
        conn.commit()
        conn.close()

        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"{}"
        with patch("integrations.urlopen", return_value=response) as request_mock:
            for platform, order_id in orders:
                with self.subTest(platform=platform):
                    success, message = sync_shop_status(order_id, "livree")
                    self.assertTrue(success)
                    self.assertIn(platform, message)
        self.assertEqual(request_mock.call_count, 4)
        for call in request_mock.call_args_list:
            payload = call.args[0].data.decode("utf-8")
            self.assertIn('"status": "delivered"', payload)

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

    def test_partner_api_imports_and_auto_dispatches_order(self):
        response = app.test_client().post(
            "/api/v1/commandes",
            headers={"Authorization": f"Bearer {self.webhook_token}"},
            json={
                "external_order_id": "API-AUTO-1",
                "recipient_name": "Client API",
                "recipient_phone": "224620000006",
                "shipping_address": {"address1": "Kaloum", "city": "Conakry"},
                "items": [{"sku": "TEST-ALPHA", "quantity": 1}],
            },
        )
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertTrue(data["auto_dispatch"]["assigned"])
        conn = get_db()
        order = conn.execute("SELECT status, livreur_id FROM orders WHERE id=?", (data["order_id"],)).fetchone()
        conn.close()
        self.assertEqual(order["status"], "proposee")
        self.assertIsNotNone(order["livreur_id"])

        courier = self.logged_client(order["livreur_id"])
        with patch("routes.orders_routes.send_order_notification", return_value=(True, "envoyée")), patch(
            "routes.orders_routes.sync_shop_status", return_value=(True, "synchronisée")
        ):
            accepted = courier.post(
                f"/commandes/{data['order_id']}/repondre-proposition?_tab=test-tab",
                data={"action": "accept"},
            )
        self.assertEqual(accepted.status_code, 302)
        conn = get_db()
        accepted_status = conn.execute("SELECT status FROM orders WHERE id=?", (data["order_id"],)).fetchone()["status"]
        conn.close()
        self.assertEqual(accepted_status, "affectee")

        invalid = app.test_client().post(
            "/api/v1/commandes",
            headers={"Authorization": "Bearer invalid-token"},
            json={"external_order_id": "NOPE"},
        )
        self.assertEqual(invalid.status_code, 401)

    def test_partner_api_and_gps_are_scoped_to_the_connection(self):
        conn = get_db()
        order_id = conn.execute(
            "INSERT INTO orders (order_number, client_id, status, zone_id, livreur_id, shop_connection_id, external_order_id) "
            "VALUES (?,?,?,?,?,?,?)",
            ("CMD-GPS-API", self.first_client_id, "en_livraison", self.zone_id, self.courier_id, self.connection_id, "GPS-API-1"),
        ).lastrowid
        conn.commit()
        conn.close()

        courier = self.logged_client(self.courier_id)
        gps_response = courier.post(
            f"/commandes/{order_id}/position?_tab=test-tab",
            json={"latitude": 9.6412, "longitude": -13.5784, "accuracy": 8.5},
        )
        self.assertEqual(gps_response.status_code, 200)

        api_response = app.test_client().get(
            "/api/v1/commandes/GPS-API-1",
            headers={"Authorization": f"Bearer {self.webhook_token}"},
        )
        self.assertEqual(api_response.status_code, 200)
        self.assertEqual(api_response.get_json()["gps"]["latitude"], 9.6412)

        wrong_connection = app.test_client().get(
            "/api/v1/commandes/GPS-API-1",
            headers={"Authorization": "Bearer woo-test-token"},
        )
        self.assertEqual(wrong_connection.status_code, 404)

        other_client = self.logged_client(self.second_client_id)
        forbidden = other_client.get(f"/commandes/{order_id}/temps-reel?_tab=test-tab")
        self.assertEqual(forbidden.status_code, 403)

    def test_partner_dashboard_exposes_connection_api(self):
        client = self.logged_client(self.first_client_id)
        response = client.get("/dashboard?_tab=test-tab")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Tableau de bord partenaire", response.data)
        self.assertIn(b"/api/v1/commandes", response.data)

    def test_whatsapp_otp_is_hashed_verified_and_updates_notification_badge(self):
        conn = get_db()
        conn.execute("DELETE FROM whatsapp_verifications WHERE user_id=?", (self.first_client_id,))
        conn.execute("DELETE FROM user_notifications WHERE user_id=?", (self.first_client_id,))
        conn.commit()
        conn.close()
        client = self.logged_client(self.first_client_id)

        with patch("routes.settings_routes.send_whatsapp_otp", return_value={"messages": [{"id": "wamid.test"}]}) as sender:
            response = client.post(
                "/parametres/profil/whatsapp/envoyer?_tab=test-tab",
                data={"whatsapp_phone": "+224 620 00 00 01"},
            )
            self.assertEqual(response.status_code, 302)
            otp = sender.call_args.args[1]
            self.assertRegex(otp, r"^\d{6}$")

            resend = client.post(
                "/parametres/profil/whatsapp/envoyer?_tab=test-tab",
                data={"whatsapp_phone": "224620000001"},
            )
            self.assertEqual(resend.status_code, 302)
            self.assertEqual(sender.call_count, 1)

        conn = get_db()
        stored = conn.execute(
            "SELECT * FROM whatsapp_verifications WHERE user_id=?", (self.first_client_id,)
        ).fetchone()
        conn.close()
        self.assertNotEqual(stored["otp_hash"], otp)
        self.assertTrue(check_password_hash(stored["otp_hash"], otp))
        self.assertNotIn(otp.encode(), client.get("/parametres/profil?_tab=test-tab").data)

        invalid = client.post(
            "/parametres/profil/whatsapp/verifier?_tab=test-tab", data={"otp": "000000"}
        )
        self.assertEqual(invalid.status_code, 302)
        verified = client.post(
            "/parametres/profil/whatsapp/verifier?_tab=test-tab", data={"otp": otp}
        )
        self.assertEqual(verified.status_code, 302)
        profile = client.get("/parametres/profil?_tab=test-tab")
        self.assertIn("WhatsApp lié avec succès".encode(), profile.data)

        badge = client.get("/notifications/non-lues?_tab=test-tab")
        self.assertEqual(badge.status_code, 200)
        self.assertEqual(badge.get_json()["count"], 1)
        conn = get_db()
        user = conn.execute("SELECT whatsapp_phone FROM users WHERE id=?", (self.first_client_id,)).fetchone()
        conn.close()
        self.assertEqual(user["whatsapp_phone"], "224620000001")


if __name__ == "__main__":
    unittest.main()
