import csv
import io
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, Response
from db import get_db, log_action
from auth import roles_required, login_required

bp = Blueprint("invoices", __name__, url_prefix="/factures")


def accessible_invoice(conn, invoice_id):
    invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    return invoice if invoice and (g.user["role"] != "client" or invoice["client_id"] == g.user["id"]) else None


@bp.route("/")
@login_required
def list_invoices():
    conn = get_db()
    query = ("SELECT i.*, u.full_name client_name, COUNT(io.order_id) order_count, "
             "i.amount + COALESCE((SELECT SUM(amount) FROM invoice_messages WHERE invoice_id=i.id),0) display_amount "
             "FROM invoices i JOIN users u ON u.id=i.client_id LEFT JOIN invoice_orders io ON io.invoice_id=i.id ")
    params = []
    if g.user["role"] == "client":
        query += "WHERE i.client_id=? "
        params.append(g.user["id"])
    query += "GROUP BY i.id ORDER BY i.created_at DESC"
    invoices = conn.execute(query, params).fetchall()
    total_impaye = sum(row["display_amount"] for row in invoices if row["status"] != "payee")
    conn.close()
    return render_template("invoices_list.html", invoices=invoices, total_impaye=total_impaye)


@bp.route("/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    conn = get_db()
    if not accessible_invoice(conn, invoice_id):
        conn.close()
        flash("Facture introuvable ou accès refusé.", "danger")
        return redirect(url_for("invoices.list_invoices"))
    invoice = conn.execute(
        "SELECT i.*, u.full_name client_name, u.email client_email, COUNT(io.order_id) order_count, "
        "i.amount + COALESCE((SELECT SUM(amount) FROM invoice_messages WHERE invoice_id=i.id),0) grand_total "
        "FROM invoices i JOIN users u ON u.id=i.client_id LEFT JOIN invoice_orders io ON io.invoice_id=i.id "
        "WHERE i.id=? GROUP BY i.id", (invoice_id,)).fetchone()
    orders = conn.execute("SELECT o.* FROM orders o JOIN invoice_orders io ON io.order_id=o.id WHERE io.invoice_id=? ORDER BY o.delivered_at, o.id", (invoice_id,)).fetchall()
    messages = conn.execute("SELECT m.*, u.full_name author FROM invoice_messages m LEFT JOIN users u ON u.id=m.created_by WHERE m.invoice_id=? ORDER BY m.created_at DESC, m.id DESC", (invoice_id,)).fetchall()
    conn.close()
    return render_template("invoice_detail.html", invoice=invoice, orders=orders, messages=messages)


@bp.route("/<int:invoice_id>/cloture", methods=["POST"])
@roles_required("super_admin", "moderateur")
def toggle_closed(invoice_id):
    conn = get_db()
    invoice = accessible_invoice(conn, invoice_id)
    if invoice:
        closed = 0 if invoice["is_closed"] else 1
        conn.execute("UPDATE invoices SET is_closed=?, closed_at=CASE WHEN ?=1 THEN datetime('now') ELSE NULL END WHERE id=?", (closed, closed, invoice_id))
        conn.commit()
        log_action(g.user, "Clôture facture", f"{invoice['invoice_number']} -> {'clôturée' if closed else 'ouverte'}")
    conn.close()
    return redirect(request.referrer or url_for("invoices.list_invoices"))


@bp.route("/<int:invoice_id>/payer", methods=["POST"])
@roles_required("super_admin", "moderateur")
def mark_paid(invoice_id):
    conn = get_db()
    invoice = accessible_invoice(conn, invoice_id)
    if invoice:
        paid = invoice["status"] != "payee"
        conn.execute("UPDATE invoices SET status=?, paid_at=CASE WHEN ?=1 THEN datetime('now') ELSE NULL END WHERE id=?", ("payee" if paid else "impayee", paid, invoice_id))
        conn.commit()
        log_action(g.user, "Versement facture", f"{invoice['invoice_number']} -> {'versée' if paid else 'non versée'}")
    conn.close()
    return redirect(request.referrer or url_for("invoices.list_invoices"))


@bp.route("/<int:invoice_id>/messages", methods=["POST"])
@roles_required("super_admin", "moderateur")
def add_message(invoice_id):
    message = request.form.get("message", "").strip()
    try:
        amount = float(request.form.get("amount", "0").replace(" ", "").replace(",", ".") or 0)
    except ValueError:
        amount = None
    conn = get_db()
    invoice = accessible_invoice(conn, invoice_id)
    if invoice and message and amount is not None:
        conn.execute("INSERT INTO invoice_messages (invoice_id, message, amount, created_by) VALUES (?,?,?,?)", (invoice_id, message, amount, g.user["id"]))
        conn.commit()
        flash("Charge ou message ajouté à la facture.", "success")
    else:
        flash("Veuillez saisir un message et un montant valide.", "danger")
    conn.close()
    return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))


@bp.route("/export")
@roles_required("super_admin", "moderateur")
def export_invoices():
    conn = get_db()
    invoices = conn.execute("SELECT i.invoice_number, u.full_name client_name, COUNT(io.order_id) order_count, i.amount, i.status, i.is_closed, i.created_at, i.paid_at FROM invoices i JOIN users u ON u.id=i.client_id LEFT JOIN invoice_orders io ON io.invoice_id=i.id GROUP BY i.id ORDER BY i.created_at DESC").fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["N° Facture", "Nb. commandes", "Client", "Montant (GNF)", "Clôturée", "Versée", "Date création", "Date versement"])
    for inv in invoices:
        writer.writerow([inv["invoice_number"], inv["order_count"], inv["client_name"], inv["amount"], "Oui" if inv["is_closed"] else "Non", "Oui" if inv["status"] == "payee" else "Non", inv["created_at"], inv["paid_at"] or ""])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=export_factures_trustdelivery.csv"})
