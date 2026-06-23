# TrustDelivery — Plateforme de Gestion de Livraison, Stock & Commandes

Application web développée en **Python (Flask)** à partir du cahier des charges fourni.
Aucune dépendance lourde : base SQLite intégrée, un seul package externe (Flask).

## Installation

```bash
pip install -r requirements.txt
python app.py
```

Puis ouvrez **http://127.0.0.1:5000** dans votre navigateur.

## Application Android (APK WebView)

Le dossier `android-app/` contient une application Android native qui affiche
TrustDelivery dans une WebView sécurisée. Elle prend en charge les sessions, les
uploads caméra/galerie, la géolocalisation des livreurs, les téléchargements, le
bouton retour, l'actualisation par glissement et un écran hors connexion.

### Générer l'APK avec GitHub

1. Ouvrez l'onglet **Actions** du dépôt GitHub.
2. Sélectionnez **Build Android APK**, puis **Run workflow**.
3. Renseignez l'URL HTTPS publique du site, ou laissez-la vide pour que l'APK la
   demande au premier lancement.
4. Téléchargez l'artefact **TrustDelivery-Android** à la fin de l'exécution.

Le fichier contenu dans l'artefact se nomme `TrustDelivery.apk`. Android peut
demander d'autoriser l'installation d'applications provenant du navigateur ou du
gestionnaire de fichiers utilisé.

### Générer avec Android Studio

Ouvrez le dossier `android-app/`, attendez la synchronisation Gradle, puis utilisez
**Build > Build APK(s)**. Pour intégrer directement l'adresse du serveur :

```bash
gradle :app:assembleDebug -PTRUSTDELIVERY_URL=https://votre-site.com
```

Pour changer ultérieurement l'adresse mémorisée, effectuez un appui long dans
l'application et saisissez la nouvelle URL.

Sur la machine de développement configurée, une nouvelle compilation locale se
lance simplement avec :

```powershell
powershell -ExecutionPolicy Bypass -File .\build-android.ps1 -SiteUrl "https://votre-site.com"
```

Sans `-SiteUrl`, l'application demandera l'adresse du site au premier lancement.

## Notifications Android en arrière-plan

Les nouvelles affectations peuvent produire une notification Android sonore, même
si l'APK est fermé. Créez un projet Firebase, ajoutez l'application Android
`com.trustdelivery.mobile`, puis fournissez au build les valeurs Mobile App ID,
Web API Key, Project ID et Sender ID. Le serveur doit recevoir le compte de service
Firebase dans la variable secrète :

```text
FIREBASE_SERVICE_ACCOUNT_JSON={...JSON complet du compte de service...}
```

Le jeton de l'appareil est associé automatiquement au livreur après sa connexion
dans l'APK. Le compte de service ne doit jamais être ajouté au dépôt Git.

La base de données (`trustdelivery.db`) et les comptes de démonstration sont créés
automatiquement au premier démarrage.

## Compte Super Administrateur

Conformément à votre demande, le compte Super Administrateur appartient à
**Thierno Abdoul Keita** :

| Champ | Valeur |
|---|---|
| Email | `thierno.keita@trustdelivery.com` |
| Mot de passe | `TrustDelivery@2026` |

⚠️ **Changez ce mot de passe dès la première connexion** (menu "Mon profil").

## Comptes de démonstration (mot de passe : `Demo@2026`)

| Rôle | Email |
|---|---|
| Modérateur | moderateur@trustdelivery.com |
| Agent de confirmation | agent@trustdelivery.com |
| Livreur | livreur@trustdelivery.com |
| Client | client@trustdelivery.com |

Vous pouvez supprimer ces comptes de test depuis le module **Utilisateurs** une fois
en production.

## Fonctionnalités couvertes (cahier des charges)

- **Authentification & sécurité** : connexion par email/mot de passe, session sécurisée,
  mots de passe hashés (Werkzeug), journalisation des connexions.
- **Gestion des utilisateurs** : création, modification, suspension, suppression ;
  5 rôles (Super Administrateur, Modérateur, Agent de confirmation, Livreur, Client).
- **Tableau de bord** : taux de confirmation, taux de livraison réussie, taux de retour,
  chiffre d'affaires (graphique 7 jours), performance par zone, alertes de stock.
- **Produits & Stocks** : catalogue, stock par entrepôt, mouvements (entrées/sorties),
  alertes de rupture.
- **Commandes** : création (client ou agent), confirmation, affectation à un livreur,
  suivi de statut, annulation, retour — avec réajustement automatique du stock.
- **Boutiques connectées** : réception automatique des commandes Shopify, WooCommerce,
  PrestaShop, Meta/WhatsApp ou API personnalisée, déduplication et rapprochement par SKU.
- **Synchronisation des statuts** : WooCommerce passe à `completed`, Shopify à
  `fulfilled`, et les autres plateformes reçoivent un callback universel `delivered`
  après la livraison. Le destinataire est notifié par SMS/WhatsApp lors du dispatch.
- **Livraisons** : interface dédiée au livreur pour démarrer/clôturer ses livraisons.
- **Facturation** : génération automatique de la facture à la livraison, suivi des
  paiements, export comptable au format CSV.
- **Paramètres** : libellés et couleurs des statuts de commande, zones de livraison et
  frais associés, entrepôts.
- **Journal d'audit** : historique des actions sensibles (réservé au Super Administrateur).

## Architecture technique

