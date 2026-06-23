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


def get_json(url, headers=None, timeout=8):
    request = Request(url, headers={"Accept": "application/json", **(headers or {})}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def record_sync_event(conn, row, order_id, success, message):
    conn.execute(
        "INSERT INTO shop_sync_events (connection_id, external_order_id, status, message, order_id) VALUES (?,?,?,?,?)",
        (row["shop_connection_id"], row["external_order_id"], "success" if success else "error", message[:500], order_id),
    )


def sync_woocommerce(row, status):
    woo_status = {
        "confirmee": "processing",
        "affectee": "processing",
        "en_livraison": "processing",
        "livree": "completed",
        "annulee": "cancelled",
        "retournee": "cancelled",
    }[status]
    endpoint = f"{row['store_url'].rstrip('/')}/wp-json/wc/v3/orders/{quote(str(row['external_order_id']))}"
    credentials = base64.b64encode(f"{row['api_key']}:{row['api_secret']}".encode()).decode()
    post_json(endpoint, {"status": woo_status}, {"Authorization": f"Basic {credentials}"}, method="PUT")
    return f"Statut WooCommerce mis à jour : {woo_status}"


def sync_shopify(row):
    api_version = os.environ.get("SHOPIFY_API_VERSION", "2026-04")
    base_url = row["store_url"].rstrip("/")
    order_id = quote(str(row["external_order_id"]))
    headers = {"X-Shopify-Access-Token": row["api_secret"]}
    data = get_json(f"{base_url}/admin/api/{api_version}/orders/{order_id}/fulfillment_orders.json", headers)
    fulfillment_orders = [
        {"fulfillment_order_id": item["id"]}
        for item in data.get("fulfillment_orders", [])
        if item.get("id") and item.get("status") in ("open", "in_progress")
    ]
    if not fulfillment_orders:
        return "Commande Shopify déjà traitée ou sans expédition ouverte"
    post_json(
        f"{base_url}/admin/api/{api_version}/fulfillments.json",
        {"fulfillment": {"line_items_by_fulfillment_order": fulfillment_orders, "notify_customer": True}},
        headers,
    )
    return "Commande Shopify marquée comme fulfilled"


def sync_universal_callback(row, order_id, status):
    public_status = {
        "confirmee": "confirmed",
        "affectee": "assigned",
        "en_livraison": "out_for_delivery",
        "livree": "delivered",
        "annulee": "cancelled",
        "retournee": "returned",
    }[status]
    token = row["api_secret"] or row["webhook_token"]
    post_json(
        row["status_callback_url"],
        {
            "event": f"order.{public_status}",
            "status": public_status,
            "platform": row["platform"],
            "external_order_id": row["external_order_id"],
            "trustdelivery_order_id": order_id,
        },
        {"Authorization": f"Bearer {token}", "X-TrustDelivery-Event": f"order.{public_status}"},
    )
    return f"Statut {public_status} envoyé vers {row['platform']}"


def log_notification(order_id, channel, recipient, status, message):
    conn = get_db()
    conn.execute(
        "INSERT INTO notification_log (order_id, channel, recipient, status, message) VALUES (?,?,?,?,?)",
        (order_id, channel, recipient, status, message[:500]),
    )
    conn.commit()
    conn.close()


def whatsapp_link(phone, message):
    recipient = re.sub(r"\D", "", phone or "")
    if not recipient:
        return ""
    return f"https://wa.me/{recipient}?text={quote(message)}"


def send_whatsapp_otp(phone, otp):
    """Envoie un OTP avec un modèle d'authentification WhatsApp approuvé par Meta."""
    recipient = re.sub(r"\D", "", phone or "")
    token = os.environ.get("META_WHATSAPP_TOKEN", "").strip()
    phone_number_id = os.environ.get("META_WHATSAPP_PHONE_NUMBER_ID", "").strip()
    template_name = os.environ.get("META_WHATSAPP_OTP_TEMPLATE", "trustdelivery_otp").strip()
    template_language = os.environ.get("META_WHATSAPP_OTP_LANGUAGE", "fr").strip()
    if not token or not phone_number_id:
        raise ValueError("WhatsApp Business Cloud API n'est pas configurée.")
    graph_version = os.environ.get("META_GRAPH_API_VERSION", "v23.0")
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": template_language},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": otp}]},
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": otp}],
                },
            ],
        },
    }
    return post_json(
        f"https://graph.facebook.com/{graph_version}/{quote(phone_number_id)}/messages",
        payload,
        {"Authorization": f"Bearer {token}"},
        timeout=12,
    )


