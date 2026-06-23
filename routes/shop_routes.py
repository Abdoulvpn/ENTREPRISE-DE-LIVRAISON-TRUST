import secrets

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from auth import roles_required
from db import get_db, log_action
from integrations import send_courier_notification, sync_shop_status, valid_store_url
from routes.orders_routes import create_order_record, deduct_stock_for_items


bp = Blueprint("shops", __name__, url_prefix="/boutiques")
api_bp = Blueprint("partner_api", __name__, url_prefix="/api/v1")

PLATFORMS = {
    "shopify": "Shopify",
    "woocommerce": "WooCommerce",
    "prestashop": "PrestaShop",
    "facebook": "Facebook / Instagram",
    "whatsapp": "WhatsApp",
    "custom": "Autre boutique / API personnalisée",
}


def text(value):
    return str(value or "").strip()


def full_name(data):
    if not isinstance(data, dict):
        return ""
    return text(data.get("name") or " ".join(filter(None, [text(data.get("first_name")), text(data.get("last_name"))])))


def address_text(data):
    if isinstance(data, str):
        return text(data)
    if not isinstance(data, dict):
        return ""
    fields = ("address1", "address_1", "address2", "address_2", "street", "city", "province", "state", "country", "zip", "postcode")
    return ", ".join(dict.fromkeys(text(data.get(field)) for field in fields if text(data.get(field))))


