import os
from datetime import date
from uuid import uuid4

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, current_app
from werkzeug.utils import secure_filename
from db import backup_database, get_db, log_action
from auth import roles_required, login_required

bp = Blueprint("products", __name__, url_prefix="/produits")

ALLOWED_IMAGES = {"png", "jpg", "jpeg", "gif", "webp"}


def save_photo(file):
    if not file or not file.filename:
        return None
    extension = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if extension not in ALLOWED_IMAGES:
        raise ValueError("Format d'image non autorisé (PNG, JPG, GIF ou WEBP uniquement).")
    upload_dir = os.path.join(current_app.static_folder, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    filename = secure_filename(f"{uuid4().hex}.{extension}")
    file.save(os.path.join(upload_dir, filename))
    return f"uploads/{filename}"


@bp.route("/")
@login_required
def list_products():
    conn = get_db()
    search = request.args.get("q", "").strip()
    client_filter = request.args.get("client_id", "").strip()
    query = (
        "SELECT p.*, u.full_name as supplier_client_name, "
        "COALESCE(SUM(s.quantity),0) as total_stock, COALESCE(SUM(s.initial_quantity),0) as qte_initiale, "
        "COALESCE(SUM(s.damaged_quantity),0) as qte_endommagee, "
        "COALESCE(SUM(s.delivered_quantity),0) as qte_livree, "
        "MAX(s.note) as stock_note, MAX(s.visible_seller) as visible_seller, MIN(s.alert_threshold) as alert_threshold "
        "FROM products p LEFT JOIN stock s ON s.product_id = p.id "
        "LEFT JOIN users u ON u.id=p.supplier_client_id "
    )
    conditions, params = ["p.is_archived=0"], []

    if g.user["role"] == "client":
        conditions.append("p.supplier_client_id = ?")
        params.append(g.user["id"])
        client_filter = str(g.user["id"])
    elif client_filter:
        conditions.append("p.supplier_client_id = ?")
        params.append(client_filter)

    if search:
        conditions.append("(p.name LIKE ? OR p.sku LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if conditions:
        query += "WHERE " + " AND ".join(conditions) + " "
    query += "GROUP BY p.id ORDER BY p.created_at DESC"
    products = conn.execute(query, params).fetchall()
    clients = []
    if g.user["role"] != "client":
        clients = conn.execute(
            "SELECT id, full_name, email FROM users WHERE role='client' AND is_active=1 ORDER BY full_name"
        ).fetchall()
    conn.close()
    return render_template(
        "products_list.html", products=products, search=search, clients=clients, client_filter=client_filter
    )


@bp.route("/nouveau", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur", "client")
def create_product():
    conn = get_db()
    warehouses = conn.execute("SELECT * FROM warehouses").fetchall()
    clients = conn.execute(
        "SELECT id, full_name, email FROM users WHERE role='client' AND is_active=1 ORDER BY full_name"
    ).fetchall()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        sku = request.form.get("sku", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        supplier_client_id = str(g.user["id"]) if g.user["role"] == "client" else request.form.get("supplier_client_id", "").strip()
        price = request.form.get("price", "0")
        warehouse_id = request.form.get("warehouse_id")
        initial_qty = request.form.get("initial_qty", "0")
        alert_threshold = request.form.get("alert_threshold", "5")

        error = None
        try:
            initial_qty_value = int(initial_qty or 0)
            damaged_qty_value = int(request.form.get("damaged_qty", 0) or 0)
            price_value = float(price)
            if initial_qty_value < 0 or damaged_qty_value < 0 or damaged_qty_value > initial_qty_value:
                raise ValueError("La quantité endommagée doit être comprise entre 0 et la quantité initiale.")
            photo = save_photo(request.files.get("photo"))
        except (ValueError, TypeError) as exc:
            photo, error = None, str(exc)
        supplier_client = conn.execute(
            "SELECT id, full_name FROM users WHERE id=? AND role='client' AND is_active=1",
            (supplier_client_id,),
        ).fetchone() if supplier_client_id else None
        if not name or not sku or not price or not warehouse_id or not supplier_client:
            error = "Veuillez renseigner les champs obligatoires, notamment le client fournisseur."
        elif conn.execute("SELECT id FROM products WHERE sku=?", (sku,)).fetchone():
            error = "Cette référence (SKU) existe déjà."

        if error is None:
            cur = conn.execute(
                "INSERT INTO products (name, sku, description, category, supplier, supplier_client_id, price, is_validated, link, photo) "
                "VALUES (?,?,?,?,?,?,?,1,?,?)",
                (name, sku, description, category, supplier_client["full_name"], supplier_client_id, price_value, request.form.get("link", "").strip(), photo),
            )
            product_id = cur.lastrowid
            conn.execute(
                "INSERT INTO stock (product_id, warehouse_id, quantity, initial_quantity, damaged_quantity, alert_threshold, note, visible_seller, is_validated) VALUES (?,?,?,?,?,?,?,?,1)",
                (product_id, warehouse_id, initial_qty_value - damaged_qty_value, initial_qty_value, damaged_qty_value, int(alert_threshold or 5), request.form.get("note", "").strip(), 1 if request.form.get("visible_seller") else 0),
            )
            if initial_qty_value > 0:
                conn.execute(
                    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, quantity, note, created_by) VALUES (?,?,?,?,?,?)",
                    (product_id, warehouse_id, "entree", initial_qty_value, "Stock initial à la création du produit", g.user["id"]),
                )
            conn.commit()
            log_action(g.user, "Création produit", f"{name} ({sku})")
            conn.close()
            flash(f"Le produit « {name} » a été ajouté au catalogue.", "success")
            return redirect(url_for("products.list_products"))

        flash(error, "danger")

    conn.close()
    return render_template("product_form.html", warehouses=warehouses, clients=clients, product=None)


@bp.route("/<int:product_id>/modifier", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur", "client")
def edit_product(product_id):
    conn = get_db()
    product = conn.execute(
        "SELECT p.*, COALESCE(MAX(s.damaged_quantity),0) damaged_qty, MAX(s.note) stock_note, "
        "COALESCE(MAX(s.visible_seller),0) stock_visible_seller FROM products p LEFT JOIN stock s ON s.product_id=p.id WHERE p.id=? GROUP BY p.id",
        (product_id,),
    ).fetchone()
    warehouses = conn.execute("SELECT * FROM warehouses").fetchall()
    clients = conn.execute(
        "SELECT id, full_name, email FROM users WHERE role='client' AND is_active=1 ORDER BY full_name"
    ).fetchall()
    if not product:
        conn.close()
        flash("Produit introuvable.", "danger")
        return redirect(url_for("products.list_products"))

    if request.method == "POST":
        if g.user["role"] == "client" and product["supplier_client_id"] != g.user["id"]:
            conn.close()
            return "Accès refusé", 403
        supplier_client_id = str(g.user["id"]) if g.user["role"] == "client" else request.form.get("supplier_client_id", "").strip()
        supplier_client = conn.execute(
            "SELECT id, full_name FROM users WHERE id=? AND role='client' AND is_active=1",
            (supplier_client_id,),
        ).fetchone() if supplier_client_id else None
        if not supplier_client:
            conn.close()
            flash("Veuillez sélectionner un client fournisseur actif.", "danger")
            return redirect(url_for("products.edit_product", product_id=product_id))
        try:
            photo = save_photo(request.files.get("photo")) or product["photo"]
            price_value = float(request.form.get("price", "0"))
            damaged_qty_value = int(request.form.get("damaged_qty", 0) or 0)
            if damaged_qty_value < 0:
                raise ValueError("La quantité endommagée ne peut pas être négative.")
        except (ValueError, TypeError) as exc:
            conn.close()
            flash(str(exc), "danger")
            return redirect(url_for("products.edit_product", product_id=product_id))
        conn.execute(
            "UPDATE products SET name=?, description=?, category=?, supplier=?, supplier_client_id=?, price=?, link=?, photo=? WHERE id=?",
            (
                request.form.get("name", "").strip(),
                request.form.get("description", "").strip(),
                request.form.get("category", "").strip(),
                supplier_client["full_name"],
                supplier_client_id,
                price_value,
                request.form.get("link", "").strip(),
                photo,
                product_id,
            ),
        )
        damaged_delta = damaged_qty_value - product["damaged_qty"]
        available = conn.execute("SELECT COALESCE(SUM(quantity),0) q FROM stock WHERE product_id=?", (product_id,)).fetchone()["q"]
        if damaged_delta > available:
            conn.rollback(); conn.close()
            flash("La quantité endommagée dépasse le stock restant.", "danger")
            return redirect(url_for("products.edit_product", product_id=product_id))
        stock_rows = conn.execute("SELECT id FROM stock WHERE product_id=? ORDER BY id", (product_id,)).fetchall()
        for index, row in enumerate(stock_rows):
            conn.execute(
                "UPDATE stock SET quantity=MAX(quantity-?,0), damaged_quantity=?, note=?, visible_seller=? WHERE id=?",
                (damaged_delta if index == 0 else 0, damaged_qty_value if index == 0 else 0, request.form.get("note", "").strip(), 1 if request.form.get("visible_seller") else 0, row["id"]),
            )
        conn.commit()
        log_action(g.user, "Modification produit", f"Produit #{product_id} mis à jour")
        conn.close()
        flash("Produit mis à jour avec succès.", "success")
        return redirect(url_for("products.list_products"))

    conn.close()
    return render_template("product_form.html", warehouses=warehouses, clients=clients, product=product)


@bp.route("/<int:product_id>/mouvements", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur")
def stock_movements(product_id):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    warehouses = conn.execute("SELECT * FROM warehouses").fetchall()
    if not product:
        conn.close()
        flash("Produit introuvable.", "danger")
        return redirect(url_for("products.list_products"))

    if request.method == "POST":
        warehouse_id = int(request.form.get("warehouse_id"))
        movement_type = request.form.get("movement_type")
        quantity = int(request.form.get("quantity", 0))
        note = request.form.get("note", "").strip()

        if quantity <= 0 or movement_type not in ("entree", "sortie"):
            flash("Quantité ou type de mouvement invalide.", "danger")
        else:
            stock_row = conn.execute(
                "SELECT * FROM stock WHERE product_id=? AND warehouse_id=?", (product_id, warehouse_id)
            ).fetchone()
            if stock_row is None:
                conn.execute(
                    "INSERT INTO stock (product_id, warehouse_id, quantity, alert_threshold) VALUES (?,?,0,5)",
                    (product_id, warehouse_id),
                )
                current_qty = 0
            else:
                current_qty = stock_row["quantity"]

            if movement_type == "sortie" and quantity > current_qty:
                flash(f"Stock insuffisant : {current_qty} unité(s) disponible(s) seulement.", "danger")
            else:
                delta = quantity if movement_type == "entree" else -quantity
                conn.execute(
                    "UPDATE stock SET quantity = quantity + ? WHERE product_id=? AND warehouse_id=?",
                    (delta, product_id, warehouse_id),
                )
                conn.execute(
                    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, quantity, note, created_by) VALUES (?,?,?,?,?,?)",
                    (product_id, warehouse_id, movement_type, quantity, note, g.user["id"]),
                )
                conn.commit()
                log_action(g.user, "Mouvement de stock", f"{movement_type} de {quantity} pour {product['name']}")
                flash("Mouvement de stock enregistré.", "success")

    stock_levels = conn.execute(
        "SELECT s.*, w.name as warehouse_name FROM stock s JOIN warehouses w ON w.id=s.warehouse_id WHERE s.product_id=?",
        (product_id,),
    ).fetchall()
    movements = conn.execute(
        "SELECT m.*, w.name as warehouse_name, u.full_name as created_by_name FROM stock_movements m "
        "JOIN warehouses w ON w.id=m.warehouse_id LEFT JOIN users u ON u.id=m.created_by "
        "WHERE m.product_id=? ORDER BY m.created_at DESC LIMIT 50",
        (product_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "stock_movements.html", product=product, warehouses=warehouses, stock_levels=stock_levels, movements=movements
    )


@bp.route("/<int:product_id>/supprimer", methods=["POST"])
@roles_required("super_admin", "moderateur")
def delete_product(product_id):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        flash("Produit introuvable.", "danger")
    elif product["is_archived"]:
        flash("Ce produit est déjà archivé.", "info")
    else:
        backup_database(force=True)
        conn.execute("UPDATE products SET is_archived=1, is_validated=0 WHERE id=?", (product_id,))
        conn.commit()
        log_action(g.user, "Archivage produit", f"{product['name']} ({product['sku']})")
        flash("Produit archivé; son stock et tout son historique sont conservés.", "success")
    conn.close()
    return redirect(url_for("products.list_products"))


@bp.route("/envois")
@roles_required("super_admin", "moderateur", "client")
def list_shipments():
    conn = get_db()
    query = (
        "SELECT sh.*, u.full_name client_name FROM shipments sh "
        "JOIN users u ON u.id=sh.client_id "
    )
    params = []
    if g.user["role"] == "client":
        query += "WHERE sh.client_id=? "
        params.append(g.user["id"])
    query += "ORDER BY sh.shipment_date DESC, sh.id DESC"
    shipments = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("shipments_list.html", shipments=shipments)


def shipment_form(shipment_id=None):
    conn = get_db()
    shipment = conn.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,)).fetchone() if shipment_id else None
    if shipment and g.user["role"] == "client" and shipment["client_id"] != g.user["id"]:
        conn.close()
        return "Accès refusé", 403
    clients = conn.execute("SELECT id, full_name FROM users WHERE role='client' AND is_active=1 ORDER BY full_name").fetchall()
    if request.method == "POST":
        if shipment and shipment["validated"]:
            conn.close()
            flash("Un envoi validé ne peut plus être modifié ; modifiez le produit créé dans le stock client.", "warning")
            return redirect(url_for("products.list_shipments"))
        client_id = g.user["id"] if g.user["role"] == "client" else request.form.get("client_id")
        title = request.form.get("product_title", "").strip()
        ref = request.form.get("ref", "").strip()
        try:
            quantity = int(request.form.get("quantity", 0))
            photo = save_photo(request.files.get("photo")) or (shipment["photo"] if shipment else None)
        except (ValueError, TypeError) as exc:
            flash(str(exc) if str(exc) else "Quantité invalide.", "danger")
        else:
            if not client_id or not title or not ref or quantity <= 0:
                flash("Client, produit, référence et quantité positive sont obligatoires.", "danger")
            else:
                values = (client_id, title, ref, quantity, request.form.get("description", "").strip(), request.form.get("link", "").strip(), photo, request.form.get("shipment_date") or date.today().isoformat())
                if shipment:
                    conn.execute("UPDATE shipments SET client_id=?, product_title=?, ref=?, quantity=?, description=?, link=?, photo=?, shipment_date=? WHERE id=?", (*values, shipment_id))
                    action = "Modification envoi produit"
                else:
                    conn.execute("INSERT INTO shipments (client_id, product_title, ref, quantity, description, link, photo, shipment_date, created_by) VALUES (?,?,?,?,?,?,?,?,?)", (*values, g.user["id"]))
                    action = "Création envoi produit"
                conn.commit()
                log_action(g.user, action, f"{title} ({ref}), quantité {quantity}")
                conn.close()
                flash("Envoi enregistré.", "success")
                return redirect(url_for("products.list_shipments"))
    conn.close()
    return render_template("shipment_form.html", shipment=shipment, clients=clients, today=date.today().isoformat())


@bp.route("/envois/nouveau", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur", "client")
def create_shipment():
    return shipment_form()


@bp.route("/envois/<int:shipment_id>/modifier", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur", "client")
def edit_shipment(shipment_id):
    return shipment_form(shipment_id)


@bp.route("/envois/<int:shipment_id>/valider", methods=["POST"])
@roles_required("super_admin", "moderateur")
def validate_shipment(shipment_id):
    conn = get_db()
    shipment = conn.execute("SELECT sh.*, u.full_name client_name FROM shipments sh JOIN users u ON u.id=sh.client_id WHERE sh.id=?", (shipment_id,)).fetchone()
    if not shipment or shipment["validated"]:
        conn.close()
        flash("Envoi introuvable ou déjà validé.", "danger")
        return redirect(url_for("products.list_shipments"))
    if conn.execute("SELECT 1 FROM products WHERE sku=?", (shipment["ref"],)).fetchone():
        conn.close()
        flash("Un produit possède déjà cette référence.", "danger")
        return redirect(url_for("products.list_shipments"))
    warehouse = conn.execute("SELECT id FROM warehouses ORDER BY id LIMIT 1").fetchone()
    if not warehouse:
        conn.close()
        flash("Créez d'abord un entrepôt.", "danger")
        return redirect(url_for("products.list_shipments"))
    cur = conn.execute(
        "INSERT INTO products (name, sku, description, supplier, supplier_client_id, price, is_validated, link, photo) VALUES (?,?,?,?,?,0,1,?,?)",
        (shipment["product_title"], shipment["ref"], shipment["description"], shipment["client_name"], shipment["client_id"], shipment["link"], shipment["photo"]),
    )
    product_id = cur.lastrowid
    conn.execute("INSERT INTO stock (product_id, warehouse_id, quantity, initial_quantity, alert_threshold, is_validated) VALUES (?,?,?,?,5,1)", (product_id, warehouse["id"], shipment["quantity"], shipment["quantity"]))
    conn.execute("INSERT INTO stock_movements (product_id, warehouse_id, movement_type, quantity, note, created_by) VALUES (?,?,?,?,?,?)", (product_id, warehouse["id"], "entree", shipment["quantity"], "Validation de l'envoi produit", g.user["id"]))
    conn.execute("UPDATE shipments SET validated=1, product_id=? WHERE id=?", (product_id, shipment_id))
    conn.commit()
    log_action(g.user, "Validation envoi produit", f"{shipment['product_title']} ({shipment['ref']})")
    conn.close()
    flash("Envoi validé : le produit a été créé dans le stock client.", "success")
    return redirect(url_for("products.list_shipments"))


@bp.route("/stock-livreurs")
@roles_required("super_admin", "moderateur", "agent_confirmation")
def courier_stock():
    conn = get_db()
    rows = conn.execute(
        "SELECT cs.*, u.full_name courier_name, p.name product_name, p.sku "
        "FROM courier_stock cs JOIN users u ON u.id=cs.courier_id "
        "JOIN products p ON p.id=cs.product_id ORDER BY cs.taken_at DESC"
    ).fetchall()
    conn.close()
    return render_template("courier_stock.html", rows=rows)
