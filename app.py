import csv
import io
import json
import os
import secrets
import urllib.request
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
db_url = os.environ.get("DATABASE_URL", "sqlite:///livraisons.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
db = SQLAlchemy(app)
csrf = CSRFProtect(app)

STATUTS = ["en attente", "en cours", "livré"]


# ---------- Modèles ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="admin")  # admin | livreur
    livreur_id = db.Column(db.Integer, db.ForeignKey("livreur.id"))


class Livreur(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nom = db.Column(db.String(100), nullable=False)
    telephone = db.Column(db.String(30), nullable=False)
    disponible = db.Column(db.Boolean, default=True)
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    vu_le = db.Column(db.DateTime)


class Partenaire(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nom = db.Column(db.String(100), nullable=False)
    telephone = db.Column(db.String(30))
    adresse = db.Column(db.String(255))


class Commande(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(12), unique=True, default=lambda: secrets.token_hex(4).upper())
    client_nom = db.Column(db.String(100), nullable=False)
    client_tel = db.Column(db.String(30), nullable=False)
    adresse = db.Column(db.String(255), nullable=False)
    prix = db.Column(db.Integer, default=0)
    statut = db.Column(db.String(20), default="en attente")
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    livreur_id = db.Column(db.Integer, db.ForeignKey("livreur.id"))
    livreur = db.relationship("Livreur")
    partenaire_id = db.Column(db.Integer, db.ForeignKey("partenaire.id"))
    partenaire = db.relationship("Partenaire")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---------- Utilitaires ----------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            return redirect(url_for("mes_commandes"))
        return f(*a, **k)
    return wrapper


def can_livreur(lid):
    return session.get("role") == "admin" or session.get("livreur_id") == lid


def to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def to_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def notifier_client(c):
    """Envoie un SMS via un service HTTP (SMS_API_URL / SMS_API_KEY). Sans config : simple log."""
    suivi = request.host_url + "suivi/" + c.code
    msg = f"Votre commande #{c.id} est maintenant : {c.statut}. Suivi : {suivi}"
    app.logger.info("SMS -> %s : %s", c.client_tel, msg)
    url = os.environ.get("SMS_API_URL")
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"to": c.client_tel, "message": msg}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + os.environ.get("SMS_API_KEY", "")})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:  # ne jamais bloquer la livraison à cause du SMS
        app.logger.warning("Échec SMS : %s", e)


def changer_statut(c, statut):
    if statut in STATUTS and statut != c.statut:
        c.statut = statut
        db.session.commit()
        notifier_client(c)