def send_courier_notification(order_id, courier_id):
    conn = get_db()
    row = conn.execute(
        "SELECT o.order_number, o.delivery_address, o.recipient_name, u.full_name, u.whatsapp_phone "
        "FROM orders o JOIN users u ON u.id=? WHERE o.id=?",
        (courier_id, order_id),
    ).fetchone()
    conn.close()
    if not row or not row["whatsapp_phone"]:
        return False, "Le livreur n’a pas encore lié son numéro WhatsApp.", ""

    recipient = re.sub(r"\D", "", row["whatsapp_phone"])
    message = (
        f"Bonjour {row['full_name']}, une livraison {row['order_number']} vous est proposée. "
        f"Destinataire : {row['recipient_name'] or 'à confirmer'}. Adresse : {row['delivery_address'] or 'à confirmer'}. "
        "Ouvrez TrustDelivery pour accepter ou refuser."
    )
    direct_link = whatsapp_link(recipient, message)
    webhook_url = os.environ.get("NOTIFICATION_WEBHOOK_URL", "").strip()
    whatsapp_token = os.environ.get("META_WHATSAPP_TOKEN", "").strip()
    phone_number_id = os.environ.get("META_WHATSAPP_PHONE_NUMBER_ID", "").strip()
    try:
        if webhook_url:
            post_json(webhook_url, {"channel": "whatsapp", "to": recipient, "message": message, "order_id": order_id})
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
            log_notification(order_id, "whatsapp_link", recipient, "ready", "Lien WhatsApp direct disponible")
            return None, "Ouvrez WhatsApp pour notifier le livreur.", direct_link
        log_notification(order_id, channel, recipient, "success", message)
        return True, "Notification WhatsApp envoyée au livreur.", direct_link
    except (HTTPError, URLError, OSError, ValueError) as exc:
        log_notification(order_id, "whatsapp", recipient, "error", str(exc))
        return False, f"Échec WhatsApp : {exc}", direct_link


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
    supported_statuses = ("confirmee", "affectee", "en_livraison", "livree", "annulee", "retournee")
    if status not in supported_statuses:
        conn.close()
        return None, "Retour de statut non requis."
    try:
        if row["platform"] == "woocommerce" and row["store_url"] and row["api_key"] and row["api_secret"]:
            if not valid_store_url(row["store_url"]):
                raise ValueError("URL WooCommerce non sécurisée ou invalide")
            message = sync_woocommerce(row, status)
        elif row["platform"] == "shopify" and status == "livree" and row["store_url"] and row["api_secret"]:
            if not valid_store_url(row["store_url"]):
                raise ValueError("URL Shopify non sécurisée ou invalide")
            message = sync_shopify(row)
        elif row["status_callback_url"]:
            if not valid_store_url(row["status_callback_url"]):
                raise ValueError("Callback de statut non sécurisé ou invalide")
            message = sync_universal_callback(row, order_id, status)
        else:
            if status != "livree":
                conn.close()
                return None, "Callback temps réel non configuré."
            raise ValueError(f"Retour de statut {row['platform']} non configuré")
        success = True
    except (HTTPError, URLError, OSError, ValueError) as exc:
        success, message = False, f"Échec {row['platform']} : {exc}"

    record_sync_event(conn, row, order_id, success, message)
    conn.commit()
    conn.close()
    return success, message
