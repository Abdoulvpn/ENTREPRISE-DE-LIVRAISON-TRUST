"""
TrustDelivery — Plateforme de Gestion de Livraison, Stock et Commandes
Application Flask développée à partir du cahier des charges fourni.
Démarrage : python app.py  →  http://127.0.0.1:5000
"""
import os
from flask import Flask, g, redirect, url_for

import db as db_module
from auth import load_logged_in_user, role_label
from db import ROLES

from routes import auth_routes, dashboard_routes, users_routes, products_routes, orders_routes, invoices_routes, settings_routes


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "trustdelivery-dev-secret-change-me")

    db_module.init_db()

    app.before_request(load_logged_in_user)

    app.register_blueprint(auth_routes.bp)
    app.register_blueprint(dashboard_routes.bp)
    app.register_blueprint(users_routes.bp)
    app.register_blueprint(products_routes.bp)
    app.register_blueprint(orders_routes.bp)
    app.register_blueprint(invoices_routes.bp)
    app.register_blueprint(settings_routes.bp)

    @app.context_processor
    def inject_globals():
        return {
            "current_user": g.get("user"),
            "ROLES": ROLES,
            "role_label": role_label,
        }

    @app.template_filter("money")
    def money_filter(value):
        try:
            return f"{int(round(float(value))):,}".replace(",", " ") + " GNF"
        except (TypeError, ValueError):
            return value

    @app.template_filter("dt")
    def datetime_filter(value, fmt="%d/%m/%Y %H:%M"):
        if not value:
            return ""
        try:
            from datetime import datetime
            for parse_fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(value, parse_fmt).strftime(fmt)
                except ValueError:
                    continue
            return value
        except Exception:
            return value

    @app.errorhandler(404)
    def not_found(e):
        return redirect(url_for("dashboard.index"))

    return app


app = create_app()

if __name__ == "__main__":
    print("=" * 70)
    print(" TrustDelivery — Plateforme de Gestion de Livraison")
    print(" Application disponible sur : http://127.0.0.1:5000")
    print(" Compte Super Administrateur :")
    print("   Email    : thierno.keita@trustdelivery.com")
    print("   Mot de passe : TrustDelivery@2026")
    print(" (Changez ce mot de passe après la première connexion.)")
    print("=" * 70)
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(debug=debug, host="0.0.0.0", port=port)