# ---------- Authentification ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(username=request.form["username"]).first()
        if user and check_password_hash(user.password_hash, request.form["password"]):
            session.clear()
            session.update(user_id=user.id, role=user.role, livreur_id=user.livreur_id)
            return redirect(url_for("commandes" if user.role == "admin" else "mes_commandes"))
        flash("Identifiants incorrects.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Commandes (admin) ----------
@app.route("/")
@admin_required
def commandes():
    filtre = request.args.get("statut")
    q = Commande.query.order_by(Commande.created_at.desc())
    if filtre in STATUTS:
        q = q.filter_by(statut=filtre)
    return render_template("commandes.html", commandes=q.all(),
                           livreurs=Livreur.query.all(), statuts=STATUTS)


@app.route("/commandes/nouvelle", methods=["GET", "POST"])
@admin_required
def nouvelle_commande():
    if request.method == "POST":
        c = Commande(client_nom=request.form["client_nom"], client_tel=request.form["client_tel"],
                     adresse=request.form["adresse"], prix=to_int(request.form.get("prix")) or 0,
                     partenaire_id=to_int(request.form.get("partenaire_id")),
                     lat=to_float(request.form.get("lat")), lng=to_float(request.form.get("lng")))
        db.session.add(c)
        db.session.commit()
        flash("Commande créée.")
        return redirect(url_for("commandes"))
    return render_template("nouvelle.html", partenaires=Partenaire.query.all())


@app.route("/commandes/<int:cid>/modifier", methods=["GET", "POST"])
@admin_required
def modifier_commande(cid):
    c = db.get_or_404(Commande, cid)
    if request.method == "POST":
        c.client_nom, c.client_tel = request.form["client_nom"], request.form["client_tel"]
        c.adresse = request.form["adresse"]
        c.prix = to_int(request.form.get("prix")) or 0
        c.partenaire_id = to_int(request.form.get("partenaire_id"))
        db.session.commit()
        flash("Commande modifiée.")
        return redirect(url_for("commandes"))
    return render_template("modifier.html", c=c, partenaires=Partenaire.query.all())


@app.route("/commandes/<int:cid>/maj", methods=["POST"])
@admin_required
def maj_commande(cid):
    c = db.get_or_404(Commande, cid)
    c.livreur_id = to_int(request.form.get("livreur_id"))
    db.session.commit()
    changer_statut(c, request.form.get("statut"))
    return redirect(url_for("commandes"))


@app.route("/commandes/<int:cid>/supprimer", methods=["POST"])
@admin_required
def supprimer_commande(cid):
    db.session.delete(db.get_or_404(Commande, cid))
    db.session.commit()
    flash("Commande supprimée.")
    return redirect(url_for("commandes"))


# ---------- Espace livreur (web) ----------
@app.route("/mes-commandes")
@login_required
def mes_commandes():
    lid = session.get("livreur_id")
    if not lid:
        return redirect(url_for("commandes"))
    cmds = (Commande.query.filter_by(livreur_id=lid).filter(Commande.statut != "livré")
            .order_by(Commande.created_at).all())
    return render_template("mes_commandes.html", commandes=cmds, livreur_id=lid)


@app.route("/mes-commandes/<int:cid>/statut", methods=["POST"])
@login_required
def livreur_statut(cid):
    c = db.get_or_404(Commande, cid)
    if c.livreur_id != session.get("livreur_id"):
        abort(403)
    changer_statut(c, request.form.get("statut"))
    return redirect(url_for("mes_commandes"))


@app.route("/livreurs/<int:lid>/suivi")
@login_required
def suivi_livreur(lid):
    if not can_livreur(lid):
        abort(403)
    return render_template("suivi.html", livreur=db.get_or_404(Livreur, lid))


@app.route("/api/livreurs/<int:lid>/position", methods=["POST"])
@login_required
def maj_position(lid):
    if not can_livreur(lid):
        abort(403)
    l = db.get_or_404(Livreur, lid)
    d = request.get_json(silent=True) or {}
    l.lat, l.lng, l.vu_le = to_float(d.get("lat")), to_float(d.get("lng")), datetime.utcnow()
    db.session.commit()
    return jsonify(ok=True)


# ---------- Livreurs (admin) ----------
@app.route("/livreurs", methods=["GET", "POST"])
@admin_required
def livreurs():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        pwd = request.form.get("password") or ""
        if username and (not pwd or User.query.filter_by(username=username).first()):
            flash("Identifiant déjà pris ou mot de passe manquant.")
            return redirect(url_for("livreurs"))
        l = Livreur(nom=request.form["nom"], telephone=request.form["telephone"])
        db.session.add(l)
        db.session.flush()
        if username:
            db.session.add(User(username=username, password_hash=generate_password_hash(pwd),
                                role="livreur", livreur_id=l.id))
        db.session.commit()
        flash("Livreur ajouté.")
        return redirect(url_for("livreurs"))
    comptes = {u.livreur_id: u.username for u in User.query.filter_by(role="livreur").all()}
    return render_template("livreurs.html", livreurs=Livreur.query.all(), comptes=comptes)


@app.route("/livreurs/<int:lid>/dispo", methods=["POST"])
@admin_required
def dispo_livreur(lid):
    l = db.get_or_404(Livreur, lid)
    l.disponible = not l.disponible
    db.session.commit()
    return redirect(url_for("livreurs"))


@app.route("/livreurs/<int:lid>/supprimer", methods=["POST"])
@admin_required
def supprimer_livreur(lid):
    l = db.get_or_404(Livreur, lid)
    Commande.query.filter_by(livreur_id=lid).update({"livreur_id": None})
    User.query.filter_by(livreur_id=lid).delete()
    db.session.delete(l)
    db.session.commit()
    flash("Livreur supprimé.")
    return redirect(url_for("livreurs"))


# ---------- Partenaires (admin) ----------
@app.route("/partenaires", methods=["GET", "POST"])
@admin_required
def partenaires():
    if request.method == "POST":
        db.session.add(Partenaire(nom=request.form["nom"], telephone=request.form.get("telephone"),
                                  adresse=request.form.get("adresse")))
        db.session.commit()
        flash("Partenaire ajouté.")
        return redirect(url_for("partenaires"))
    stats = dict(db.session.query(Commande.partenaire_id, func.count(Commande.id))
                 .group_by(Commande.partenaire_id).all())
    return render_template("partenaires.html", partenaires=Partenaire.query.all(), stats=stats)


@app.route("/partenaires/<int:pid>/supprimer", methods=["POST"])
@admin_required
def supprimer_partenaire(pid):
    p = db.get_or_404(Partenaire, pid)
    Commande.query.filter_by(partenaire_id=pid).update({"partenaire_id": None})
    db.session.delete(p)
    db.session.commit()
    flash("Partenaire supprimé.")
    return redirect(url_for("partenaires"))


# ---------- Statistiques, carte, rapports ----------
@app.route("/dashboard")
@admin_required
def dashboard():
    total = Commande.query.count()
    par_statut = dict(db.session.query(Commande.statut, func.count(Commande.id))
                      .group_by(Commande.statut).all())
    ca = db.session.query(func.coalesce(func.sum(Commande.prix), 0)).filter_by(statut="livré").scalar()
    par_livreur = (db.session.query(Livreur.nom, func.count(Commande.id))
                   .join(Commande, Commande.livreur_id == Livreur.id).group_by(Livreur.nom).all())
    par_partenaire = (db.session.query(Partenaire.nom, func.count(Commande.id))
                      .join(Commande, Commande.partenaire_id == Partenaire.id)
                      .group_by(Partenaire.nom).all())
    depuis = datetime.utcnow() - timedelta(days=7)
    par_jour = (db.session.query(func.date(Commande.created_at), func.count(Commande.id))
                .filter(Commande.created_at >= depuis).group_by(func.date(Commande.created_at))
                .order_by(func.date(Commande.created_at)).all())
    par_jour = [(str(j), n) for j, n in par_jour]
    return render_template("dashboard.html", total=total, par_statut=par_statut, ca=ca,
                           par_livreur=par_livreur, par_partenaire=par_partenaire,
                           par_jour=par_jour, statuts=STATUTS)


@app.route("/carte")
@admin_required
def carte():
    livs = [dict(nom=l.nom, lat=l.lat, lng=l.lng) for l in Livreur.query.all() if l.lat and l.lng]
    cmds = [dict(id=c.id, client=c.client_nom, statut=c.statut, lat=c.lat, lng=c.lng)
            for c in Commande.query.filter(Commande.statut != "livré").all() if c.lat and c.lng]
    return render_template("carte.html", livs=livs, cmds=cmds)


@app.route("/rapports/export.csv")
@admin_required
def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "code", "date", "client", "telephone", "adresse", "prix", "statut",
                "livreur", "partenaire"])
    for c in Commande.query.order_by(Commande.id).all():
        w.writerow([c.id, c.code, c.created_at.strftime("%Y-%m-%d %H:%M"), c.client_nom,
                    c.client_tel, c.adresse, c.prix, c.statut,
                    c.livreur.nom if c.livreur else "", c.partenaire.nom if c.partenaire else ""])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=commandes.csv"})


