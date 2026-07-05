import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from db import backup_database, create_user_notification, get_db, log_action, validate_password_strength
from auth import roles_required, login_required
from integrations import send_whatsapp_otp

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


@bp.route("/sauvegarde-base")
@roles_required("super_admin")
def download_database_backup():
    backup_path = backup_database(force=True)
    log_action(g.user, "Sauvegarde base de données", "Export manuel sécurisé")
    return send_file(
        backup_path,
        as_attachment=True,
        download_name=f"trustdelivery-sauvegarde-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db",
        mimetype="application/x-sqlite3",
    )


@bp.route("/profil", methods=["GET", "POST"])
@login_required
def profile():
    conn = get_db()
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        if g.user["is_protected"]:
            full_name = g.user["full_name"]
        phone = request.form.get("phone", "").strip()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")

        if new_password:
            password_error = validate_password_strength(new_password)
            if password_error:
                flash(password_error, "danger")
                conn.close()
                return redirect(url_for("settings.profile"))
            if not check_password_hash(g.user["password_hash"], current_password):
                flash("Mot de passe actuel incorrect.", "danger")
                conn.close()
                return redirect(url_for("settings.profile"))
            conn.execute(
                "UPDATE users SET full_name=?, phone=?, password_hash=? WHERE id=?",
                (full_name, phone, generate_password_hash(new_password), g.user["id"]),
            )
        else:
            conn.execute(
                "UPDATE users SET full_name=?, phone=? WHERE id=?",
                (full_name, phone, g.user["id"]),
            )
        conn.commit()
        log_action(g.user, "Mise à jour profil", "")
        conn.close()
        flash("Profil mis à jour avec succès.", "success")
        return redirect(url_for("settings.profile"))

    whatsapp_verification = conn.execute(
        "SELECT * FROM whatsapp_verifications WHERE user_id=?", (g.user["id"],)
    ).fetchone()
    conn.close()
    return render_template("profile.html", whatsapp_verification=whatsapp_verification)


def normalize_whatsapp_number(value):
    number = re.sub(r"\D", "", value or "")
    return number if 8 <= len(number) <= 15 else ""


@bp.route("/profil/whatsapp/envoyer", methods=["POST"])
@login_required
def send_whatsapp_verification():
    phone = normalize_whatsapp_number(request.form.get("whatsapp_phone"))
    if not phone:
        flash("Saisissez le numéro complet avec l'indicatif pays, entre 8 et 15 chiffres.", "danger")
        return redirect(url_for("settings.profile"))

    conn = get_db()
    duplicate = conn.execute(
        "SELECT user_id FROM whatsapp_verifications WHERE phone_number=? AND is_verified=1 AND user_id<>?",
        (phone, g.user["id"]),
    ).fetchone()
    current = conn.execute(
        "SELECT * FROM whatsapp_verifications WHERE user_id=?", (g.user["id"],)
    ).fetchone()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if duplicate:
        conn.close()
        flash("Ce numéro WhatsApp est déjà lié à un autre compte.", "danger")
        return redirect(url_for("settings.profile"))
    if current and current["is_verified"] and current["phone_number"] == phone:
        conn.close()
        flash("Ce numéro WhatsApp est déjà vérifié.", "info")
        return redirect(url_for("settings.profile"))
    if current:
        last_sent = datetime.strptime(current["last_sent_at"], "%Y-%m-%d %H:%M:%S")
        if now - last_sent < timedelta(seconds=60):
            remaining = 60 - int((now - last_sent).total_seconds())
            conn.close()
            flash(f"Patientez encore {remaining} seconde(s) avant de renvoyer un code.", "warning")
            return redirect(url_for("settings.profile"))
    conn.close()

    otp = f"{secrets.randbelow(1_000_000):06d}"
    try:
        send_whatsapp_otp(phone, otp)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        flash(f"Le code n'a pas pu être envoyé : {exc}", "danger")
        return redirect(url_for("settings.profile"))

    sent_at = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    otp_hash = generate_password_hash(otp)
    conn = get_db()
    conn.execute(
        "INSERT INTO whatsapp_verifications "
        "(user_id, phone_number, otp_hash, expires_at, is_verified, verified_at, last_sent_at, failed_attempts, updated_at) "
        "VALUES (?,?,?,?,0,NULL,?,0,?) "
        "ON CONFLICT(user_id) DO UPDATE SET phone_number=excluded.phone_number, otp_hash=excluded.otp_hash, "
        "expires_at=excluded.expires_at, is_verified=0, verified_at=NULL, last_sent_at=excluded.last_sent_at, "
        "failed_attempts=0, updated_at=excluded.updated_at",
        (g.user["id"], phone, otp_hash, expires_at, sent_at, sent_at),
    )
    conn.commit()
    conn.close()
    log_action(g.user, "Envoi OTP WhatsApp", f"Vérification demandée pour le compte #{g.user['id']}")
    flash("Un code de vérification a été envoyé sur WhatsApp. Il est valable 5 minutes.", "success")
    return redirect(url_for("settings.profile"))


@bp.route("/profil/whatsapp/verifier", methods=["POST"])
@login_required
def verify_whatsapp():
    otp = re.sub(r"\D", "", request.form.get("otp", ""))
    if len(otp) != 6:
        flash("Le code doit contenir exactement 6 chiffres.", "danger")
        return redirect(url_for("settings.profile"))
    conn = get_db()
    verification = conn.execute(
        "SELECT * FROM whatsapp_verifications WHERE user_id=?", (g.user["id"],)
    ).fetchone()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if not verification or verification["is_verified"]:
        conn.close()
        flash("Aucune vérification WhatsApp en attente.", "warning")
        return redirect(url_for("settings.profile"))
    if verification["failed_attempts"] >= 5:
        conn.close()
        flash("Trop de tentatives. Demandez un nouveau code.", "danger")
        return redirect(url_for("settings.profile"))
    if now > datetime.strptime(verification["expires_at"], "%Y-%m-%d %H:%M:%S"):
        conn.close()
        flash("Ce code a expiré. Demandez un nouveau code.", "danger")
        return redirect(url_for("settings.profile"))
    if not check_password_hash(verification["otp_hash"], otp):
        conn.execute(
            "UPDATE whatsapp_verifications SET failed_attempts=failed_attempts+1, updated_at=? WHERE user_id=?",
            (now.strftime("%Y-%m-%d %H:%M:%S"), g.user["id"]),
        )
        conn.commit()
        conn.close()
        flash("Code incorrect. Vérifiez le message reçu sur WhatsApp.", "danger")
        return redirect(url_for("settings.profile"))

    verified_at = now.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE whatsapp_verifications SET is_verified=1, verified_at=?, otp_hash='', updated_at=? WHERE user_id=?",
        (verified_at, verified_at, g.user["id"]),
    )
    conn.execute(
        "UPDATE users SET whatsapp_phone=? WHERE id=?",
        (verification["phone_number"], g.user["id"]),
    )
    conn.commit()
    conn.close()
    create_user_notification(
        g.user["id"], "WhatsApp lié", "Votre numéro WhatsApp a été vérifié et lié avec succès.", url_for("settings.profile")
    )
    log_action(g.user, "Liaison WhatsApp vérifiée", f"Compte #{g.user['id']}")
    flash("WhatsApp lié avec succès", "success")
    return redirect(url_for("settings.profile"))
