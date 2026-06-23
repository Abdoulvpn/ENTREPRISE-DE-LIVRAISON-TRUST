from flask import Blueprint, g, render_template

from auth import login_required


bp = Blueprint("help", __name__, url_prefix="/aide")


COMMON_HELP = [
    {
        "icon": "bi-shield-check",
        "title": "Votre compte et votre sécurité",
        "description": "Modifiez vos informations et votre mot de passe depuis Mon profil. Déconnectez-vous après une utilisation sur un appareil partagé.",
        "steps": ["Ouvrez Mon profil dans le menu", "Mettez à jour vos coordonnées", "Utilisez un mot de passe unique"],
    },
    {
        "icon": "bi-palette",
        "title": "Personnaliser l’affichage",
        "description": "Le bouton d’apparence permet de passer du mode clair au mode sombre. Votre choix est conservé sur cet appareil.",
        "steps": ["Cliquez sur Clair ou Sombre dans le menu", "Le thème change immédiatement", "Le réglage sera repris à votre prochaine visite"],
    },
]


ROLE_HELP = {
    "super_admin": [
        ("bi-speedometer2", "Piloter l’activité", "Analysez les commandes, revenus, profits, livreurs et performances avec les filtres du tableau de bord."),
        ("bi-people", "Administrer les utilisateurs", "Créez les comptes, attribuez les rôles, managers, types de clients et livreurs parents."),
        ("bi-box-seam", "Gérer les stocks", "Validez les envois, créez les produits, corrigez le stock et consultez l’historique des mouvements."),
        ("bi-journal-text", "Contrôler les actions", "Le journal d’audit retrace les opérations sensibles effectuées sur la plateforme."),
        ("bi-gear", "Configurer la plateforme", "Adaptez les zones, frais, entrepôts et statuts depuis Paramètres."),
    ],
    "moderateur": [
        ("bi-speedometer2", "Suivre l’exploitation", "Utilisez le tableau de bord et ses filtres pour suivre les résultats opérationnels."),
        ("bi-box-seam", "Gérer produits et stocks", "Créez des produits, validez les envois clients et suivez le stock confié aux livreurs."),
        ("bi-people", "Gérer les comptes autorisés", "Créez et mettez à jour les utilisateurs selon les permissions de votre rôle."),
        ("bi-truck", "Superviser les livraisons", "Confirmez les commandes et affectez-les aux livreurs disponibles."),
    ],
    "agent_confirmation": [
        ("bi-telephone-check", "Confirmer les commandes", "Vérifiez les informations du destinataire puis confirmez ou qualifiez chaque commande."),
        ("bi-truck", "Affecter un livreur", "Après confirmation, choisissez un livreur disponible et suivez la prise en charge."),
        ("bi-boxes", "Consulter le stock", "Vérifiez la disponibilité des produits visibles Seller et le stock confié aux livreurs."),
    ],
    "livreur": [
        ("bi-truck", "Gérer vos livraisons", "Consultez uniquement les missions qui vous sont proposées ou affectées."),
        ("bi-check2-circle", "Mettre à jour une mission", "Acceptez la proposition, démarrez la livraison puis indiquez le résultat réel."),
        ("bi-geo-alt", "Partager votre position", "Pendant une livraison active, autorisez la géolocalisation pour permettre le suivi en temps réel."),
        ("bi-receipt", "Consulter vos données", "Votre tableau de bord résume vos livraisons actives, livrées et retournées."),
    ],
    "client": [
        ("bi-bag-check", "Suivre vos commandes", "Vous voyez uniquement vos propres commandes et leur progression jusqu’à la livraison."),
        ("bi-send", "Déclarer un envoi produit", "Ajoutez la référence, la quantité, la description et une photo. Le produit rejoint votre stock après validation."),
        ("bi-box-seam", "Consulter votre stock", "Suivez les quantités initiales, endommagées, livrées et restantes de vos produits."),
        ("bi-shop", "Connecter une boutique", "Reliez votre boutique e-commerce pour importer automatiquement les commandes."),
        ("bi-receipt", "Suivre vos factures", "Consultez vos factures et leur état de paiement depuis Mes factures."),
    ],
}


@bp.route("/")
@login_required
def index():
    role = g.user["role"]
    role_cards = [
        {"icon": icon, "title": title, "description": description}
        for icon, title, description in ROLE_HELP.get(role, [])
    ]
    return render_template("help.html", role_cards=role_cards, common_help=COMMON_HELP)