# ---------- Suivi public pour le client ----------
@app.route("/suivi/<code>")
def suivi_public(code):
    c = Commande.query.filter_by(code=code.upper()).first_or_404()
    return render_template("suivi_public.html", c=c)


# ---------- API : site web de la startup (clé X-API-Key) ----------
def api_ok():
    cle = os.environ.get("API_KEY")
    return bool(cle) and request.headers.get("X-API-Key") == cle


@app.route("/api/commandes", methods=["POST"])
@csrf.exempt
def api_creer_commande():
    if not api_ok():
        return jsonify(erreur="clé API invalide"), 401
    d = request.get_json(silent=True) or {}
    if not all(d.get(k) for k in ("client_nom", "client_tel", "adresse")):
        return jsonify(erreur="client_nom, client_tel et adresse sont obligatoires"), 400
    c = Commande(client_nom=d["client_nom"], client_tel=d["client_tel"], adresse=d["adresse"],
                 prix=to_int(d.get("prix")) or 0, lat=to_float(d.get("lat")), lng=to_float(d.get("lng")))
    db.session.add(c)
    db.session.commit()
    return jsonify(id=c.id, code=c.code, statut=c.statut, suivi=request.host_url + "suivi/" + c.code), 201


@app.route("/api/commandes/<int:cid>")
def api_statut_commande(cid):
    if not api_ok():
        return jsonify(erreur="clé API invalide"), 401
    c = db.get_or_404(Commande, cid)
    return jsonify(id=c.id, statut=c.statut, livreur=c.livreur.nom if c.livreur else None)


