import csv
import io
import math
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, jsonify
from datetime import datetime
from db import get_db, log_action, create_user_notification
from auth import roles_required, login_required
from integrations import send_courier_notification, send_order_notification, sync_shop_status, whatsapp_link, send_push_to_user

bp = Blueprint("orders", __name__, url_prefix="/commandes")


def generate_order_number(conn):
    year = datetime.now().year
    count = conn.execute("SELECT COUNT(*) c FROM orders WHERE strftime('%Y', created_at) = ?", (str(year),)).fetchone()["c"]
    return f"CMD-{year}-{count + 1:05d}"


def generate_invoice_number(conn):
    year = datetime.now().year
    count = conn.execute("SELECT COUNT(*) c FROM invoices WHERE strftime('%Y', created_at) = ?", (str(year),)).fetchone()["c"]
    return f"FAC-{year}-{count + 1:05d}"


def restock_items(conn, order_id, note):
    items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    warehouse = conn.execute("SELECT id FROM warehouses LIMIT 1").fetchone()
    for item in items:
        if warehouse:
            conn.execute(
                "INSERT INTO stock (product_id, warehouse_id, quantity, alert_threshold) VALUES (?,?,0,5) "
                "ON CONFLICT(product_id, warehouse_id) DO NOTHING",
                (item["product_id"], warehouse["id"]),
            )
            conn.execute(
                "UPDATE stock SET quantity = quantity + ? WHERE product_id=? AND warehouse_id=?",
                (item["quantity"], item["product_id"], warehouse["id"]),
            )
            conn.execute(
                "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, quantity, note, created_by) VALUES (?,?,?,?,?,?)",
                (item["product_id"], warehouse["id"], "entree", item["quantity"], note, g.user["id"]),
            )


def deduct_stock_for_items(conn, items, created_by=None):
    """Vérifie la disponibilité puis décrémente le stock (toutes entrepôts confondus). Retourne un message d'erreur ou None."""
    for product_id, quantity in items:
        total_available = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) q FROM stock WHERE product_id=?", (product_id,)
        ).fetchone()["q"]
        if total_available < quantity:
            product = conn.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
            return f"Stock insuffisant pour « {product['name']} » ({total_available} disponible(s), {quantity} demandé(s))."

    for product_id, quantity in items:
        remaining = quantity
        stock_rows = conn.execute(
            "SELECT * FROM stock WHERE product_id=? AND quantity > 0 ORDER BY quantity DESC", (product_id,)
        ).fetchall()
        for row in stock_rows:
            if remaining <= 0:
                break
            take = min(remaining, row["quantity"])
            conn.execute("UPDATE stock SET quantity = quantity - ? WHERE id=?", (take, row["id"]))
            conn.execute(
                "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, quantity, note, created_by) VALUES (?,?,?,?,?,?)",
                (
                    product_id,
                    row["warehouse_id"],
                    "sortie",
                    take,
                    "Confirmation de commande",
                    created_by if created_by is not None else (g.user["id"] if g.get("user") else None),
                ),
            )
            remaining -= take
    return None


SHOP_PLATFORMS = {
    "": "",
    "shopify": "Shopify",
    "woocommerce": "WooCommerce",
    "prestashop": "PrestaShop",
    "facebook": "Facebook / Instagram",
    "whatsapp": "WhatsApp",
    "other": "Autre boutique",
}


def clean_optional(value):
    return (value or "").strip()


