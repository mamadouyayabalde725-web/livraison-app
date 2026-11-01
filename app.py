import base64
import csv
import io
import json
import os
import re
import secrets
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode
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
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
db = SQLAlchemy(app)
csrf = CSRFProtect(app)

# Pages HTML intégrées (un seul fichier à envoyer)
from jinja2 import DictLoader
TEMPLATES = {'base.html': '<!doctype html>\n<html lang="fr"><head>\n<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n<title>Gestion Livraisons</title>\n<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">\n<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>\n<style>\nbody{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#222}\nnav{background:#1f3a5f;padding:12px;display:flex;gap:16px;flex-wrap:wrap}\nnav a{color:#fff;text-decoration:none;font-weight:600}\nmain{padding:16px;max-width:900px;margin:auto}\n.card{background:#fff;border-radius:8px;padding:12px;margin-bottom:12px;box-shadow:0 1px 3px #0002}\ninput,select,button{padding:8px;margin:4px 0;font-size:16px;max-width:100%}\nbutton{background:#1f3a5f;color:#fff;border:0;border-radius:6px;cursor:pointer}\nbutton.danger{background:#a33}\n.flash{background:#e7f5e7;padding:8px;border-radius:6px;margin-bottom:12px}\n.bar{background:#1f3a5f;color:#fff;padding:2px 6px;border-radius:4px;margin:2px 0;white-space:nowrap}\n.tag{padding:2px 8px;border-radius:10px;font-size:13px;background:#ddd}\n</style></head><body>\n{% if session.user_id %}<nav>\n{% if session.role == \'admin\' %}\n<a href="{{ url_for(\'commandes\') }}">Commandes</a>\n<a href="{{ url_for(\'nouvelle_commande\') }}">+ Commande</a>\n<a href="{{ url_for(\'livreurs\') }}">Livreurs</a>\n<a href="{{ url_for(\'partenaires\') }}">Partenaires</a>\n<a href="{{ url_for(\'dashboard\') }}">Stats</a>\n<a href="{{ url_for(\'carte\') }}">Carte</a>\n{% else %}<a href="{{ url_for(\'mes_commandes\') }}">Mes commandes</a>{% endif %}\n<a href="{{ url_for(\'logout\') }}">Déconnexion</a></nav>{% endif %}\n<main>\n{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}\n{% block content %}{% endblock %}\n</main></body></html>\n', 'carte.html': '{% extends "base.html" %}{% block content %}\n<h2>Carte (mise à jour auto)</h2>\n<div id="map" style="height:70vh"></div>\n<p>🔵 livreurs · 🔴 commandes en cours</p>\n<script>\nvar map=L.map(\'map\').setView([9.6412,-13.5784],12);\nL.tileLayer(\'https://tile.openstreetmap.org/{z}/{x}/{y}.png\',{attribution:\'© OpenStreetMap\'}).addTo(map);\nvar livs={{ livs|tojson }}, cmds={{ cmds|tojson }};\nlivs.forEach(function(l){L.marker([l.lat,l.lng]).addTo(map).bindPopup(l.nom);});\ncmds.forEach(function(c){L.circleMarker([c.lat,c.lng],{color:\'red\'}).addTo(map).bindPopup(\'#\'+c.id+\' \'+c.client+\' — \'+c.statut);});\nsetTimeout(function(){location.reload();},15000);\n</script>{% endblock %}\n', 'commande_ok.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Commande enregistrée ✔</h2>\n<p>Votre code de suivi : <b>{{ c.code }}</b></p>\n<p><a href="{{ url_for(\'suivi_public\', code=c.code) }}">Suivre ma commande</a></p></div>{% endblock %}\n', 'commander.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Passer une commande</h2>\n<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<input name="client_nom" placeholder="Votre nom" required><br>\n<input name="client_tel" placeholder="Votre téléphone" required><br>\n<input name="adresse" placeholder="Adresse de livraison" required><br>\n<input name="site" style="display:none" tabindex="-1" autocomplete="off">\n<button>Envoyer la commande</button></form></div>{% endblock %}\n', 'commandes.html': '{% extends "base.html" %}{% block content %}\n<h2>Commandes</h2>\n<p><a href="{{ url_for(\'commandes\') }}">Toutes</a>\n{% for s in statuts %} | <a href="{{ url_for(\'commandes\', statut=s) }}">{{ s }}</a>{% endfor %}\n| <a href="{{ url_for(\'export_csv\') }}">Exporter CSV</a></p>\n{% for c in commandes %}\n<div class="card">\n<b>#{{ c.id }} {{ c.client_nom }}</b> ({{ c.client_tel }}) · code <code>{{ c.code }}</code><br>\n{{ c.adresse }} — {{ c.prix }} GNF{% if c.partenaire %} · {{ c.partenaire.nom }}{% endif %}<br>\n<span class="tag">{{ c.statut }}</span>{% if c.livreur %} Livreur : {{ c.livreur.nom }}{% endif %}\n<form method="post" action="{{ url_for(\'maj_commande\', cid=c.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<select name="statut">{% for s in statuts %}\n<option value="{{ s }}" {{ "selected" if s == c.statut }}>{{ s }}</option>{% endfor %}</select>\n<select name="livreur_id"><option value="">— livreur —</option>\n{% for l in livreurs %}<option value="{{ l.id }}" {{ "selected" if l.id == c.livreur_id }}>{{ l.nom }}</option>{% endfor %}</select>\n<button>Mettre à jour</button></form>\n<a href="{{ url_for(\'modifier_commande\', cid=c.id) }}">Modifier</a> ·\n<a href="{{ url_for(\'suivi_public\', code=c.code) }}">Lien client</a> ·\n<a href="{{ wa_link(c) }}" target="_blank">WhatsApp client</a>\n<form method="post" action="{{ url_for(\'supprimer_commande\', cid=c.id) }}" style="display:inline" onsubmit="return confirm(\'Supprimer cette commande ?\')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<button class="danger">Supprimer</button></form></div>\n{% else %}<p>Aucune commande.</p>{% endfor %}{% endblock %}\n', 'dashboard.html': '{% extends "base.html" %}{% block content %}\n<h2>Statistiques</h2>\n<p><a href="{{ url_for(\'export_csv\') }}">Télécharger le rapport (CSV)</a></p>\n<div class="card"><b>Total commandes :</b> {{ total }}<br>\n{% for s in statuts %}<span class="tag">{{ s }} : {{ par_statut.get(s, 0) }}</span> {% endfor %}<br><br>\n<b>Chiffre d\'affaires (livré) :</b> {{ ca }} GNF</div>\n{% macro barres(titre, data) %}\n<div class="card"><b>{{ titre }}</b>\n{% for nom, n in data %}<div class="bar" style="width:{{ [n*40, 20]|max }}px">{{ nom }} : {{ n }}</div>\n{% else %}<p>Pas de données.</p>{% endfor %}</div>\n{% endmacro %}\n{{ barres("7 derniers jours", par_jour) }}\n{{ barres("Par livreur", par_livreur) }}\n{{ barres("Par partenaire", par_partenaire) }}\n{% endblock %}\n', 'livreurs.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Ajouter un livreur</h2>\n<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<input name="nom" placeholder="Nom" required>\n<input name="telephone" placeholder="Téléphone" required><br>\n<small>Compte de connexion (optionnel) :</small><br>\n<input name="username" placeholder="Identifiant">\n<input name="password" type="password" placeholder="Mot de passe"><br>\n<button>Ajouter</button></form></div>\n{% for l in livreurs %}\n<div class="card"><b>{{ l.nom }}</b> — {{ l.telephone }}\n<span class="tag">{{ "disponible" if l.disponible else "indisponible" }}</span>\n{% if comptes.get(l.id) %}<span class="tag">compte : {{ comptes[l.id] }}</span>{% endif %}<br>\n<form method="post" action="{{ url_for(\'dispo_livreur\', lid=l.id) }}" style="display:inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button>Changer dispo</button></form>\n<form method="post" action="{{ url_for(\'supprimer_livreur\', lid=l.id) }}" style="display:inline" onsubmit="return confirm(\'Supprimer ce livreur ?\')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Supprimer</button></form></div>\n{% else %}<p>Aucun livreur.</p>{% endfor %}{% endblock %}\n', 'login.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Connexion</h2>\n<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<input name="username" placeholder="Nom d\'utilisateur" required><br>\n<input name="password" type="password" placeholder="Mot de passe" required><br>\n<button>Se connecter</button></form></div>\n<p><a href="{{ url_for(\'suivre\') }}">Suivre une commande</a> · <a href="{{ url_for(\'commander\') }}">Passer une commande</a></p>{% endblock %}\n', 'mes_commandes.html': '{% extends "base.html" %}{% block content %}\n<h2>Mes livraisons</h2>\n<p><a href="{{ url_for(\'suivi_livreur\', lid=livreur_id) }}">📍 Partager ma position</a></p>\n{% for c in commandes %}\n<div class="card"><b>#{{ c.id }} {{ c.client_nom }}</b><br>\n<a href="tel:{{ c.client_tel }}">{{ c.client_tel }}</a> · <a href="{{ wa_link(c) }}" target="_blank">WhatsApp</a><br>\n{{ c.adresse }} — {{ c.prix }} GNF<br>\n{% if c.lat and c.lng %}<a href="https://www.openstreetmap.org/?mlat={{ c.lat }}&mlon={{ c.lng }}#map=17/{{ c.lat }}/{{ c.lng }}">Voir sur la carte</a><br>{% endif %}\n<span class="tag">{{ c.statut }}</span>\n<form method="post" action="{{ url_for(\'livreur_statut\', cid=c.id) }}" style="display:inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<input type="hidden" name="statut" value="en cours"><button>Démarrer</button></form>\n<form method="post" action="{{ url_for(\'livreur_statut\', cid=c.id) }}" style="display:inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<input type="hidden" name="statut" value="livré"><button>Livré ✔</button></form></div>\n{% else %}<p>Aucune livraison à faire.</p>{% endfor %}{% endblock %}\n', 'modifier.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Modifier la commande #{{ c.id }}</h2>\n<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<input name="client_nom" value="{{ c.client_nom }}" required><br>\n<input name="client_tel" value="{{ c.client_tel }}" required><br>\n<input name="adresse" value="{{ c.adresse }}" required><br>\n<input name="prix" type="number" min="0" value="{{ c.prix }}"><br>\n<select name="partenaire_id"><option value="">— partenaire —</option>\n{% for p in partenaires %}<option value="{{ p.id }}" {{ "selected" if p.id == c.partenaire_id }}>{{ p.nom }}</option>{% endfor %}</select><br>\n<button>Enregistrer</button></form></div>{% endblock %}\n', 'nouvelle.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Nouvelle commande</h2>\n<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<input name="client_nom" placeholder="Nom du client" required><br>\n<input name="client_tel" placeholder="Téléphone" required><br>\n<input name="adresse" placeholder="Adresse de livraison" required><br>\n<input name="prix" type="number" min="0" placeholder="Prix (GNF)"><br>\n<select name="partenaire_id"><option value="">— partenaire (optionnel) —</option>\n{% for p in partenaires %}<option value="{{ p.id }}">{{ p.nom }}</option>{% endfor %}</select><br>\n<input type="hidden" name="lat" id="lat"><input type="hidden" name="lng" id="lng">\n<p>Touche la carte pour placer la destination (optionnel) :</p>\n<div id="map" style="height:260px"></div>\n<button>Enregistrer</button></form></div>\n<script>\nvar map=L.map(\'map\').setView([9.6412,-13.5784],12);\nL.tileLayer(\'https://tile.openstreetmap.org/{z}/{x}/{y}.png\',{attribution:\'© OpenStreetMap\'}).addTo(map);\nvar m;map.on(\'click\',function(e){if(m)m.setLatLng(e.latlng);else m=L.marker(e.latlng).addTo(map);\ndocument.getElementById(\'lat\').value=e.latlng.lat;document.getElementById(\'lng\').value=e.latlng.lng;});\n</script>{% endblock %}\n', 'partenaires.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Ajouter un partenaire</h2>\n<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<input name="nom" placeholder="Nom (boutique, entreprise)" required><br>\n<input name="telephone" placeholder="Téléphone"><br>\n<input name="adresse" placeholder="Adresse"><br>\n<button>Ajouter</button></form></div>\n{% for p in partenaires %}\n<div class="card"><b>{{ p.nom }}</b> — {{ p.telephone or "" }}<br>{{ p.adresse or "" }}<br>\n<span class="tag">{{ stats.get(p.id, 0) }} commande(s)</span>\n<form method="post" action="{{ url_for(\'supprimer_partenaire\', pid=p.id) }}" onsubmit="return confirm(\'Supprimer ?\')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="danger">Supprimer</button></form></div>\n{% else %}<p>Aucun partenaire.</p>{% endfor %}{% endblock %}\n', 'suivi.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Position de {{ livreur.nom }}</h2>\n<p id="etat">Appuie sur le bouton et autorise la localisation.</p>\n<button onclick="demarrer()">Partager ma position</button></div>\n<script>\nfunction demarrer(){\n if(!navigator.geolocation){document.getElementById(\'etat\').textContent=\'GPS non disponible\';return;}\n navigator.geolocation.watchPosition(function(p){\n  fetch(\'{{ url_for("maj_position", lid=livreur.id) }}\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\',\'X-CSRFToken\':\'{{ csrf_token() }}\'},\n   body:JSON.stringify({lat:p.coords.latitude,lng:p.coords.longitude})});\n  document.getElementById(\'etat\').textContent=\'Position envoyée : \'+new Date().toLocaleTimeString();\n },function(e){document.getElementById(\'etat\').textContent=\'Erreur : \'+e.message;},{enableHighAccuracy:true});\n}\n</script>{% endblock %}\n', 'suivi_public.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Commande #{{ c.id }}</h2>\n<p>Statut : <span class="tag">{{ c.statut }}</span></p>\n{% if c.livreur %}<p>Livreur : {{ c.livreur.nom }}</p>{% endif %}</div>\n<div id="map" style="height:50vh"></div>\n<script>\nvar map=L.map(\'map\').setView([9.6412,-13.5784],13);\nL.tileLayer(\'https://tile.openstreetmap.org/{z}/{x}/{y}.png\',{attribution:\'© OpenStreetMap\'}).addTo(map);\nvar dest={{ ([c.lat, c.lng]|tojson) if (c.lat and c.lng) else \'null\' }};\nvar liv={{ ([c.livreur.lat, c.livreur.lng]|tojson) if (c.livreur and c.livreur.lat and c.statut == \'en cours\') else \'null\' }};\nif(dest){L.circleMarker(dest,{color:\'red\'}).addTo(map).bindPopup(\'Destination\');map.setView(dest,14);}\nif(liv){L.marker(liv).addTo(map).bindPopup(\'Livreur\');}\nsetTimeout(function(){location.reload();},20000);\n</script>{% endblock %}\n', 'suivre.html': '{% extends "base.html" %}{% block content %}\n<div class="card"><h2>Suivre ma commande</h2>\n<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">\n<input name="code" placeholder="Code de suivi (ex. 1A2B3C4D)" required><br>\n<button>Suivre</button></form></div>{% endblock %}\n'}
app.jinja_loader = DictLoader(TEMPLATES)

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


