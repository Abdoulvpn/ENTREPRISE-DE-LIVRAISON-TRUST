from flask import Blueprint, render_template, g
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
            "SELECT i.*, o.total_amount as display_amount FROM invoices i "
            "JOIN orders o ON o.id=i.order_id WHERE i.client_id = ? ORDER BY i.created_at DESC LIMIT 10",
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
        }
        conn.close()
        return render_template("dashboard_client.html", orders=orders, invoices=invoices, stats=stats)

    if role == "livreur":
        deliveries = conn.execute(
            "SELECT o.*, u.full_name as client_name FROM orders o JOIN users u ON u.id=o.client_id "
            "WHERE o.livreur_id=? AND o.status IN ('affectee','en_livraison') ORDER BY o.assigned_at DESC",
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

    # --- Vue globale : super_admin, moderateur, agent_confirmation ---
    total_orders = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    confirmed_or_more = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE status NOT IN ('en_attente','annulee')"
    ).fetchone()["c"]
    livrees = conn.execute("SELECT COUNT(*) c FROM orders WHERE status='livree'").fetchone()["c"]
    retournees = conn.execute("SELECT COUNT(*) c FROM orders WHERE status='retournee'").fetchone()["c"]
    annulees = conn.execute("SELECT COUNT(*) c FROM orders WHERE status='annulee'").fetchone()["c"]
    en_attente = conn.execute("SELECT COUNT(*) c FROM orders WHERE status='en_attente'").fetchone()["c"]
    cloturees = livrees + retournees + annulees

    taux_confirmation = round((confirmed_or_more / total_orders) * 100, 1) if total_orders else 0
    taux_livraison = round((livrees / cloturees) * 100, 1) if cloturees else 0
    taux_retour = round((retournees / cloturees) * 100, 1) if cloturees else 0

    ca_total = conn.execute(
        "SELECT COALESCE(SUM(total_amount),0) ca FROM orders WHERE status='livree'"
    ).fetchone()["ca"]

    # CA des 7 derniers jours (pour le graphique)
    days, ca_per_day = [], []
    for i in range(6, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        days.append((datetime.now() - timedelta(days=i)).strftime("%d/%m"))
        val = conn.execute(
            "SELECT COALESCE(SUM(total_amount),0) ca FROM orders "
            "WHERE status='livree' AND date(delivered_at)=?",
            (d,),
        ).fetchone()["ca"]
        ca_per_day.append(val)

    # Performance par zone
    perf_zone = conn.execute(
        "SELECT z.name as zone_name, COUNT(o.id) as nb_commandes, "
        "COALESCE(SUM(CASE WHEN o.status='livree' THEN o.total_amount ELSE 0 END),0) as ca "
        "FROM orders o LEFT JOIN zones z ON z.id=o.zone_id GROUP BY o.zone_id ORDER BY nb_commandes DESC"
    ).fetchall()

    alerts_stock = conn.execute(
        "SELECT p.name, p.sku, s.quantity, s.alert_threshold FROM stock s "
        "JOIN products p ON p.id=s.product_id WHERE s.quantity <= s.alert_threshold ORDER BY s.quantity ASC"
    ).fetchall()

    recent_orders = conn.execute(
        "SELECT o.*, u.full_name as client_name FROM orders o JOIN users u ON u.id=o.client_id "
        "ORDER BY o.created_at DESC LIMIT 8"
    ).fetchall()

    pending_confirmation = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE status='en_attente'"
    ).fetchone()["c"]

    conn.close()

    stats = {
        "total_orders": total_orders,
        "en_attente": en_attente,
        "taux_confirmation": taux_confirmation,
        "taux_livraison": taux_livraison,
        "taux_retour": taux_retour,
        "ca_total": ca_total,
        "pending_confirmation": pending_confirmation,
    }

    chart_data = {
        "days": days,
        "ca_per_day": ca_per_day,
    }

    return render_template(
        "dashboard.html",
        stats=stats,
        chart_data=json.dumps(chart_data),
        perf_zone=perf_zone,
        alerts_stock=alerts_stock,
        recent_orders=recent_orders,
    )
