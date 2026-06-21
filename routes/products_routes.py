from flask import Blueprint, render_template, request, redirect, url_for, flash, g
from db import get_db, log_action
from auth import roles_required, login_required

bp = Blueprint("products", __name__, url_prefix="/produits")


@bp.route("/")
@login_required
def list_products():
    conn = get_db()
    search = request.args.get("q", "").strip()
    client_filter = request.args.get("client_id", "").strip()
    query = (
        "SELECT p.*, u.full_name as supplier_client_name, "
        "COALESCE(SUM(s.quantity),0) as total_stock, MIN(s.alert_threshold) as alert_threshold "
        "FROM products p LEFT JOIN stock s ON s.product_id = p.id "
        "LEFT JOIN users u ON u.id=p.supplier_client_id "
    )
    conditions, params = [], []

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
@roles_required("super_admin", "moderateur")
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
        supplier_client_id = request.form.get("supplier_client_id", "").strip()
        price = request.form.get("price", "0")
        warehouse_id = request.form.get("warehouse_id")
        initial_qty = request.form.get("initial_qty", "0")
        alert_threshold = request.form.get("alert_threshold", "5")

        error = None
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
                "INSERT INTO products (name, sku, description, category, supplier, supplier_client_id, price, is_validated) "
                "VALUES (?,?,?,?,?,?,?,1)",
                (name, sku, description, category, supplier_client["full_name"], supplier_client_id, float(price)),
            )
            product_id = cur.lastrowid
            conn.execute(
                "INSERT INTO stock (product_id, warehouse_id, quantity, alert_threshold) VALUES (?,?,?,?)",
                (product_id, warehouse_id, int(initial_qty or 0), int(alert_threshold or 5)),
            )
            if int(initial_qty or 0) > 0:
                conn.execute(
                    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, quantity, note, created_by) VALUES (?,?,?,?,?,?)",
                    (product_id, warehouse_id, "entree", int(initial_qty), "Stock initial à la création du produit", g.user["id"]),
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
@roles_required("super_admin", "moderateur")
def edit_product(product_id):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    warehouses = conn.execute("SELECT * FROM warehouses").fetchall()
    clients = conn.execute(
        "SELECT id, full_name, email FROM users WHERE role='client' AND is_active=1 ORDER BY full_name"
    ).fetchall()
    if not product:
        conn.close()
        flash("Produit introuvable.", "danger")
        return redirect(url_for("products.list_products"))

    if request.method == "POST":
        supplier_client_id = request.form.get("supplier_client_id", "").strip()
        supplier_client = conn.execute(
            "SELECT id, full_name FROM users WHERE id=? AND role='client' AND is_active=1",
            (supplier_client_id,),
        ).fetchone() if supplier_client_id else None
        if not supplier_client:
            conn.close()
            flash("Veuillez sélectionner un client fournisseur actif.", "danger")
            return redirect(url_for("products.edit_product", product_id=product_id))
        conn.execute(
            "UPDATE products SET name=?, description=?, category=?, supplier=?, supplier_client_id=?, price=? WHERE id=?",
            (
                request.form.get("name", "").strip(),
                request.form.get("description", "").strip(),
                request.form.get("category", "").strip(),
                supplier_client["full_name"],
                supplier_client_id,
                float(request.form.get("price", "0")),
                product_id,
            ),
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