def numero_international(tel):
    chiffres = re.sub(r"\D", "", tel or "")
    return "224" + chiffres if len(chiffres) == 9 else chiffres  # Guinée par défaut


def envoyer_sms(tel, msg):
    """Twilio si TWILIO_SID / TWILIO_TOKEN / TWILIO_FROM sont définis, sinon service HTTP générique."""
    sid, token, exp = (os.environ.get(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"))
    if sid and token and exp:
        data = urlencode({"To": "+" + numero_international(tel), "From": exp, "Body": msg}).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
        urllib.request.urlopen(req, timeout=8)
        return
    url = os.environ.get("SMS_API_URL")
    if url:
        req = urllib.request.Request(
            url, data=json.dumps({"to": tel, "message": msg}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + os.environ.get("SMS_API_KEY", "")})
        urllib.request.urlopen(req, timeout=8)


def notifier_client(c):
    """SMS au client à chaque changement de statut. Sans configuration : simple message dans les logs."""
    msg = f"Votre commande #{c.id} est maintenant : {c.statut}. Suivi : {request.host_url}suivi/{c.code}"
    app.logger.info("SMS -> %s : %s", c.client_tel, msg)
    try:
        envoyer_sms(c.client_tel, msg)
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


@app.template_global()
def wa_link(c):
    """Lien WhatsApp pré-rempli pour prévenir le client (gratuit, sans fournisseur SMS)."""
    chiffres = re.sub(r"\D", "", c.client_tel or "")
    if len(chiffres) == 9:  # numéro guinéen sans indicatif
        chiffres = "224" + chiffres
    texte = (f"Bonjour {c.client_nom}, votre commande #{c.id} est : {c.statut}. "
             f"Suivi : {request.host_url}suivi/{c.code}")
    return f"https://wa.me/{chiffres}?text={quote(texte)}"


@app.route("/suivre", methods=["GET", "POST"])
def suivre():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        if code and Commande.query.filter_by(code=code).first():
            return redirect(url_for("suivi_public", code=code))
        flash("Code introuvable.")
    return render_template("suivre.html")


@app.route("/commander", methods=["GET", "POST"])
def commander():
    if request.method == "POST":
        if request.form.get("site"):  # champ piège contre les robots
            return redirect(url_for("commander"))
        nom = (request.form.get("client_nom") or "").strip()
        tel = (request.form.get("client_tel") or "").strip()
        adresse = (request.form.get("adresse") or "").strip()
        if not (nom and tel and adresse):
            flash("Merci de remplir tous les champs.")
            return redirect(url_for("commander"))
        c = Commande(client_nom=nom, client_tel=tel, adresse=adresse)
        db.session.add(c)
        db.session.commit()
        return render_template("commande_ok.html", c=c)
    return render_template("commander.html")


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