def create_order_record(
    conn,
    client_id,
    zone_id,
    address,
    recipient_name,
    recipient_phone,
    items,
    source="manual",
    shop_platform="",
    shop_name="",
    shop_order_ref="",
    shop_order_url="",
):
    total_amount = 0
    order_items_data = []
    for item in items:
        pid, qty = item[:2]
        product = conn.execute(
            "SELECT * FROM products WHERE id=? AND supplier_client_id=? AND is_validated=1 AND is_archived=0",
            (pid, client_id),
        ).fetchone()
        if not product:
            raise ValueError("Un article sélectionné n'appartient pas au stock de ce client.")
        unit_price = float(item[2]) if len(item) > 2 else float(product["price"])
        if not math.isfinite(unit_price) or unit_price < 0:
            raise ValueError("Le prix du produit doit être un nombre positif ou nul.")
        total_amount += unit_price * qty
        order_items_data.append((pid, qty, unit_price))

    order_number = generate_order_number(conn)
    cur = conn.execute(
        "INSERT INTO orders (order_number, client_id, status, zone_id, recipient_name, recipient_phone, "
        "delivery_address, total_amount, delivery_fee, source, shop_platform, shop_name, shop_order_ref, shop_order_url) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            order_number,
            client_id,
            "en_attente",
            zone_id,
            recipient_name,
            recipient_phone,
            address,
            total_amount,
            0,
            source,
            clean_optional(shop_platform),
            clean_optional(shop_name),
            clean_optional(shop_order_ref),
            clean_optional(shop_order_url),
        ),
    )
    order_id = cur.lastrowid
    for pid, qty, price in order_items_data:
        conn.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?,?,?,?)",
            (order_id, pid, qty, price),
        )
    return order_id, order_number


@bp.route("/")
@login_required
def list_orders():
    conn = get_db()
    status_filter = request.args.get("status", "")
    role = g.user["role"]

    query = (
        "SELECT o.*, u.full_name as client_name, l.full_name as livreur_name, z.name as zone_name "
        "FROM orders o JOIN users u ON u.id=o.client_id "
        "LEFT JOIN users l ON l.id=o.livreur_id LEFT JOIN zones z ON z.id=o.zone_id "
    )
    conditions, params = [], []

    if role == "client":
        conditions.append("o.client_id = ?")
        params.append(g.user["id"])
    elif role == "livreur":
        conditions.append("o.livreur_id = ?")
        params.append(g.user["id"])

    if status_filter:
        conditions.append("o.status = ?")
        params.append(status_filter)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY o.created_at DESC"

    orders = conn.execute(query, params).fetchall()
    statuses = conn.execute("SELECT * FROM order_status_config ORDER BY sort_order").fetchall()
    conn.close()
    return render_template("orders_list.html", orders=orders, statuses=statuses, status_filter=status_filter)


@bp.route("/confirmation")
@roles_required("super_admin", "moderateur", "agent_confirmation", "client")
def confirmation_panel():
    conn = get_db()
    params = []
    where = "WHERE o.status='en_attente'"
    if g.user["role"] == "client":
        where += " AND o.client_id=?"
        params.append(g.user["id"])
    orders = conn.execute(
        "SELECT o.*, u.full_name client_name, z.name zone_name "
        "FROM orders o JOIN users u ON u.id=o.client_id "
        "LEFT JOIN zones z ON z.id=o.zone_id " + where + " ORDER BY o.created_at ASC",
        params,
    ).fetchall()
    conn.close()
    return render_template("orders_confirmation.html", orders=orders)


@bp.route("/livraison")
@roles_required("super_admin", "moderateur", "agent_confirmation")
def delivery_panel():
    conn = get_db()
    orders = conn.execute(
        "SELECT o.*, u.full_name client_name, z.name zone_name "
        "FROM orders o JOIN users u ON u.id=o.client_id "
        "LEFT JOIN zones z ON z.id=o.zone_id "
        "WHERE o.status='confirmee' ORDER BY o.confirmed_at ASC, o.created_at ASC"
    ).fetchall()
    livreurs = conn.execute(
        "SELECT u.*, COUNT(o.id) active_orders FROM users u "
        "LEFT JOIN orders o ON o.livreur_id=u.id "
        "AND o.status IN ('proposee','affectee','en_livraison') "
        "WHERE u.role='livreur' AND u.is_active=1 "
        "GROUP BY u.id ORDER BY active_orders ASC, u.full_name ASC"
    ).fetchall()
    conn.close()
    return render_template("orders_delivery.html", orders=orders, livreurs=livreurs)


