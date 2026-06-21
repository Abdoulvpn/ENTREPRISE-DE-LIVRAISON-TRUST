import csv
import io
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, Response
from db import get_db, log_action
from auth import roles_required, login_required

bp = Blueprint("invoices", __name__, url_prefix="/factures")


@bp.route("/")
@login_required
def list_invoices():
    conn = get_db()
    query = (
        "SELECT i.*, o.total_amount as display_amount, u.full_name as client_name, o.order_number FROM invoices i "
        "JOIN users u ON u.id=i.client_id JOIN orders o ON o.id=i.order_id "
    )
    params = []
    if g.user["role"] == "client":
        query += "WHERE i.client_id = ? "
        params.append(g.user["id"])
    query += "ORDER BY i.created_at DESC"
    invoices = conn.execute(query, params).fetchall()

    total_query = (
        "SELECT COALESCE(SUM(o.total_amount),0) s FROM invoices i "
        "JOIN orders o ON o.id=i.order_id WHERE i.status='impayee'"
    )
    total_params = []
    if g.user["role"] == "client":
        total_query += " AND i.client_id=?"
        total_params.append(g.user["id"])
    total_impaye = conn.execute(total_query, total_params).fetchone()["s"]
    conn.close()
    return render_template("invoices_list.html", invoices=invoices, total_impaye=total_impaye)


@bp.route("/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    conn = get_db()
    invoice = conn.execute(
        "SELECT i.*, u.full_name as client_name, u.email as client_email, o.order_number, o.delivery_address, "
        "o.recipient_name, o.recipient_phone, o.total_amount as order_total FROM invoices i "
        "JOIN users u ON u.id=i.client_id JOIN orders o ON o.id=i.order_id WHERE i.id=?",
        (invoice_id,),
    ).fetchone()
    if not invoice or (g.user["role"] == "client" and invoice["client_id"] != g.user["id"]):
        conn.close()
        flash("Facture introuvable ou accès refusé.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    items = conn.execute(
        "SELECT oi.*, p.name as product_name FROM order_items oi JOIN products p ON p.id=oi.product_id "
        "WHERE oi.order_id = (SELECT order_id FROM invoices WHERE id=?)",
        (invoice_id,),
    ).fetchall()
    conn.close()
    return render_template("invoice_detail.html", invoice=invoice, items=items)


@bp.route("/<int:invoice_id>/payer", methods=["POST"])
@roles_required("super_admin", "moderateur")
def mark_paid(invoice_id):
    conn = get_db()
    invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if invoice and invoice["status"] != "payee":
        conn.execute("UPDATE invoices SET status='payee', paid_at=datetime('now') WHERE id=?", (invoice_id,))
        conn.commit()
        log_action(g.user, "Paiement facture", invoice["invoice_number"])
        flash(f"Facture {invoice['invoice_number']} marquée comme payée.", "success")
    conn.close()
    return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))


@bp.route("/export")
@roles_required("super_admin", "moderateur")
def export_invoices():
    conn = get_db()
    invoices = conn.execute(
        "SELECT i.invoice_number, o.order_number, u.full_name as client_name, o.total_amount, i.status, i.created_at, i.paid_at "
        "FROM invoices i JOIN users u ON u.id=i.client_id JOIN orders o ON o.id=i.order_id ORDER BY i.created_at DESC"
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["N° Facture", "N° Commande", "Client", "Montant (GNF)", "Statut", "Date création", "Date paiement"])
    for inv in invoices:
        writer.writerow(
            [inv["invoice_number"], inv["order_number"], inv["client_name"], inv["total_amount"], inv["status"], inv["created_at"], inv["paid_at"] or ""]
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=export_factures_trustdelivery.csv"},
    )
