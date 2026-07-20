# Surveillance de disponibilités Maiia / Doctolib

Petit logiciel qui surveille la page d'un ou plusieurs praticiens sur **Maiia** et
**Doctolib**, et envoie un **e-mail dès que des créneaux de rendez-vous en ligne
s'ouvrent** (ou se referment).

Quand une page Maiia affiche « *Le praticien n'a plus de disponibilité en ligne
actuellement* », ou qu'une page Doctolib affiche « *Désolé, ce soignant n'est pas
réservable en ligne* », le script le détecte via les API des deux plateformes et
vous prévient au moment précis où la situation change.

## Confidentialité (conception du dépôt)

Ce dépôt est conçu pour être **public sans révéler aucune donnée personnelle** :

- la **liste des praticiens surveillés** n'est jamais committée : elle vit dans le
  secret GitHub `PRACTITIONERS_JSON` (ou dans `practitioners.json` en local, qui
  est ignoré par git) ;
- l'**état de surveillance** (`state.json`) est stocké dans le cache privé de
  GitHub Actions, jamais dans le dépôt ;
- les **logs d'exécution** (publics sur un dépôt public) n'affichent ni nom ni
  URL de praticien — uniquement « praticien 1/3 », l'état et le nombre de
  créneaux ;
- les **identifiants e-mail** vivent dans les secrets GitHub (ou `.env` en local,
  ignoré par git).

## Fonctionnement

1. Le script lit la liste des praticiens (secret `PRACTITIONERS_JSON` ou fichier
   local `practitioners.json`).
2. Pour chacun, il interroge l'API de la plateforme :
   - **Maiia** : les identifiants techniques sont résolus automatiquement depuis
     l'URL de la fiche, puis l'API `availability-closests` donne le nombre de
     créneaux ouverts.
   - **Doctolib** : l'API du tunnel de réservation indique si la prise de
     rendez-vous en ligne est ouverte, puis `availabilities.json` compte les
     créneaux réels. Tout est automatique à partir de l'URL de la page.
3. Trois états sont distingués — **fermé**, **réservation ouverte sans créneau
   visible**, **créneaux disponibles** — et chaque transition déclenche un
   e-mail. L'état est mémorisé : jamais d'e-mail en double.

Aucune dépendance à installer : Python 3.9+ et sa bibliothèque standard suffisent.

## Format de la liste des praticiens

Un tableau JSON ; une entrée = un praticien, l'URL de sa page suffit
(voir `practitioners.example.json`) :

```json
[
  {
    "label": "Dr Prénom NOM (psychiatre, Ville)",
    "platform": "maiia",
    "url": "https://www.maiia.com/psychiatre/00000-ville/prenom-nom"
  },
  {
    "label": "Dr Prénom NOM (psychiatre, Ville)",
    "platform": "doctolib",
    "url": "https://www.doctolib.fr/psychiatre/ville/prenom-nom/booking"
  }
]
```

Le champ `label` est libre : c'est le nom qui apparaîtra dans les e-mails
d'alerte (et uniquement là).

## Déploiement sur GitHub Actions

La surveillance tourne dans le cloud toutes les 30 minutes, même PC éteint.

1. Pousser ce projet dans un dépôt GitHub.
2. Dans le dépôt : **Settings → Secrets and variables → Actions →
   New repository secret**, créer :

   | Secret | Contenu |
   |---|---|
   | `PRACTITIONERS_JSON` | Le tableau JSON des praticiens (format ci-dessus) |
   | `SMTP_USER` | Adresse Gmail expéditrice |
   | `SMTP_PASSWORD` | Mot de passe d'application Gmail |
   | `SMTP_HOST`, `SMTP_PORT`, `MAIL_TO` | Facultatifs (défauts : smtp.gmail.com, 465, SMTP_USER) |

   Pour Gmail : activer la validation en 2 étapes puis créer un « mot de passe
   d'application » sur <https://myaccount.google.com/apppasswords>.
3. C'est tout : le workflow `.github/workflows/monitor.yml` s'exécute toutes les
   30 minutes. On peut le lancer à la main depuis l'onglet **Actions**
   (bouton *Run workflow*) pour tester.

> ℹ️ GitHub désactive les tâches planifiées après 60 jours sans commit : le
> workflow committe donc une fois par jour un fichier témoin anonyme
> (`.keepalive`) pour maintenir le dépôt actif.

## Test en local

```bash
cp practitioners.example.json practitioners.json   # puis renseigner vos praticiens
cp .env.example .env                               # puis renseigner vos identifiants
python monitor.py
```

`practitioners.json`, `state.json` et `.env` sont ignorés par git : rien de
personnel ne peut être committé par accident.

## Limites et bonnes pratiques

- Le script s'appuie sur les API publiques non documentées de Maiia et Doctolib :
  si elles évoluent, il faudra adapter `monitor.py` (la logique est courte et
  commentée).
- Fréquence de 30 minutes : suffisante et respectueuse des services.
  Éviter de descendre sous 10 minutes.
- La première exécution enregistre l'état initial sans envoyer d'e-mail ; les
  alertes partent à partir du premier changement détecté.