def normalize_items(items):
    normalized = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sku = text(item.get("sku") or item.get("product_reference") or item.get("product_retailer_id") or item.get("reference"))
        try:
            quantity = int(item.get("quantity") or item.get("product_quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if sku and quantity > 0:
            normalized.append({"sku": sku, "quantity": quantity})
    return normalized


def whatsapp_order(payload):
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        message = next(msg for msg in value.get("messages", []) if msg.get("type") == "order")
    except (KeyError, IndexError, StopIteration, TypeError):
        return None
    contact = (value.get("contacts") or [{}])[0]
    return {
        "external_order_id": text(message.get("id")),
        "reference": text(message.get("id")),
        "recipient_name": text(contact.get("profile", {}).get("name")),
        "recipient_phone": text(message.get("from") or contact.get("wa_id")),
        "address": "Adresse à compléter — commande WhatsApp",
        "zone": "",
        "items": normalize_items(message.get("order", {}).get("product_items")),
        "url": "",
    }


def normalize_order_payload(platform, payload):
    """Convertit les principaux formats boutique vers le format interne TrustDelivery."""
    if not isinstance(payload, dict):
        raise ValueError("Le webhook doit contenir un objet JSON.")

    if platform == "whatsapp":
        normalized = whatsapp_order(payload)
        if normalized:
            return normalized

    if platform == "shopify":
        address = payload.get("shipping_address") or payload.get("billing_address") or {}
        customer = payload.get("customer") or {}
        items = payload.get("line_items") or []
        external_id = payload.get("id") or payload.get("admin_graphql_api_id")
        reference = payload.get("name") or payload.get("order_number") or external_id
        recipient = full_name(address) or full_name(customer)
        phone = address.get("phone") or payload.get("phone") or customer.get("phone")
        url = payload.get("order_status_url") or payload.get("admin_url")
    elif platform == "woocommerce":
        address = payload.get("shipping") or payload.get("billing") or {}
        items = payload.get("line_items") or []
        external_id = payload.get("id")
        reference = payload.get("number") or payload.get("order_key") or external_id
        recipient = full_name(address)
        phone = address.get("phone") or (payload.get("billing") or {}).get("phone")
        url = payload.get("permalink")
    elif platform == "prestashop":
        address = payload.get("shipping_address") or payload.get("address_delivery") or payload.get("address") or {}
        associations = payload.get("associations") or {}
        items = payload.get("items") or payload.get("line_items") or associations.get("order_rows") or []
        external_id = payload.get("id") or payload.get("id_order")
        reference = payload.get("reference") or external_id
        recipient = full_name(address) or text(payload.get("customer_name"))
        phone = address.get("phone") or address.get("phone_mobile") or payload.get("phone")
        url = payload.get("url")
    else:
        customer = payload.get("customer") or payload.get("recipient") or {}
        if not isinstance(customer, dict):
            customer = {}
        address = payload.get("shipping_address") or payload.get("shipping") or payload.get("address") or customer.get("address") or {}
        if not isinstance(address, (dict, str)):
            address = {}
        items = payload.get("items") or payload.get("line_items") or payload.get("products") or []
        external_id = payload.get("external_order_id") or payload.get("order_id") or payload.get("id")
        reference = payload.get("reference") or payload.get("order_number") or payload.get("number") or external_id
        recipient = text(payload.get("recipient_name")) or full_name(customer) or full_name(address)
        phone = payload.get("recipient_phone") or payload.get("phone") or customer.get("phone") or (address.get("phone") if isinstance(address, dict) else "")
        url = payload.get("order_url") or payload.get("url")

    zone = ""
    if isinstance(address, dict):
        zone = text(address.get("city") or address.get("province") or address.get("state"))

    return {
        "external_order_id": text(external_id),
        "reference": text(reference),
        "recipient_name": text(recipient),
        "recipient_phone": text(phone),
        "address": address_text(address),
        "zone": zone,
        "items": normalize_items(items),
        "url": text(url),
    }


def can_manage(connection):
    return g.user["role"] in ("super_admin", "moderateur") or connection["client_id"] == g.user["id"]


def dispatch_order_automatically(conn, order_id, zone_id):
    zone = conn.execute("SELECT name FROM zones WHERE id=?", (zone_id,)).fetchone()
    zone_name = zone["name"] if zone else ""
    courier = conn.execute(
        "SELECT u.id, u.full_name, COUNT(o.id) active_orders FROM users u "
        "LEFT JOIN orders o ON o.livreur_id=u.id AND o.status IN ('proposee','affectee','en_livraison') "
        "WHERE u.role='livreur' AND u.is_active=1 GROUP BY u.id "
        "ORDER BY CASE WHEN lower(COALESCE(u.zone,''))=lower(?) THEN 0 ELSE 1 END, active_orders ASC, u.id ASC LIMIT 1",
        (zone_name,),
    ).fetchone()
    if not courier:
        return {"assigned": False, "message": "Aucun livreur actif disponible"}

    items = conn.execute("SELECT product_id, quantity FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    error = deduct_stock_for_items(conn, [(item["product_id"], item["quantity"]) for item in items], created_by=None)
    if error:
        return {"assigned": False, "message": error}

    conn.execute(
        "UPDATE orders SET status='proposee', confirmed_at=datetime('now'), livreur_id=?, assigned_at=datetime('now') WHERE id=?",
        (courier["id"], order_id),
    )
    return {"assigned": True, "courier_id": courier["id"], "courier_name": courier["full_name"]}


@bp.route("/", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur", "client")
def list_connections():
    conn = get_db()
    if request.method == "POST":
        client_id = g.user["id"] if g.user["role"] == "client" else request.form.get("client_id")
        platform = text(request.form.get("platform"))
        shop_name = text(request.form.get("shop_name"))
        zone_id = request.form.get("default_zone_id")
        store_url = text(request.form.get("store_url"))
        api_key = text(request.form.get("api_key"))
        api_secret = text(request.form.get("api_secret"))
        status_callback_url = text(request.form.get("status_callback_url"))
        auto_dispatch = 1 if request.form.get("auto_dispatch") == "on" else 0
        client = conn.execute("SELECT id FROM users WHERE id=? AND role='client' AND is_active=1", (client_id,)).fetchone()
        zone = conn.execute("SELECT id FROM zones WHERE id=?", (zone_id,)).fetchone()
        if not client or platform not in PLATFORMS or not shop_name or not zone:
            flash("Veuillez renseigner une boutique, sa plateforme et sa zone par défaut.", "danger")
        elif not valid_store_url(store_url):
            flash("L’URL de la boutique doit être une adresse HTTPS publique valide.", "danger")
        elif not valid_store_url(status_callback_url):
            flash("L’URL de retour du statut doit être une adresse HTTPS publique valide.", "danger")
        else:
            conn.execute(
                "INSERT INTO shop_connections (client_id, platform, shop_name, webhook_token, default_zone_id, "
                "store_url, api_key, api_secret, status_callback_url, auto_dispatch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (client_id, platform, shop_name, secrets.token_urlsafe(32), zone_id, store_url, api_key, api_secret, status_callback_url, auto_dispatch),
            )
            conn.commit()
            log_action(g.user, "Connexion boutique", f"{shop_name} ({platform})")
            flash("Boutique connectée. Copiez maintenant son URL webhook dans la plateforme.", "success")
            conn.close()
            return redirect(url_for("shops.list_connections"))

    params = []
    query = (
        "SELECT sc.*, u.full_name client_name, z.name zone_name, "
        "(SELECT COUNT(*) FROM orders o WHERE o.shop_connection_id=sc.id) order_count, "
        "(SELECT status FROM shop_sync_events se WHERE se.connection_id=sc.id ORDER BY se.id DESC LIMIT 1) last_sync_status, "
        "(SELECT message FROM shop_sync_events se WHERE se.connection_id=sc.id ORDER BY se.id DESC LIMIT 1) last_sync_message, "
        "(SELECT created_at FROM shop_sync_events se WHERE se.connection_id=sc.id ORDER BY se.id DESC LIMIT 1) last_sync_at "
        "FROM shop_connections sc JOIN users u ON u.id=sc.client_id JOIN zones z ON z.id=sc.default_zone_id"
    )
    if g.user["role"] == "client":
        query += " WHERE sc.client_id=?"
        params.append(g.user["id"])
    query += " ORDER BY sc.created_at DESC"
    connections = conn.execute(query, params).fetchall()
    clients = [] if g.user["role"] == "client" else conn.execute(
        "SELECT id, full_name FROM users WHERE role='client' AND is_active=1 ORDER BY full_name"
    ).fetchall()
    zones = conn.execute("SELECT id, name FROM zones ORDER BY name").fetchall()
    conn.close()
    return render_template("shops.html", connections=connections, clients=clients, zones=zones, platforms=PLATFORMS)


@bp.route("/<int:connection_id>/regenerer", methods=["POST"])
@roles_required("super_admin", "moderateur", "client")
def regenerate_token(connection_id):
    conn = get_db()
    connection = conn.execute("SELECT * FROM shop_connections WHERE id=?", (connection_id,)).fetchone()
    if not connection or not can_manage(connection):
        conn.close()
        flash("Boutique introuvable.", "danger")
        return redirect(url_for("shops.list_connections"))
    conn.execute("UPDATE shop_connections SET webhook_token=? WHERE id=?", (secrets.token_urlsafe(32), connection_id))
    conn.commit()
    conn.close()
    flash("URL webhook régénérée. L’ancienne URL ne fonctionne plus.", "success")
    return redirect(url_for("shops.list_connections"))


@bp.route("/<int:connection_id>/activer", methods=["POST"])
@roles_required("super_admin", "moderateur", "client")
def toggle_connection(connection_id):
    conn = get_db()
    connection = conn.execute("SELECT * FROM shop_connections WHERE id=?", (connection_id,)).fetchone()
    if not connection or not can_manage(connection):
        conn.close()
        flash("Boutique introuvable.", "danger")
        return redirect(url_for("shops.list_connections"))
    conn.execute("UPDATE shop_connections SET is_active=? WHERE id=?", (0 if connection["is_active"] else 1, connection_id))
    conn.commit()
    conn.close()
    flash("État de la synchronisation mis à jour.", "success")
    return redirect(url_for("shops.list_connections"))


@bp.route("/<int:connection_id>/api", methods=["POST"])
@roles_required("super_admin", "moderateur", "client")
def update_api_credentials(connection_id):
    conn = get_db()
    connection = conn.execute("SELECT * FROM shop_connections WHERE id=?", (connection_id,)).fetchone()
    if not connection or not can_manage(connection):
        conn.close()
        flash("Boutique introuvable.", "danger")
        return redirect(url_for("shops.list_connections"))
    store_url = text(request.form.get("store_url"))
    api_key = text(request.form.get("api_key"))
    api_secret = text(request.form.get("api_secret"))
    status_callback_url = text(request.form.get("status_callback_url"))
    auto_dispatch = 1 if request.form.get("auto_dispatch") == "on" else 0
    if store_url and not valid_store_url(store_url):
        conn.close()
        flash("L’URL de la boutique doit être une adresse HTTPS publique valide.", "danger")
        return redirect(url_for("shops.list_connections"))
    if status_callback_url and not valid_store_url(status_callback_url):
        conn.close()
        flash("L’URL de retour du statut doit être une adresse HTTPS publique valide.", "danger")
        return redirect(url_for("shops.list_connections"))
    conn.execute(
        "UPDATE shop_connections SET store_url=?, api_key=CASE WHEN ?='' THEN api_key ELSE ? END, "
        "api_secret=CASE WHEN ?='' THEN api_secret ELSE ? END, status_callback_url=?, auto_dispatch=? WHERE id=?",
        (store_url, api_key, api_key, api_secret, api_secret, status_callback_url, auto_dispatch, connection_id),
    )
    conn.commit()
    conn.close()
    flash("Identifiants API enregistrés. Les statuts livrés pourront être renvoyés vers la boutique.", "success")
    return redirect(url_for("shops.list_connections"))


def record_event(conn, connection_id, external_id, status, message, order_id=None):
    conn.execute(
        "INSERT INTO shop_sync_events (connection_id, external_order_id, status, message, order_id) VALUES (?,?,?,?,?)",
        (connection_id, external_id, status, message[:500], order_id),
    )


@bp.route("/webhook/<token>", methods=["GET", "POST"])
def receive_webhook(token):
    conn = get_db()
    connection = conn.execute(
        "SELECT * FROM shop_connections WHERE webhook_token=? AND is_active=1", (token,)
    ).fetchone()
    if not connection:
        conn.close()
        return jsonify({"ok": False, "error": "Webhook inconnu ou désactivé."}), 404

    if request.method == "GET":
        challenge = request.args.get("hub.challenge")
        verify_token = request.args.get("hub.verify_token")
        conn.close()
        if challenge and secrets.compare_digest(verify_token or "", token):
            return challenge, 200, {"Content-Type": "text/plain; charset=utf-8"}
        return jsonify({"ok": True, "message": "Webhook TrustDelivery actif."})

    payload = request.get_json(silent=True)
    if isinstance(payload, dict) and payload.get("webhook_id") and not payload.get("id"):
        conn.close()
        return jsonify({"ok": True, "ping": True})
    try:
        order = normalize_order_payload(connection["platform"], payload)
        external_id = order["external_order_id"]
        if not external_id:
            raise ValueError("Identifiant de commande boutique manquant.")
        if not order["items"]:
            raise ValueError("Aucun article avec SKU et quantité valide.")

        existing = conn.execute(
            "SELECT id, order_number FROM orders WHERE shop_connection_id=? AND external_order_id=?",
            (connection["id"], external_id),
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({"ok": True, "duplicate": True, "order_id": existing["id"], "order_number": existing["order_number"]})

        quantities = {}
        for item in order["items"]:
            quantities[item["sku"]] = quantities.get(item["sku"], 0) + item["quantity"]
        items = []
        for sku, quantity in quantities.items():
            product = conn.execute(
                "SELECT id FROM products WHERE lower(sku)=lower(?) AND supplier_client_id=? AND is_validated=1",
                (sku, connection["client_id"]),
            ).fetchone()
            if not product:
                raise ValueError(f"SKU absent du stock de ce client : {sku}")
            items.append((product["id"], quantity))

        zone = None
        if order["zone"]:
            zone = conn.execute("SELECT id FROM zones WHERE lower(name)=lower(?)", (order["zone"],)).fetchone()
        zone_id = zone["id"] if zone else connection["default_zone_id"]
        recipient_name = order["recipient_name"] or "Client boutique"
        recipient_phone = order["recipient_phone"] or "À compléter"
        address = order["address"] or "Adresse à compléter"
        order_id, order_number = create_order_record(
            conn,
            connection["client_id"],
            zone_id,
            address,
            recipient_name,
            recipient_phone,
            items,
            source="webhook",
            shop_platform=connection["platform"],
            shop_name=connection["shop_name"],
            shop_order_ref=order["reference"],
            shop_order_url=order["url"],
        )
        conn.execute(
            "UPDATE orders SET shop_connection_id=?, external_order_id=? WHERE id=?",
            (connection["id"], external_id, order_id),
        )
        dispatch = {"assigned": False}
        if connection["auto_dispatch"]:
            dispatch = dispatch_order_automatically(conn, order_id, zone_id)
        event_message = f"Commande {order_number} créée"
        if dispatch.get("assigned"):
            event_message += f" et affectée à {dispatch['courier_name']}"
        elif connection["auto_dispatch"]:
            event_message += f" ; dispatch en attente ({dispatch.get('message', 'indisponible')})"
        record_event(conn, connection["id"], external_id, "success", event_message, order_id)
        conn.commit()
        conn.close()
        if dispatch.get("assigned"):
            send_courier_notification(order_id, dispatch["courier_id"])
        return jsonify({
            "ok": True,
            "order_id": order_id,
            "order_number": order_number,
            "auto_dispatch": dispatch,
        }), 201
    except (TypeError, ValueError) as exc:
        external_id = text(payload.get("id") if isinstance(payload, dict) else "")
        record_event(conn, connection["id"], external_id, "error", str(exc))
        conn.commit()
        conn.close()
        return jsonify({"ok": False, "error": str(exc)}), 422


def bearer_token():
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


@api_bp.route("/commandes", methods=["POST"])
def api_create_order():
    token = bearer_token()
    if not token:
        return jsonify({"ok": False, "error": "Jeton Bearer manquant."}), 401
    conn = get_db()
    exists = conn.execute(
        "SELECT id FROM shop_connections WHERE webhook_token=? AND is_active=1", (token,)
    ).fetchone()
    conn.close()
    if not exists:
        return jsonify({"ok": False, "error": "Jeton API invalide."}), 401
    return receive_webhook(token)


@api_bp.route("/commandes/<external_id>", methods=["GET"])
def api_order_status(external_id):
    token = bearer_token()
    conn = get_db()
    connection = conn.execute(
        "SELECT id FROM shop_connections WHERE webhook_token=? AND is_active=1", (token,)
    ).fetchone() if token else None
    if not connection:
        conn.close()
        return jsonify({"ok": False, "error": "Jeton API invalide."}), 401
    order = conn.execute(
        "SELECT id, order_number, status, livreur_id, delivered_at, created_at FROM orders "
        "WHERE shop_connection_id=? AND external_order_id=?",
        (connection["id"], external_id),
    ).fetchone()
    if not order:
        conn.close()
        return jsonify({"ok": False, "error": "Commande introuvable."}), 404
    location = conn.execute(
        "SELECT latitude, longitude, accuracy, recorded_at FROM courier_locations "
        "WHERE order_id=? ORDER BY recorded_at DESC, id DESC LIMIT 1",
        (order["id"],),
    ).fetchone()
    conn.close()
    return jsonify({
        "ok": True,
        "order": dict(order),
        "gps": dict(location) if location else None,
    })
