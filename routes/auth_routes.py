import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import check_password_hash
from db import get_db, log_action
from auth import TAB_PARAM, get_request_tab_id

bp = Blueprint("auth", __name__)


def with_tab_param(url, tab_id):
    if not tab_id:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[TAB_PARAM] = tab_id
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@bp.route("/login", methods=["GET", "POST"])
def gh --version
winget install --id GitHub.cli -e    # si gh absentgh auth login
# choisissez GitHub.com → Login with a web browserlogin():
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
            tab_id = get_request_tab_id() or secrets.token_urlsafe(18)
            tab_sessions = dict(session.get("tab_sessions", {}))
            tab_sessions[tab_id] = user["id"]
            session["tab_sessions"] = tab_sessions
            log_action(user, "Connexion", f"Connexion réussie ({user['email']})")
            flash(f"Bienvenue, {user['full_name']} !", "success")
            next_url = request.args.get("next") or url_for("dashboard.index")
            return redirect(with_tab_param(next_url, tab_id))

        log_action(None, "Échec de connexion", f"Tentative avec email : {email}")
        flash(error, "danger")

    return render_template("login.html")


@bp.route("/logout")
def logout():
    if g.user is not None:
        log_action(g.user, "Déconnexion", "")
    tab_id = get_request_tab_id()
    tab_sessions = dict(session.get("tab_sessions", {}))
    if tab_id:
        tab_sessions.pop(tab_id, None)
        session["tab_sessions"] = tab_sessions
    else:
        session.pop("tab_sessions", None)
    flash("Vous avez été déconnecté.", "info")
    return redirect(url_for("auth.login", _tab=tab_id) if tab_id else url_for("auth.login"))
