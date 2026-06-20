"""
Authentification & contrôle d'accès basé sur les rôles (RBAC) — TrustDelivery
Session Flask légère (cookie signé), mots de passe hashés avec Werkzeug.
"""
from functools import wraps
from flask import session, redirect, url_for, request, flash, g
from db import get_db, ROLES


def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        conn = get_db()
        g.user = conn.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
        conn.close()
        if g.user is None:
            session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Veuillez vous connecter pour accéder à cette page.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Veuillez vous connecter pour accéder à cette page.", "warning")
                return redirect(url_for("auth.login", next=request.path))
            if g.user["role"] not in roles:
                flash("Accès refusé : vous n'avez pas les droits nécessaires pour cette action.", "danger")
                return redirect(url_for("dashboard.index"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def role_label(role_key):
    return ROLES.get(role_key, role_key)