@bp.route("/nouvelle", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur", "agent_confirmation", "client")
def create_order():
    conn = get_db()
    if g.user["role"] == "client":
        products = conn.execute(
            "SELECT * FROM products WHERE is_validated=1 AND is_archived=0 AND supplier_client_id=? ORDER BY name",
            (g.user["id"],),
        ).fetchall()
    else:
        products = conn.execute("SELECT * FROM products WHERE is_validated=1 AND is_archived=0 ORDER BY name").fetchall()
    zones = conn.execute("SELECT * FROM zones ORDER BY name").fetchall()
    clients = []
    if g.user["role"] in ("super_admin", "moderateur", "agent_confirmation"):
        clients = conn.execute("SELECT * FROM users WHERE role='client' AND is_active=1 ORDER BY full_name").fetchall()

    if request.method == "POST":
        client_id = g.user["id"] if g.user["role"] == "client" else request.form.get("client_id")
        zone_id = request.form.get("zone_id")
        recipient_name = request.form.get("recipient_name", "").strip()
        recipient_phone = request.form.get("recipient_phone", "").strip()
        address = request.form.get("delivery_address", "").strip()
        shop_platform = request.form.get("shop_platform", "").strip()
        shop_name = request.form.get("shop_name", "").strip()
        shop_order_ref = request.form.get("shop_order_ref", "").strip()
        shop_order_url = request.form.get("shop_order_url", "").strip()
        product_ids = request.form.getlist("product_id")
        quantities = request.form.getlist("quantity")
        unit_prices = request.form.getlist("unit_price")

        items = []
        invalid_item = False
        submitted_prices = bool(unit_prices)
        prices = unit_prices if submitted_prices else [None] * len(product_ids)
        for pid, qty, unit_price in zip(product_ids, quantities, prices):
            try:
                parsed_quantity = int(qty)
                parsed_price = float(unit_price) if unit_price is not None else None
                if not pid or parsed_quantity <= 0 or (
                    parsed_price is not None and (parsed_price < 0 or not math.isfinite(parsed_price))
                ):
                    raise ValueError
                items.append(
                    (int(pid), parsed_quantity, parsed_price)
                    if parsed_price is not None else (int(pid), parsed_quantity)
                )
            except (TypeError, ValueError):
                invalid_item = True

        error = None
        if invalid_item or (submitted_prices and len(product_ids) != len(unit_prices)):
            error = "Chaque article doit avoir un produit, un prix valide et une quantité positive."
        if not client_id or not zone_id or not recipient_name or not recipient_phone or not address or not items:
            error = error or "Veuillez renseigner le client, le destinataire, son telephone, la zone, l'adresse et au moins un article."

        client = conn.execute(
            "SELECT id FROM users WHERE id=? AND role='client' AND is_active=1", (client_id,)
        ).fetchone() if client_id else None
        if error is None and not client:
            error = "Le client sélectionné est invalide ou inactif."
        if error is None:
            owned_count = conn.execute(
                f"SELECT COUNT(*) c FROM products WHERE is_validated=1 AND is_archived=0 AND supplier_client_id=? "
                f"AND id IN ({','.join('?' for _ in items)})",
                [client_id, *[item[0] for item in items]],
            ).fetchone()["c"]
            if owned_count != len({item[0] for item in items}):
                error = "Tous les articles doivent appartenir au stock du client sélectionné."

        if error is None:
            order_id, order_number = create_order_record(
                conn,
                client_id,
                zone_id,
                address,
                recipient_name,
                recipient_phone,
                items,
                shop_platform=shop_platform,
                shop_name=shop_name,
                shop_order_ref=shop_order_ref,
                shop_order_url=shop_order_url,
            )
            conn.commit()
            log_action(g.user, "Création commande", f"{order_number}")
            conn.close()
            flash(f"Commande {order_number} créée avec succès. Elle est en attente de confirmation.", "success")
            return redirect(url_for("orders.order_detail", order_id=order_id))

        flash(error, "danger")

    conn.close()
    return render_template("order_form.html", products=products, zones=zones, clients=clients, shop_platforms=SHOP_PLATFORMS)


@bp.route("/sheet", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur", "agent_confirmation", "client")
def import_sheet():
    conn = get_db()
    clients = []
    if g.user["role"] in ("super_admin", "moderateur", "agent_confirmation"):
        clients = conn.execute("SELECT * FROM users WHERE role='client' AND is_active=1 ORDER BY full_name").fetchall()

    if request.method == "POST":
        client_id = g.user["id"] if g.user["role"] == "client" else request.form.get("client_id")
        csv_text = request.form.get("csv_data", "").strip()
        file = request.files.get("csv_file")
        if file and file.filename:
            csv_text = file.read().decode("utf-8-sig")

        if not client_id or not csv_text:
            conn.close()
            flash("Veuillez choisir un client et fournir un fichier CSV ou des lignes collees depuis le sheet.", "danger")
            return redirect(url_for("orders.import_sheet"))

        client = conn.execute(
            "SELECT id FROM users WHERE id=? AND role='client' AND is_active=1", (client_id,)
        ).fetchone()
        if not client:
            conn.close()
            flash("Le client sélectionné est invalide ou inactif.", "danger")
            return redirect(url_for("orders.import_sheet"))

        rows = csv.DictReader(io.StringIO(csv_text))
        required = {"destinataire", "telephone", "adresse", "zone", "sku", "quantite"}
        headers = {h.strip().lower() for h in (rows.fieldnames or [])}
        if not required.issubset(headers):
            conn.close()
            flash("Colonnes requises: destinataire, telephone, adresse, zone, sku, quantite.", "danger")
            return redirect(url_for("orders.import_sheet"))

        grouped = {}
        errors = []
        for line_number, row in enumerate(rows, start=2):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            shop_platform = row.get("plateforme", "") or row.get("boutique_type", "") or row.get("source_boutique", "")
            shop_name = row.get("boutique", "") or row.get("nom_boutique", "") or row.get("shop", "")
            shop_order_ref = (
                row.get("reference_boutique", "")
                or row.get("ref_boutique", "")
                or row.get("commande_boutique", "")
                or row.get("shopify_order", "")
            )
            shop_order_url = row.get("lien_boutique", "") or row.get("url_boutique", "") or row.get("shopify_url", "")
            key = (
                row.get("destinataire", ""),
                row.get("telephone", ""),
                row.get("adresse", ""),
                row.get("zone", ""),
                shop_platform,
                shop_name,
                shop_order_ref,
                shop_order_url,
            )
            sku = row.get("sku", "")
            try:
                quantity = int(row.get("quantite", "0"))
            except ValueError:
                quantity = 0
            if not all(key[:4]) or not sku or quantity <= 0:
                errors.append(f"Ligne {line_number}: donnees incompletes.")
                continue

            zone = conn.execute("SELECT id FROM zones WHERE lower(name)=lower(?)", (key[3],)).fetchone()
            product = conn.execute(
                "SELECT id FROM products WHERE lower(sku)=lower(?) AND is_validated=1 AND is_archived=0 AND supplier_client_id=?",
                (sku, client_id),
            ).fetchone()
            if not zone:
                errors.append(f"Ligne {line_number}: zone inconnue ({key[3]}).")
                continue
            if not product:
                errors.append(f"Ligne {line_number}: produit SKU absent du stock de ce client ({sku}).")
                continue

            grouped.setdefault(key, {"zone_id": zone["id"], "items": []})["items"].append((product["id"], quantity))

        created = []
        if not errors:
            for (
                recipient_name,
                recipient_phone,
                address,
                _zone_name,
                shop_platform,
                shop_name,
                shop_order_ref,
                shop_order_url,
            ), data in grouped.items():
                order_id, order_number = create_order_record(
                    conn,
                    client_id,
                    data["zone_id"],
                    address,
                    recipient_name,
                    recipient_phone,
                    data["items"],
                    source="sheet",
                    shop_platform=shop_platform,
                    shop_name=shop_name,
                    shop_order_ref=shop_order_ref,
                    shop_order_url=shop_order_url,
                )
                created.append((order_id, order_number))
            conn.commit()

        if errors:
            conn.rollback()
            conn.close()
            flash("Import annule: " + " ".join(errors[:4]), "danger")
            return redirect(url_for("orders.import_sheet"))

        for _order_id, order_number in created:
            log_action(g.user, "Creation commande sheet", order_number)

        conn.close()
        flash(f"{len(created)} commande(s) creee(s) depuis le sheet boutique.", "success")
        if len(created) == 1:
            return redirect(url_for("orders.order_detail", order_id=created[0][0]))
        return redirect(url_for("orders.list_orders"))

    conn.close()
    return render_template("order_sheet_import.html", clients=clients, shop_platforms=SHOP_PLATFORMS)


@bp.route("/<int:order_id>")
@login_required
def order_detail(order_id):
    conn = get_db()
    order = conn.execute(
        "SELECT o.*, u.full_name as client_name, u.email as client_email, u.phone as client_phone, "
        "l.full_name as livreur_name, l.whatsapp_phone as livreur_whatsapp, c.full_name as confirmed_by_name, z.name as zone_name "
        "FROM orders o JOIN users u ON u.id=o.client_id "
        "LEFT JOIN users l ON l.id=o.livreur_id LEFT JOIN users c ON c.id=o.confirmed_by "
        "LEFT JOIN zones z ON z.id=o.zone_id WHERE o.id=?",
        (order_id,),
    ).fetchone()

    if not order:
        conn.close()
        flash("Commande introuvable.", "danger")
        return redirect(url_for("orders.list_orders"))

    if g.user["role"] == "client" and order["client_id"] != g.user["id"]:
        conn.close()
        flash("Vous n'avez pas accès à cette commande.", "danger")
        return redirect(url_for("orders.list_orders"))
    if g.user["role"] == "livreur" and order["livreur_id"] != g.user["id"]:
        conn.close()
        flash("Cette commande ne vous est pas assignée.", "danger")
        return redirect(url_for("orders.list_orders"))

    items = conn.execute(
        "SELECT oi.*, p.name as product_name, p.sku FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=?",
        (order_id,),
    ).fetchall()
    livreurs = []
    if g.user["role"] in ("super_admin", "moderateur", "agent_confirmation") and order["status"] == "confirmee":
        livreurs = conn.execute(
            "SELECT * FROM users WHERE role='livreur' AND is_active=1 ORDER BY full_name"
        ).fetchall()
    invoice = conn.execute(
        "SELECT i.* FROM invoices i JOIN invoice_orders io ON io.invoice_id=i.id WHERE io.order_id=?",
        (order_id,),
    ).fetchone()
    statuses = conn.execute("SELECT * FROM order_status_config ORDER BY sort_order").fetchall()
    last_location = conn.execute(
        "SELECT latitude, longitude, accuracy, recorded_at FROM courier_locations "
        "WHERE order_id=? ORDER BY recorded_at DESC, id DESC LIMIT 1",
        (order_id,),
    ).fetchone()
    courier_whatsapp_url = ""
    if order["livreur_whatsapp"]:
        courier_message = (
            f"Bonjour {order['livreur_name']}, une livraison {order['order_number']} vous est proposée. "
            f"Destinataire : {order['recipient_name'] or 'à confirmer'}. Adresse : {order['delivery_address'] or 'à confirmer'}. "
            "Ouvrez TrustDelivery pour accepter ou refuser."
        )
        courier_whatsapp_url = whatsapp_link(order["livreur_whatsapp"], courier_message)
    conn.close()
    return render_template(
        "order_detail.html", order=order, items=items, livreurs=livreurs, invoice=invoice,
        statuses=statuses, last_location=last_location, courier_whatsapp_url=courier_whatsapp_url
    )


def can_access_order(order):
    if not order:
        return False
    if g.user["role"] == "client":
        return order["client_id"] == g.user["id"]
    if g.user["role"] == "livreur":
        return order["livreur_id"] == g.user["id"]
    return g.user["role"] in ("super_admin", "moderateur", "agent_confirmation")


@bp.route("/<int:order_id>/position", methods=["POST"])
@roles_required("livreur")
def update_courier_location(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["livreur_id"] != g.user["id"] or order["status"] not in ("affectee", "en_livraison"):
        conn.close()
        return jsonify({"ok": False, "error": "Livraison non autorisée."}), 403
    data = request.get_json(silent=True) or {}
    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
        accuracy = float(data.get("accuracy")) if data.get("accuracy") is not None else None
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"ok": False, "error": "Coordonnées GPS invalides."}), 422
    conn.execute(
        "INSERT INTO courier_locations (livreur_id, order_id, latitude, longitude, accuracy) VALUES (?,?,?,?,?)",
        (g.user["id"], order_id, latitude, longitude, accuracy),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@bp.route("/<int:order_id>/temps-reel")
@login_required
def realtime_order(order_id):
    conn = get_db()
    order = conn.execute(
        "SELECT o.id, o.client_id, o.livreur_id, o.status, o.delivered_at, u.full_name livreur_name "
        "FROM orders o LEFT JOIN users u ON u.id=o.livreur_id WHERE o.id=?",
        (order_id,),
    ).fetchone()
    if not can_access_order(order):
        conn.close()
        return jsonify({"ok": False, "error": "Accès refusé."}), 403
    status = conn.execute("SELECT label, color FROM order_status_config WHERE status_key=?", (order["status"],)).fetchone()
    location = conn.execute(
        "SELECT latitude, longitude, accuracy, recorded_at FROM courier_locations "
        "WHERE order_id=? ORDER BY recorded_at DESC, id DESC LIMIT 1",
        (order_id,),
    ).fetchone()
    conn.close()
    return jsonify({
        "ok": True,
        "status": order["status"],
        "status_label": status["label"] if status else order["status"],
        "status_color": status["color"] if status else "#64748b",
        "livreur_name": order["livreur_name"],
        "delivered_at": order["delivered_at"],
        "gps": dict(location) if location else None,
    })


@bp.route("/<int:order_id>/confirmer", methods=["POST"])
@roles_required("super_admin", "moderateur", "agent_confirmation")
def confirm_order(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] != "en_attente":
        conn.close()
        flash("Cette commande ne peut pas être confirmée dans son état actuel.", "danger")
        return redirect(url_for("orders.order_detail", order_id=order_id))

    items = conn.execute("SELECT product_id, quantity FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    error = deduct_stock_for_items(conn, [(i["product_id"], i["quantity"]) for i in items])
    if error:
        conn.close()
        flash(error, "danger")
        return redirect(url_for("orders.order_detail", order_id=order_id))

    conn.execute(
        "UPDATE orders SET status='confirmee', confirmed_by=?, confirmed_at=datetime('now') WHERE id=?",
        (g.user["id"], order_id),
    )
    conn.commit()
    log_action(g.user, "Confirmation commande", order["order_number"])
    conn.close()
    flash(f"Commande {order['order_number']} confirmée. Le stock a été mis à jour.", "success")
    sync_shop_status(order_id, "confirmee")
    return redirect(url_for("orders.order_detail", order_id=order_id))


@bp.route("/<int:order_id>/affecter", methods=["POST"])
@roles_required("super_admin", "moderateur", "agent_confirmation")
def assign_livreur(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    livreur_id = request.form.get("livreur_id")
    share_whatsapp = request.form.get("share_whatsapp") == "1"
    livreur = conn.execute(
        "SELECT id, full_name FROM users WHERE id=? AND role='livreur' AND is_active=1", (livreur_id,)
    ).fetchone() if livreur_id else None
    if not order or order["status"] != "confirmee" or not livreur:
        conn.close()
        flash("Affectation impossible : la commande doit être confirmée et un livreur sélectionné.", "danger")
        return redirect(url_for("orders.order_detail", order_id=order_id))

    conn.execute(
        "UPDATE orders SET status='proposee', livreur_id=?, assigned_at=datetime('now') WHERE id=?",
        (livreur_id, order_id),
    )
    items = conn.execute("SELECT product_id, quantity FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    for item in items:
        conn.execute(
            "INSERT INTO courier_stock (courier_id, product_id, order_id, quantity_taken, status) VALUES (?,?,?,?,?) "
            "ON CONFLICT(courier_id, product_id, order_id) DO UPDATE SET quantity_taken=excluded.quantity_taken, taken_at=datetime('now'), status='propose'",
            (livreur_id, item["product_id"], order_id, item["quantity"], "propose"),
        )
    conn.commit()
    log_action(g.user, "Affectation livreur", f"{order['order_number']} -> {livreur['full_name']}")
    conn.close()
    flash(f"Livraison proposée à {livreur['full_name']}. Elle doit maintenant être acceptée.", "success")
    create_user_notification(
        livreur["id"], "Nouvelle livraison", f"La livraison {order['order_number']} vous est proposée.",
        url_for("orders.order_detail", order_id=order_id),
    )
    try:
        send_push_to_user(
            livreur["id"], "Nouvelle livraison",
            f"La livraison {order['order_number']} vous est proposée.",
            f"/commandes/{order_id}",
        )
    except Exception:
        pass
    notification_sent, notification_message, _direct_link = send_courier_notification(order_id, livreur["id"])
    if notification_sent is False:
        flash(notification_message, "warning")
    if share_whatsapp and _direct_link:
        return redirect(_direct_link)
    return redirect(url_for("orders.order_detail", order_id=order_id))


@bp.route("/<int:order_id>/repondre-proposition", methods=["POST"])
@roles_required("livreur")
def respond_to_assignment(order_id):
    action = request.form.get("action")
    conn = get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE id=? AND livreur_id=? AND status='proposee'",
        (order_id, g.user["id"]),
    ).fetchone()
    if not order or action not in ("accept", "reject"):
        conn.close()
        flash("Cette proposition n’est plus disponible.", "warning")
        return redirect(url_for("orders.list_orders"))
    if action == "accept":
        conn.execute("UPDATE orders SET status='affectee' WHERE id=?", (order_id,))
        conn.execute("UPDATE courier_stock SET status='pris_en_charge' WHERE order_id=? AND courier_id=?", (order_id, g.user["id"]))
        message = "Livraison acceptée. Vous pouvez maintenant la démarrer."
        log_entry = ("Livraison acceptée", order["order_number"])
    else:
        conn.execute("UPDATE courier_stock SET status='refuse' WHERE order_id=? AND courier_id=?", (order_id, g.user["id"]))
        conn.execute(
            "UPDATE orders SET status='confirmee', livreur_id=NULL, assigned_at=NULL WHERE id=?",
            (order_id,),
        )
        message = "Livraison refusée. Elle retourne au dispatch pour une nouvelle proposition."
        log_entry = ("Livraison refusée", order["order_number"])
    conn.commit()
    conn.close()
    log_action(g.user, *log_entry)
    if action == "accept":
        send_order_notification(order_id, "assigned", g.user["full_name"])
        sync_shop_status(order_id, "affectee")
    flash(message, "success" if action == "accept" else "info")
    return redirect(url_for("orders.order_detail", order_id=order_id) if action == "accept" else url_for("orders.list_orders"))


@bp.route("/<int:order_id>/statut-livraison", methods=["POST"])
@roles_required("livreur")
def update_delivery_status(order_id):
    new_status = request.form.get("new_status")
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=? AND livreur_id=?", (order_id, g.user["id"])).fetchone()
    if not order:
        conn.close()
        flash("Commande introuvable ou non assignée.", "danger")
        return redirect(url_for("dashboard.index"))

    allowed_transitions = {
        "affectee": ["en_livraison"],
        "en_livraison": ["livree", "retournee"],
    }
    if new_status not in allowed_transitions.get(order["status"], []):
        conn.close()
        flash("Transition de statut non autorisée.", "danger")
        return redirect(url_for("orders.order_detail", order_id=order_id))

    pending_log = None  # (action, details) — exécuté après commit + fermeture de la connexion

    if new_status == "livree":
        conn.execute("UPDATE orders SET status='livree', delivered_at=datetime('now') WHERE id=?", (order_id,))
        conn.execute("UPDATE courier_stock SET status='livre' WHERE order_id=?", (order_id,))
        for item in conn.execute("SELECT product_id, quantity FROM order_items WHERE order_id=?", (order_id,)).fetchall():
            stock_row = conn.execute("SELECT id FROM stock WHERE product_id=? ORDER BY id LIMIT 1", (item["product_id"],)).fetchone()
            if stock_row:
                conn.execute("UPDATE stock SET delivered_quantity=delivered_quantity+? WHERE id=?", (item["quantity"], stock_row["id"]))
        amount = order["total_amount"]
        invoice = conn.execute("SELECT * FROM invoices WHERE client_id=? AND is_closed=0 ORDER BY created_at DESC LIMIT 1", (order["client_id"],)).fetchone()
        if invoice:
            invoice_number, invoice_id = invoice["invoice_number"], invoice["id"]
            conn.execute("UPDATE invoices SET amount=amount+? WHERE id=?", (amount, invoice_id))
        else:
            invoice_number = generate_invoice_number(conn)
            invoice_id = conn.execute(
                "INSERT INTO invoices (invoice_number, order_id, client_id, amount, status) VALUES (?,?,?,?,'impayee')",
                (invoice_number, order_id, order["client_id"], amount),
            ).lastrowid
        conn.execute("INSERT OR IGNORE INTO invoice_orders (invoice_id, order_id) VALUES (?,?)", (invoice_id, order_id))
        pending_log = ("Livraison terminée", f"{order['order_number']} — ajoutée à la facture {invoice_number}")
        flash_msg = (f"Commande marquée comme livrée et ajoutée à la facture {invoice_number}.", "success")
    elif new_status == "retournee":
        conn.execute("UPDATE orders SET status='retournee' WHERE id=?", (order_id,))
        conn.execute("UPDATE courier_stock SET status='retourne' WHERE order_id=?", (order_id,))
        restock_items(conn, order_id, f"Retour commande {order['order_number']}")
        pending_log = ("Retour commande", order["order_number"])
        flash_msg = ("Commande marquée comme retournée. Le stock a été réajusté.", "warning")
    else:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (new_status, order_id))
        pending_log = ("Mise à jour statut livraison", f"{order['order_number']} -> {new_status}")
        flash_msg = ("Statut de la livraison mis à jour.", "success")

    conn.commit()
    conn.close()

    if pending_log:
        log_action(g.user, *pending_log)
    status_messages = {
        "en_livraison": ("Livraison en cours", f"Votre commande {order['order_number']} est en cours de livraison."),
        "livree": ("Commande livrée", f"Votre commande {order['order_number']} a été livrée."),
        "retournee": ("Commande retournée", f"Votre commande {order['order_number']} a été signalée comme retournée."),
    }
    if new_status in status_messages:
        title, message = status_messages[new_status]
        create_user_notification(order["client_id"], title, message, url_for("orders.order_detail", order_id=order_id))
    flash(*flash_msg)
    if new_status == "livree":
        notification_sent, notification_message = send_order_notification(order_id, "delivered")
        if not notification_sent:
            flash(notification_message, "warning")
        sync_result, sync_message = sync_shop_status(order_id, "livree")
        if sync_result is False:
            flash(f"Livraison enregistrée, mais la boutique n’a pas été mise à jour : {sync_message}", "warning")
    else:
        sync_shop_status(order_id, new_status)
    return redirect(url_for("orders.order_detail", order_id=order_id))


@bp.route("/<int:order_id>/annuler", methods=["POST"])
@login_required
def cancel_order(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        conn.close()
        flash("Commande introuvable.", "danger")
        return redirect(url_for("orders.list_orders"))

    if g.user["role"] == "client":
        if order["client_id"] != g.user["id"] or order["status"] != "en_attente":
            conn.close()
            flash("Vous ne pouvez annuler qu'une commande en attente qui vous appartient.", "danger")
            return redirect(url_for("orders.order_detail", order_id=order_id))
    elif g.user["role"] not in ("super_admin", "moderateur", "agent_confirmation"):
        conn.close()
        flash("Vous n'avez pas le droit d'annuler cette commande.", "danger")
        return redirect(url_for("orders.order_detail", order_id=order_id))

    if order["status"] in ("livree", "annulee", "retournee"):
        conn.close()
        flash("Cette commande ne peut plus être annulée.", "danger")
        return redirect(url_for("orders.order_detail", order_id=order_id))

    reason = request.form.get("reason", "").strip()
    if order["status"] in ("confirmee", "proposee", "affectee", "en_livraison"):
        restock_items(conn, order_id, f"Annulation commande {order['order_number']}")

    conn.execute("UPDATE orders SET status='annulee', cancel_reason=? WHERE id=?", (reason, order_id))
    conn.commit()
    log_action(g.user, "Annulation commande", f"{order['order_number']} — motif : {reason or 'non précisé'}")
    conn.close()
    flash(f"Commande {order['order_number']} annulée.", "info")
    sync_shop_status(order_id, "annulee")
    return redirect(url_for("orders.order_detail", order_id=order_id))
