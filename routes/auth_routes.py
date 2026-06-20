from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import check_password_hash
from db import get_db, log_action

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        error = None
        if user is None or not check_password_hash(user["password_hash"], password):
            error = "Email ou mot de passe incorrect."
        elif not user["is_active"]:
            error = "Ce compte a été suspendu. Contactez un administrateur."

        if error is None:
            session.clear()
            session["user_id"] = user["id"]
            log_action(user, "Connexion", f"Connexion réussie ({user['email']})")
            flash(f"Bienvenue, {user['full_name']} !", "success")
            next_url = request.args.get("next") or url_for("dashboard.index")
            return redirect(next_url)

        log_action(None, "Échec de connexion", f"Tentative avec email : {email}")
        flash(error, "danger")

    return render_template("login.html")


@bp.route("/logout")
def logout():
    if g.user is not None:
        log_action(g.user, "Déconnexion", "")
    session.clear()
    flash("Vous avez été déconnecté.", "info")
    return redirect(url_for("auth.login"))