# ---------- API : application mobile du livreur (jeton Bearer) ----------
def serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="livreur-api")


def livreur_api(f):
    @wraps(f)
    def wrapper(*a, **k):
        tok = request.headers.get("Authorization", "").replace("Bearer ", "")
        try:
            g.livreur_id = serializer().loads(tok, max_age=60 * 60 * 24 * 30)
        except (BadSignature, SignatureExpired):
            return jsonify(erreur="non autorisé"), 401
        return f(*a, **k)
    return wrapper


@app.route("/api/livreur/login", methods=["POST"])
@csrf.exempt
def api_livreur_login():
    d = request.get_json(silent=True) or {}
    u = User.query.filter_by(username=d.get("username", "")).first()
    if not u or u.role != "livreur" or not check_password_hash(u.password_hash, d.get("password", "")):
        return jsonify(erreur="identifiants invalides"), 401
    return jsonify(token=serializer().dumps(u.livreur_id), livreur_id=u.livreur_id)


@app.route("/api/livreur/commandes")
@livreur_api
def api_livreur_commandes():
    cmds = (Commande.query.filter_by(livreur_id=g.livreur_id).filter(Commande.statut != "livré")
            .order_by(Commande.created_at).all())
    return jsonify([dict(id=c.id, code=c.code, client_nom=c.client_nom, client_tel=c.client_tel,
                         adresse=c.adresse, prix=c.prix, statut=c.statut, lat=c.lat, lng=c.lng)
                    for c in cmds])


@app.route("/api/livreur/commandes/<int:cid>/statut", methods=["POST"])
@csrf.exempt
@livreur_api
def api_livreur_statut(cid):
    c = db.get_or_404(Commande, cid)
    if c.livreur_id != g.livreur_id:
        return jsonify(erreur="interdit"), 403
    changer_statut(c, (request.get_json(silent=True) or {}).get("statut"))
    return jsonify(id=c.id, statut=c.statut)


@app.route("/api/livreur/position", methods=["POST"])
@csrf.exempt
@livreur_api
def api_livreur_position():
    l = db.get_or_404(Livreur, g.livreur_id)
    d = request.get_json(silent=True) or {}
    l.lat, l.lng, l.vu_le = to_float(d.get("lat")), to_float(d.get("lng")), datetime.utcnow()
    db.session.commit()
    return jsonify(ok=True)


# ---------- Initialisation ----------
with app.app_context():
    db.create_all()
    if not User.query.first():
        db.session.add(User(username=os.environ.get("ADMIN_USER", "admin"), role="admin",
                            password_hash=generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123"))))
        db.session.commit()

if __name__ == "__main__":
    app.run(debug=True)
