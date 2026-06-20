from flask import Blueprint, render_template, request, redirect, url_for, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db, log_action
from auth import roles_required, login_required

bp = Blueprint("settings", __name__, url_prefix="/parametres")


@bp.route("/")
@roles_required("super_admin", "moderateur")
def index():
    conn = get_db()
    statuses = conn.execute("SELECT * FROM order_status_config ORDER BY sort_order").fetchall()
    zones = conn.execute("SELECT * FROM zones ORDER BY name").fetchall()
    warehouses = conn.execute("SELECT * FROM warehouses").fetchall()
    conn.close()
    return render_template("settings.html", statuses=statuses, zones=zones, warehouses=warehouses)


@bp.route("/statuts/<status_key>", methods=["POST"])
@roles_required("super_admin")
def update_status(status_key):
    label = request.form.get("label", "").strip()
    color = request.form.get("color", "#888888").strip()
    conn = get_db()
    conn.execute("UPDATE order_status_config SET label=?, color=? WHERE status_key=?", (label, color, status_key))
    conn.commit()
    log_action(g.user, "Configuration statut commande", f"{status_key} -> {label} ({color})")
    conn.close()
    flash("Statut mis à jour.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/zones/nouvelle", methods=["POST"])
@roles_required("super_admin", "moderateur")
def create_zone():
    name = request.form.get("name", "").strip()
    region = request.form.get("region", "").strip()
    delivery_fee = request.form.get("delivery_fee", "0")
    conn = get_db()
    if name:
        conn.execute("INSERT INTO zones (name, region, delivery_fee) VALUES (?,?,?)", (name, region, float(delivery_fee or 0)))
        conn.commit()
        log_action(g.user, "Création zone", name)
        flash(f"Zone « {name} » ajoutée.", "success")
    conn.close()
    return redirect(url_for("settings.index"))


@bp.route("/zones/<int:zone_id>/modifier", methods=["POST"])
@roles_required("super_admin", "moderateur")
def edit_zone(zone_id):
    name = request.form.get("name", "").strip()
    region = request.form.get("region", "").strip()
    delivery_fee = request.form.get("delivery_fee", "0")
    conn = get_db()
    conn.execute(
        "UPDATE zones SET name=?, region=?, delivery_fee=? WHERE id=?", (name, region, float(delivery_fee or 0), zone_id)
    )
    conn.commit()
    log_action(g.user, "Modification zone", f"Zone #{zone_id}")
    conn.close()
    flash("Zone mise à jour.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/audit")
@roles_required("super_admin")
def audit_log():
    conn = get_db()
    logs = conn.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 300").fetchall()
    conn.close()
    return render_template("audit_log.html", logs=logs)


@bp.route("/profil", methods=["GET", "POST"])
@login_required
def profile():
    conn = get_db()
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")

        if new_password:
            if not check_password_hash(g.user["password_hash"], current_password):
                flash("Mot de passe actuel incorrect.", "danger")
                conn.close()
                return redirect(url_for("settings.profile"))
            conn.execute(
                "UPDATE users SET full_name=?, phone=?, password_hash=? WHERE id=?",
                (full_name, phone, generate_password_hash(new_password), g.user["id"]),
            )
        else:
            conn.execute("UPDATE users SET full_name=?, phone=? WHERE id=?", (full_name, phone, g.user["id"]))
        conn.commit()
        log_action(g.user, "Mise à jour profil", "")
        conn.close()
        flash("Profil mis à jour avec succès.", "success")
        return redirect(url_for("settings.profile"))

    conn.close()
    return render_template("profile.html")
