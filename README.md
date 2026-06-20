# TrustDelivery — Plateforme de Gestion de Livraison, Stock & Commandes

Application web développée en **Python (Flask)** à partir du cahier des charges fourni.
Aucune dépendance lourde : base SQLite intégrée, un seul package externe (Flask).

## Installation

```bash
pip install -r requirements.txt
python app.py
```

Puis ouvrez **http://127.0.0.1:5000** dans votre navigateur.

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
