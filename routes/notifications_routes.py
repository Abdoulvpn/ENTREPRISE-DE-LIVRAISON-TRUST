from flask import Blueprint, g, jsonify, redirect, render_template, request, url_for

from auth import login_required
from db import get_db


bp = Blueprint("notifications", __name__, url_prefix="/notifications")


@bp.route("/")
@login_required
def index():
    conn = get_db()
    notifications = conn.execute(
        "SELECT * FROM user_notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
        (g.user["id"],),
    ).fetchall()
    conn.close()
    return render_template("notifications.html", notifications=notifications)


@bp.route("/non-lues")
@login_required
def unread_count():
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) c FROM user_notifications WHERE user_id=? AND is_read=0", (g.user["id"],)
    ).fetchone()["c"]
    conn.close()
    return jsonify({"count": count})


@bp.route("/appareil", methods=["POST"])
@login_required
def register_device():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    if len(token) < 20 or len(token) > 4096:
        return jsonify({"error": "Jeton appareil invalide"}), 400
    conn = get_db()
    conn.execute(
        "INSERT INTO push_device_tokens (user_id, token, platform) VALUES (?,?, 'android') "
        "ON CONFLICT(token) DO UPDATE SET user_id=excluded.user_id, is_active=1, last_seen_at=datetime('now')",
        (g.user["id"], token),
    )
    conn.commit()
    conn.close()
    return jsonify({"registered": True})


@bp.route("/tout-lire", methods=["POST"])
@login_required
def mark_all_read():
    conn = get_db()
    conn.execute(
        "UPDATE user_notifications SET is_read=1, read_at=datetime('now') WHERE user_id=? AND is_read=0",
        (g.user["id"],),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("notifications.index"))


@bp.route("/<int:notification_id>/ouvrir", methods=["POST"])
@login_required
def open_notification(notification_id):
    conn = get_db()
    notification = conn.execute(
        "SELECT * FROM user_notifications WHERE id=? AND user_id=?", (notification_id, g.user["id"])
    ).fetchone()
    if notification:
        conn.execute(
            "UPDATE user_notifications SET is_read=1, read_at=COALESCE(read_at,datetime('now')) WHERE id=?",
            (notification_id,),
        )
        conn.commit()
    conn.close()
    link = notification["link"] if notification else ""
    if not link or not link.startswith("/") or link.startswith("//"):
        link = url_for("notifications.index")
    return redirect(link)
