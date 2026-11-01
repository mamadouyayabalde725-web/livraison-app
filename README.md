# Plateforme de gestion de livraisons

Backend Flask + PostgreSQL, interface web, carte OpenStreetMap, API pour le site web, application Android Flutter.

## Fonctions
- Commandes : créer, modifier, supprimer, filtrer par statut, code de suivi client
- Livreurs : ajout, disponibilité, compte de connexion, suivi GPS
- Partenaires : ajout, suppression, commandes par partenaire
- Rapports : statistiques (jour, livreur, partenaire, chiffre d'affaires), export CSV
- Carte en quasi temps réel (OpenStreetMap + Leaflet)
- Page de suivi pour le client : `/suivi/<code>`
- SMS : message envoyé à chaque changement de statut (voir plus bas)
- Rôles : `admin` (tout) et `livreur` (ses livraisons seulement)

## Lancer sur PC
1. `pip install -r requirements.txt`
2. Supprime le dossier `instance/` si tu as une ancienne version
3. `python app.py` puis ouvre http://127.0.0.1:5000 (admin / admin123, à changer)

## Variables d'environnement
| Nom | Rôle |
|---|---|
| SECRET_KEY | clé secrète (obligatoire en ligne) |
| DATABASE_URL | PostgreSQL (SQLite par défaut en local) |
| ADMIN_USER / ADMIN_PASSWORD | compte admin créé au premier démarrage |
| API_KEY | clé pour l'API du site web (API désactivée si absente) |
| SMS_API_URL / SMS_API_KEY | service SMS (sans cela, les SMS sont seulement écrits dans les logs) |

## API pour le site web (en-tête `X-API-Key`)
- `POST /api/commandes` — JSON : `client_nom`, `client_tel`, `adresse`, `prix`, `lat`, `lng` → renvoie `id`, `code`, `suivi`
- `GET /api/commandes/<id>` — statut et livreur

## SMS
`notifier_client()` envoie en POST JSON `{"to": ..., "message": ...}` à `SMS_API_URL`. Chaque fournisseur (Twilio, Orange, etc.) a son format : adapte cette fonction au service choisi.

## Mise en ligne sur Render
1. Mets le dossier sur GitHub.
2. Render > New > Blueprint, choisis le dépôt (`render.yaml`).
3. Renseigne `ADMIN_PASSWORD`. Ajoute `SMS_API_URL` / `SMS_API_KEY` si besoin.
4. Le plan gratuit s'endort et sa base expire : prends un plan payant pour un client réel.

## Application Android (dossier `flutter_app`)
1. Installe Flutter, puis dans `flutter_app` : `flutter create .`
2. Dans `android/app/src/main/AndroidManifest.xml`, ajoute avant `<application>` :
   `<uses-permission android:name="android.permission.INTERNET"/>`
   `<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION"/>`
3. Dans `lib/main.dart`, remplace `baseUrl` par l'adresse de ton application en ligne.
4. `flutter run` (ou `flutter build apk`).
Le livreur se connecte avec le compte créé dans la page Livreurs, voit ses livraisons, change les statuts, et sa position est envoyée toutes les 20 secondes.

## Pistes d'amélioration
Tests automatiques, multi-agences, paiement mobile, preuve de livraison (photo/signature), notifications push.
