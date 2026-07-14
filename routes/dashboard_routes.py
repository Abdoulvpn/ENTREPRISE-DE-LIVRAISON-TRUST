from flask import Blueprint, render_template, g, request
from db import get_db
from datetime import datetime, timedelta
import json

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@bp.route("/dashboard")
def index():
    from auth import login_required  # local import avoids circular import at module load
    conn = get_db()
    role = g.user["role"] if g.user else None

    if g.user is None:
        conn.close()
        from flask import redirect, url_for
        return redirect(url_for("auth.login"))

    if role == "client":
        orders = conn.execute(
            "SELECT * FROM orders WHERE client_id = ? ORDER BY created_at DESC LIMIT 10", (g.user["id"],)
        ).fetchall()
        invoices = conn.execute(
            "SELECT i.*, i.amount + COALESCE((SELECT SUM(amount) FROM invoice_messages WHERE invoice_id=i.id),0) display_amount "
            "FROM invoices i WHERE i.client_id = ? ORDER BY i.created_at DESC LIMIT 10",
            (g.user["id"],),
        ).fetchall()
        stats = {
            "total_orders": conn.execute("SELECT COUNT(*) c FROM orders WHERE client_id=?", (g.user["id"],)).fetchone()["c"],
            "en_cours": conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE client_id=? AND status NOT IN ('livree','annulee','retournee')",
                (g.user["id"],),
            ).fetchone()["c"],
            "livrees": conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE client_id=? AND status='livree'", (g.user["id"],)
            ).fetchone()["c"],
            "factures_impayees": conn.execute(
                "SELECT COUNT(*) c FROM invoices WHERE client_id=? AND status='impayee'", (g.user["id"],)
            ).fetchone()["c"],
            "boutiques_connectees": conn.execute(
                "SELECT COUNT(*) c FROM shop_connections WHERE client_id=? AND is_active=1", (g.user["id"],)
            ).fetchone()["c"],
            "imports_automatiques": conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE client_id=? AND source='webhook'", (g.user["id"],)
            ).fetchone()["c"],
            "livraisons_gps": conn.execute(
                "SELECT COUNT(DISTINCT o.id) c FROM orders o JOIN courier_locations cl ON cl.order_id=o.id "
                "WHERE o.client_id=? AND o.status IN ('affectee','en_livraison')", (g.user["id"],)
            ).fetchone()["c"],
        }
        connections = conn.execute(
            "SELECT id, platform, shop_name, webhook_token, auto_dispatch, is_active FROM shop_connections "
            "WHERE client_id=? ORDER BY created_at DESC", (g.user["id"],)
        ).fetchall()
        conn.close()
        return render_template("dashboard_client.html", orders=orders, invoices=invoices, stats=stats, connections=connections)

    if role == "livreur":
        deliveries = conn.execute(
            "SELECT o.*, u.full_name as client_name FROM orders o JOIN users u ON u.id=o.client_id "
            "WHERE o.livreur_id=? AND o.status IN ('proposee','affectee','en_livraison') ORDER BY o.assigned_at DESC",
            (g.user["id"],),
        ).fetchall()
        stats = {
            "a_livrer": len(deliveries),
            "livrees_total": conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE livreur_id=? AND status='livree'", (g.user["id"],)
            ).fetchone()["c"],
            "retournees": conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE livreur_id=? AND status='retournee'", (g.user["id"],)
            ).fetchone()["c"],
        }
        conn.close()
        return render_template("dashboard_livreur.html", deliveries=deliveries, stats=stats)

    # --- Vue analytique globale : filtres combinables et métriques métier ---
    filter_keys = ("manager_id", "employee_id", "account_id", "seller_id", "affiliate_id", "courier_id", "sub_courier_id", "zone_id", "product_id", "date_from", "date_to")
    selected = {key: request.args.get(key, "").strip() for key in filter_keys}
    conditions, params = [], []
    mappings = {
        "employee_id": "o.confirmed_by=?", "account_id": "o.shop_connection_id=?",
        "seller_id": "o.client_id=?", "affiliate_id": "o.client_id=?",
        "courier_id": "o.livreur_id=?", "sub_courier_id": "o.livreur_id=?", "zone_id": "o.zone_id=?",
    }
    if selected["manager_id"]:
        conditions.append("EXISTS (SELECT 1 FROM users mc WHERE mc.id=o.client_id AND mc.manager_id=?)")
        params.append(selected["manager_id"])
    for key, clause in mappings.items():
        if selected[key]:
            conditions.append(clause)
            params.append(selected[key])
    if selected["product_id"]:
        conditions.append("EXISTS (SELECT 1 FROM order_items fi WHERE fi.order_id=o.id AND fi.product_id=?)")
        params.append(selected["product_id"])
    if selected["date_from"]:
        conditions.append("date(o.created_at)>=?")
        params.append(selected["date_from"])
    if selected["date_to"]:
        conditions.append("date(o.created_at)<=?")
        params.append(selected["date_to"])
    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    counts = conn.execute(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN o.status='livree' THEN 1 ELSE 0 END) delivered, "
        "SUM(CASE WHEN o.status IN ('confirmee','proposee','affectee','en_livraison','expediee') THEN 1 ELSE 0 END) in_progress, "
        "SUM(CASE WHEN o.status IN ('pdr','injoignable','retournee') THEN 1 ELSE 0 END) pdr, "
        "SUM(CASE WHEN o.status IN ('reportee','interessee') THEN 1 ELSE 0 END) postponed, "
        "SUM(CASE WHEN o.status IN ('annulee','refusee') THEN 1 ELSE 0 END) cancelled, "
        "SUM(CASE WHEN o.status NOT IN ('en_attente','annulee','refusee','injoignable') THEN 1 ELSE 0 END) confirmed "
        "FROM orders o" + where, params
    ).fetchone()
    total = counts["total"] or 0
    confirmed = counts["confirmed"] or 0
    pct = lambda value, denominator=total: round(((value or 0) / denominator) * 100, 1) if denominator else 0

    finance = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN o.status='livree' THEN o.total_amount ELSE 0 END),0) ca, "
        "COALESCE(SUM(CASE WHEN o.status='livree' THEN o.delivery_fee ELSE 0 END),0) fees, "
        "COALESCE(SUM(CASE WHEN o.status='livree' THEN o.courier_paid_amount ELSE 0 END),0) courier_paid "
        "FROM orders o" + where, params
    ).fetchone()
    paid = conn.execute(
        "SELECT COALESCE(SUM(i.amount),0) amount FROM invoices i JOIN orders o ON o.id=i.order_id" +
        where + (" AND " if where else " WHERE ") + "i.status='payee'", params
    ).fetchone()["amount"]
    expense_conditions, expense_params = [], []
    if selected["date_from"]:
        expense_conditions.append("date(expense_date)>=?"); expense_params.append(selected["date_from"])
    if selected["date_to"]:
        expense_conditions.append("date(expense_date)<=?"); expense_params.append(selected["date_to"])
    expense_where = " WHERE " + " AND ".join(expense_conditions) if expense_conditions else ""
    expenses = conn.execute("SELECT COALESCE(SUM(amount),0) amount FROM expenses" + expense_where, expense_params).fetchone()["amount"]

    perf_params = list(params)
    product_perf = conn.execute(
        "SELECT p.name label, COUNT(DISTINCT o.id) total, "
        "COUNT(DISTINCT CASE WHEN o.status NOT IN ('en_attente','annulee','refusee','injoignable') THEN o.id END) confirmed, "
        "COUNT(DISTINCT CASE WHEN o.status='livree' THEN o.id END) delivered "
        "FROM orders o JOIN order_items oi ON oi.order_id=o.id JOIN products p ON p.id=oi.product_id" + where +
        " GROUP BY p.id ORDER BY total DESC", perf_params
    ).fetchall()
    supplier_perf = conn.execute(
        "SELECT u.full_name label, COUNT(DISTINCT o.id) total, "
        "COUNT(DISTINCT CASE WHEN o.status NOT IN ('en_attente','annulee','refusee','injoignable') THEN o.id END) confirmed, "
        "COUNT(DISTINCT CASE WHEN o.status='livree' THEN o.id END) delivered "
        "FROM orders o JOIN users u ON u.id=o.client_id" + where + " GROUP BY u.id ORDER BY total DESC", params
    ).fetchall()
    courier_perf = conn.execute(
        "SELECT COALESCE(u.full_name, 'Non affecté') label, COUNT(DISTINCT o.id) total, "
        "COUNT(DISTINCT CASE WHEN o.status NOT IN ('en_attente','annulee','refusee','injoignable') THEN o.id END) confirmed, "
        "COUNT(DISTINCT CASE WHEN o.status='livree' THEN o.id END) delivered "
        "FROM orders o LEFT JOIN users u ON u.id=o.livreur_id" + where + " GROUP BY o.livreur_id ORDER BY total DESC", params
    ).fetchall()

    def decorate(rows):
        return [{**dict(row), "confirmation_rate": pct(row["confirmed"], row["total"]), "delivery_rate": pct(row["delivered"], row["confirmed"])} for row in rows]

    days, ca_per_day = [], []
    for i in range(6, -1, -1):
        day = datetime.now() - timedelta(days=i)
        days.append(day.strftime("%d/%m"))
        daily_where = where + (" AND " if where else " WHERE ") + "o.status='livree' AND date(o.delivered_at)=?"
        ca_per_day.append(conn.execute("SELECT COALESCE(SUM(o.total_amount),0) ca FROM orders o" + daily_where, [*params, day.strftime("%Y-%m-%d")]).fetchone()["ca"])

    perf_zone = conn.execute(
        "SELECT COALESCE(z.name,'Non définie') zone_name, COUNT(o.id) nb_commandes, "
        "COALESCE(SUM(CASE WHEN o.status='livree' THEN o.total_amount ELSE 0 END),0) ca "
        "FROM orders o LEFT JOIN zones z ON z.id=o.zone_id" + where + " GROUP BY o.zone_id ORDER BY nb_commandes DESC", params
    ).fetchall()
    recent_orders = conn.execute(
        "SELECT o.*, u.full_name client_name FROM orders o JOIN users u ON u.id=o.client_id" + where + " ORDER BY o.created_at DESC LIMIT 8", params
    ).fetchall()
    activities = conn.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 12").fetchall()
    alerts_stock = conn.execute(
        "SELECT p.name, p.sku, s.quantity, s.alert_threshold FROM stock s JOIN products p ON p.id=s.product_id "
        "WHERE s.quantity<=s.alert_threshold AND p.is_archived=0 ORDER BY s.quantity"
    ).fetchall()
    options = {
        "managers": conn.execute("SELECT id, full_name FROM users WHERE role IN ('super_admin','moderateur') AND is_active=1 ORDER BY full_name").fetchall(),
        "employees": conn.execute("SELECT id, full_name FROM users WHERE role='agent_confirmation' AND is_active=1 ORDER BY full_name").fetchall(),
        "accounts": conn.execute("SELECT id, shop_name name FROM shop_connections WHERE is_active=1 ORDER BY shop_name").fetchall(),
        "sellers": conn.execute("SELECT id, full_name FROM users WHERE role='client' AND client_type='seller' AND is_active=1 ORDER BY full_name").fetchall(),
        "affiliates": conn.execute("SELECT id, full_name FROM users WHERE role='client' AND client_type='affiliate' AND is_active=1 ORDER BY full_name").fetchall(),
        "couriers": conn.execute("SELECT id, full_name FROM users WHERE role='livreur' AND parent_courier_id IS NULL AND is_active=1 ORDER BY full_name").fetchall(),
        "sub_couriers": conn.execute("SELECT id, full_name FROM users WHERE role='livreur' AND parent_courier_id IS NOT NULL AND is_active=1 ORDER BY full_name").fetchall(),
        "zones": conn.execute("SELECT id, name FROM zones ORDER BY name").fetchall(),
        "products": conn.execute("SELECT id, name FROM products WHERE is_archived=0 ORDER BY name").fetchall(),
    }
    stats = {
        "total_orders": total, "delivered": counts["delivered"] or 0, "delivered_pct": pct(counts["delivered"]),
        "in_progress": counts["in_progress"] or 0, "in_progress_pct": pct(counts["in_progress"]),
        "pdr": counts["pdr"] or 0, "pdr_pct": pct(counts["pdr"]), "postponed": counts["postponed"] or 0,
        "postponed_pct": pct(counts["postponed"]), "cancelled": counts["cancelled"] or 0, "cancelled_pct": pct(counts["cancelled"]),
        "taux_confirmation": pct(confirmed), "taux_livraison": pct(counts["delivered"], confirmed),
        "ca_total": finance["ca"], "ca_paid": paid, "ca_unpaid": max(finance["ca"] - paid, 0), "delivery_fees": finance["fees"],
        "delivery_profit": finance["fees"], "expenses": expenses, "net_profit": finance["fees"] - expenses,
        "courier_ca": finance["fees"], "courier_paid": finance["courier_paid"], "courier_unpaid": max(finance["fees"] - finance["courier_paid"], 0),
    }
    conn.close()
    return render_template("dashboard.html", stats=stats, selected=selected, options=options,
        chart_data=json.dumps({"days": days, "ca_per_day": ca_per_day}), perf_zone=perf_zone,
        product_perf=decorate(product_perf), supplier_perf=decorate(supplier_perf), courier_perf=decorate(courier_perf),
        alerts_stock=alerts_stock, recent_orders=recent_orders, activities=activities)
