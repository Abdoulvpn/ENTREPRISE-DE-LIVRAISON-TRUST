from flask import Blueprint, render_template, request, redirect, url_for, flash, g
from werkzeug.security import generate_password_hash
from db import get_db, log_action, ROLES
from auth import roles_required

bp = Blueprint("users", __name__, url_prefix="/utilisateurs")


@bp.route("/")
@roles_required("super_admin", "moderateur")
def list_users():
    conn = get_db()
    role_filter = request.args.get("role", "")
    query = "SELECT * FROM users"
    params = []
    if role_filter:
        query += " WHERE role = ?"
        params.append(role_filter)
    query += " ORDER BY created_at DESC"
    users = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("users_list.html", users=users, roles=ROLES, role_filter=role_filter)


@bp.route("/nouveau", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur")
def create_user():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role")
        phone = request.form.get("phone", "").strip()
        zone = request.form.get("zone", "").strip()
        password = request.form.get("password", "")

        error = None
        if not full_name or not email or not role or not password:
            error = "Tous les champs obligatoires doivent être renseignés."
        elif role == "super_admin" and g.user["role"] != "super_admin":
            error = "Seul un Super Administrateur peut créer un autre Super Administrateur."
        elif role not in ROLES:
            error = "Rôle invalide."

        if error is None:
            conn = get_db()
            existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if existing:
                error = "Cet email est déjà utilisé."
            else:
                conn.execute(
                    "INSERT INTO users (full_name, email, password_hash, role, phone, zone, is_active) VALUES (?,?,?,?,?,?,1)",
                    (full_name, email, generate_password_hash(password), role, phone, zone),
                )
                conn.commit()
                log_action(g.user, "Création utilisateur", f"{full_name} ({email}) — rôle : {ROLES.get(role)}")
                conn.close()
                flash(f"Le compte de {full_name} a été créé avec succès.", "success")
                return redirect(url_for("users.list_users"))
            conn.close()

        flash(error, "danger")

    return render_template("user_form.html", roles=ROLES, user=None)


@bp.route("/<int:user_id>/modifier", methods=["GET", "POST"])
@roles_required("super_admin", "moderateur")
def edit_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("Utilisateur introuvable.", "danger")
        return redirect(url_for("users.list_users"))

    if user["role"] == "super_admin" and g.user["role"] != "super_admin":
        conn.close()
        flash("Vous n'avez pas le droit de modifier un Super Administrateur.", "danger")
        return redirect(url_for("users.list_users"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        role = request.form.get("role")
        phone = request.form.get("phone", "").strip()
        zone = request.form.get("zone", "").strip()
        is_active = 1 if request.form.get("is_active") == "on" else 0
        new_password = request.form.get("password", "")

        if role == "super_admin" and g.user["role"] != "super_admin":
            flash("Seul un Super Administrateur peut attribuer ce rôle.", "danger")
            conn.close()
            return redirect(url_for("users.edit_user", user_id=user_id))

        if new_password:
            conn.execute(
                "UPDATE users SET full_name=?, role=?, phone=?, zone=?, is_active=?, password_hash=? WHERE id=?",
                (full_name, role, phone, zone, is_active, generate_password_hash(new_password), user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET full_name=?, role=?, phone=?, zone=?, is_active=? WHERE id=?",
                (full_name, role, phone, zone, is_active, user_id),
            )
        conn.commit()
        log_action(g.user, "Modification utilisateur", f"Compte #{user_id} ({full_name}) mis à jour")
        conn.close()
        flash("Compte mis à jour avec succès.", "success")
        return redirect(url_for("users.list_users"))

    conn.close()
    return render_template("user_form.html", roles=ROLES, user=user)


@bp.route("/<int:user_id>/supprimer", methods=["POST"])
@roles_required("super_admin")
def delete_user(user_id):
    if user_id == g.user["id"]:
        flash("Vous ne pouvez pas supprimer votre propre compte.", "danger")
        return redirect(url_for("users.list_users"))
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        log_action(g.user, "Suppression utilisateur", f"{user['full_name']} ({user['email']})")
    conn.close()
    flash("Le compte a été supprimé définitivement.", "info")
    return redirect(url_for("users.list_users"))
