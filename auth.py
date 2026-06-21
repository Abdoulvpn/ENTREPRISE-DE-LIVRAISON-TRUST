"""
Authentification & contrôle d'accès basé sur les rôles (RBAC) — TrustDelivery
Session Flask légère (cookie signé), mots de passe hashés avec Werkzeug.
"""
from functools import wraps
from flask import session, redirect, url_for, request, flash, g
from db import get_db, ROLES


TAB_PARAM = "_tab"


def get_request_tab_id():
    tab_id = request.values.get(TAB_PARAM) or request.headers.get("X-TrustDelivery-Tab")
    if tab_id:
        tab_id = tab_id.strip()
    if tab_id and len(tab_id) <= 80:
        return tab_id
    return None


def login_redirect_target():
    return request.full_path if request.query_string else request.path


def load_logged_in_user():
    g.tab_id = get_request_tab_id()
    tab_sessions = session.get("tab_sessions", {})
    user_id = tab_sessions.get(g.tab_id) if g.tab_id else None

    if user_id is None:
        g.user = None
    else:
        conn = get_db()
        g.user = conn.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
        conn.close()
        if g.user is None:
            tab_sessions.pop(g.tab_id, None)
            session["tab_sessions"] = tab_sessions
            session.modified = True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Veuillez vous connecter pour accéder à cette page.", "warning")
            return redirect(url_for("auth.login", next=login_redirect_target()))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Veuillez vous connecter pour accéder à cette page.", "warning")
                return redirect(url_for("auth.login", next=login_redirect_target()))
            if g.user["role"] not in roles:
                flash("Accès refusé : vous n'avez pas les droits nécessaires pour cette action.", "danger")
                return redirect(url_for("dashboard.index"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def role_label(role_key):
    return ROLES.get(role_key, role_key)