- Backend : Flask (Python), organisé en *blueprints* par module métier.
- Base de données : SQLite (fichier `trustdelivery.db`), facilement migrable vers
  PostgreSQL si nécessaire pour un déploiement à plus grande échelle.
- Frontend : gabarits HTML (Jinja2) + Bootstrap 5 + Chart.js, identité visuelle reprenant
  les couleurs du logo TrustDelivery (bleu/orange).
- Aucune donnée sensible n'est codée en dur côté client ; tout passe par le serveur.

## Notes pour la mise en production

- Changez `SECRET_KEY` (variable d'environnement) avant tout déploiement public.
- Remplacez le serveur de développement Flask par un serveur WSGI de production
  (ex. Gunicorn) derrière Nginx, avec HTTPS/SSL — voir le cahier des charges.
- Si vous attendez un volume important d'utilisateurs/commandes simultanés, migrez la
  base SQLite vers PostgreSQL (la structure des requêtes reste très proche).
- Ne déployez jamais SQLite sans volume persistant. Sur Render, le dépôt utilise
  `/var/data/trustdelivery.db`. Sur Railway, créez un volume monté sur `/data` puis
  définissez `DATABASE_PATH=/data/trustdelivery.db`. L'application refuse désormais
  de démarrer sur un disque éphémère afin d'éviter un redéploiement destructeur.
- Une sauvegarde cohérente est créée automatiquement chaque jour dans le dossier
  `backups` du volume (10 versions conservées). Le Super Administrateur peut aussi
  télécharger une sauvegarde depuis **Paramètres**.

## Configuration des boutiques et notifications

1. Dans **Mes boutiques**, créez la boutique WooCommerce et copiez l'URL webhook.
2. Dans WooCommerce, créez un webhook sur l'événement de création de commande avec
   cette URL de livraison.
3. Créez une clé REST WooCommerce avec les droits lecture/écriture, puis renseignez
   l'URL de la boutique, la clé `ck_...` et le secret `cs_...` dans TrustDelivery.
4. Configurez l'un des canaux de notification suivants :

   - `NOTIFICATION_WEBHOOK_URL` pour un fournisseur SMS/WhatsApp ou une automatisation ;
   - `META_WHATSAPP_TOKEN` et `META_WHATSAPP_PHONE_NUMBER_ID` pour WhatsApp Cloud API ;
   - `META_GRAPH_API_VERSION` permet de remplacer la version Graph utilisée par défaut.

### Liaison WhatsApp par OTP

La vérification d'un numéro utilise un modèle d'authentification préalablement
approuvé dans WhatsApp Manager. Configurez les variables suivantes :

- `META_WHATSAPP_TOKEN` : jeton permanent WhatsApp Business Cloud API ;
- `META_WHATSAPP_PHONE_NUMBER_ID` : identifiant du numéro émetteur ;
- `META_WHATSAPP_OTP_TEMPLATE` : nom du modèle, `trustdelivery_otp` par défaut ;
- `META_WHATSAPP_OTP_LANGUAGE` : langue approuvée du modèle, `fr` par défaut ;
- `META_GRAPH_API_VERSION` : version Graph utilisée par l'intégration.

Le modèle d'authentification doit contenir le paramètre OTP dans le corps et un
bouton de copie du code. TrustDelivery ne conserve que le hachage du code, limite
les tentatives et impose un délai avant le renvoi.

Pour Shopify, renseignez l'URL de la boutique et placez le jeton Admin API dans le
champ **Secret ou jeton API**. `SHOPIFY_API_VERSION` permet de choisir la version de
l'Admin API ; la livraison crée automatiquement le fulfillment Shopify.

Pour PrestaShop, Facebook/Instagram, WhatsApp et toute autre boutique, renseignez une
URL **Callback de statut** fournie par le module de la boutique ou une automatisation
Make, Zapier ou n8n. TrustDelivery lui envoie :

```json
{
  "event": "order.delivered",
  "status": "delivered",
  "platform": "prestashop",
  "external_order_id": "ABC-123",
  "trustdelivery_order_id": 42
}
```

Le callback reçoit aussi `Authorization: Bearer ...` et `X-TrustDelivery-Event` pour
authentifier et router chaque évolution : `confirmed`, `assigned`,
`out_for_delivery`, `delivered`, `cancelled` ou `returned`.

Le webhook de notification reçoit un JSON contenant `channel`, `to`, `message` et
`order_id`. Une panne externe n'annule jamais une affectation ou une livraison : elle
est enregistrée dans les journaux pour pouvoir être corrigée.

## API partenaire, dispatch et GPS

Chaque boutique possède une clé dans **Mes boutiques**. L'import API utilise :

```http
POST /api/v1/commandes
Authorization: Bearer CLE_DE_LA_BOUTIQUE
Content-Type: application/json
```

Le corps reprend le format universel : `external_order_id`, coordonnées du
destinataire et `items` avec `sku` et `quantity`. Le statut et la dernière position GPS
sont disponibles avec `GET /api/v1/commandes/{external_order_id}` et la même clé.

Lorsque le dispatch automatique est activé, TrustDelivery vérifie et décrémente le
stock, privilégie un livreur de la même zone, puis choisit celui ayant le moins de
livraisons actives. Sans stock ou sans livreur actif, la commande reste en attente.

Le suivi GPS repose sur l'autorisation de géolocalisation du téléphone du livreur. Le
site doit donc être servi en HTTPS ; la position n'est acceptée que pour une commande
assignée et encore active, et reste invisible aux autres partenaires.
