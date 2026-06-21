import base64
import ipaddress
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from db import get_db


def valid_store_url(value):
    if not value:
        return True
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        try:
            address = ipaddress.ip_address(host)
            return address.is_global
        except ValueError:
            return True
    except ValueError:
        return False


def post_json(url, payload, headers=None, method="POST", timeout=8):
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def log_notification(order_id, channel, recipient, status, message):
    conn = get_db()
    conn.execute(
        "INSERT INTO notification_log (order_id, channel, recipient, status, message) VALUES (?,?,?,?,?)",
        (order_id, channel, recipient, status, message[:500]),
    )
    conn.commit()
    conn.close()


def send_order_notification(order_id, event, livreur_name=""):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if not order:
        return False, "Commande introuvable."

    messages = {
        "assigned": f"Votre commande {order['order_number']} a été confiée à {livreur_name or 'un livreur'}. Livraison en préparation.",
        "delivered": f"Votre commande {order['order_number']} a été livrée. Merci pour votre confiance.",
    }
    message = messages.get(event, f"Mise à jour de votre commande {order['order_number']}.")
    recipient = re.sub(r"\D", "", order["recipient_phone"] or "")
    if not recipient:
        log_notification(order_id, "none", "", "error", "Numéro du destinataire manquant")
        return False, "Numéro du destinataire manquant."

    webhook_url = os.environ.get("NOTIFICATION_WEBHOOK_URL", "").strip()
    whatsapp_token = os.environ.get("META_WHATSAPP_TOKEN", "").strip()
    phone_number_id = os.environ.get("META_WHATSAPP_PHONE_NUMBER_ID", "").strip()
    try:
        if webhook_url:
            post_json(webhook_url, {"channel": "sms_or_whatsapp", "to": recipient, "message": message, "order_id": order_id})
            channel = "webhook"
        elif whatsapp_token and phone_number_id:
            graph_version = os.environ.get("META_GRAPH_API_VERSION", "v23.0")
            post_json(
                f"https://graph.facebook.com/{graph_version}/{quote(phone_number_id)}/messages",
                {"messaging_product": "whatsapp", "to": recipient, "type": "text", "text": {"body": message}},
                {"Authorization": f"Bearer {whatsapp_token}"},
            )
            channel = "whatsapp"
        else:
            log_notification(order_id, "none", recipient, "skipped", "Service SMS/WhatsApp non configuré")
            return False, "Service SMS/WhatsApp non configuré."
        log_notification(order_id, channel, recipient, "success", message)
        return True, "Notification envoyée."
    except (HTTPError, URLError, OSError, ValueError) as exc:
        log_notification(order_id, "notification", recipient, "error", str(exc))
        return False, f"Échec de la notification : {exc}"


def sync_shop_status(order_id, status):
    conn = get_db()
    row = conn.execute(
        "SELECT o.external_order_id, o.shop_connection_id, sc.* FROM orders o "
        "JOIN shop_connections sc ON sc.id=o.shop_connection_id WHERE o.id=?",
        (order_id,),
    ).fetchone()
    if not row:
        conn.close()
        return None, "Commande sans boutique connectée."
    if row["platform"] != "woocommerce" or status != "livree":
        conn.close()
        return None, "Retour de statut non requis pour cette plateforme."
    if not row["store_url"] or not row["api_key"] or not row["api_secret"]:
        message = "Identifiants API WooCommerce manquants"
        conn.execute(
            "INSERT INTO shop_sync_events (connection_id, external_order_id, status, message, order_id) VALUES (?,?,?,?,?)",
            (row["shop_connection_id"], row["external_order_id"], "error", message, order_id),
        )
        conn.commit()
        conn.close()
        return False, message
    if not valid_store_url(row["store_url"]):
        message = "URL WooCommerce non sécurisée ou invalide"
        conn.execute(
            "INSERT INTO shop_sync_events (connection_id, external_order_id, status, message, order_id) VALUES (?,?,?,?,?)",
            (row["shop_connection_id"], row["external_order_id"], "error", message, order_id),
        )
        conn.commit()
        conn.close()
        return False, message

    endpoint = f"{row['store_url'].rstrip('/')}/wp-json/wc/v3/orders/{quote(str(row['external_order_id']))}"
    credentials = base64.b64encode(f"{row['api_key']}:{row['api_secret']}".encode()).decode()
    try:
        post_json(endpoint, {"status": "completed"}, {"Authorization": f"Basic {credentials}"}, method="PUT")
        success, message = True, "Statut WooCommerce mis à jour : completed"
    except (HTTPError, URLError, OSError, ValueError) as exc:
        success, message = False, f"Échec WooCommerce : {exc}"

    conn.execute(
        "INSERT INTO shop_sync_events (connection_id, external_order_id, status, message, order_id) VALUES (?,?,?,?,?)",
        (row["shop_connection_id"], row["external_order_id"], "success" if success else "error", message, order_id),
    )
    conn.commit()
    conn.close()
    return success, message
