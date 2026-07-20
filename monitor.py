#!/usr/bin/env python3
"""Surveillance des disponibilités de praticiens sur Maiia et Doctolib.

Pour chaque praticien listé dans practitioners.json (une simple URL suffit),
le script :
  1. interroge l'API publique de la plateforme :
     - Maiia : availability-closests (créneaux ouverts ou non) ;
     - Doctolib : slot_selection_funnel/v1/info.json (réservation ouverte ou
       fermée), puis availabilities.json (créneaux réels) quand elle est ouverte ;
  2. compare avec l'état précédent (state.json) et envoie un e-mail à chaque
     changement d'état : réservation rouverte, créneaux disponibles, fermeture.

Aucune dépendance externe : uniquement la bibliothèque standard Python (>= 3.9).
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # zoneinfo absent : on retombe sur l'heure locale
    PARIS = None

BASE_DIR = Path(__file__).resolve().parent
PRACTITIONERS_FILE = BASE_DIR / "practitioners.json"
STATE_FILE = BASE_DIR / "state.json"

API_BASE = "https://www.maiia.com/api/pat-public"
DOCTOLIB_BASE = "https://www.doctolib.fr"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) surveillance-psy/1.0"

# Fenêtre de recherche des créneaux : de maintenant à +90 jours.
SEARCH_DAYS = 90


# --------------------------------------------------------------------------- #
# Utilitaires HTTP / configuration
# --------------------------------------------------------------------------- #

def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def http_get_json(url: str) -> tuple[int, dict]:
    """GET JSON en renvoyant (code HTTP, données), y compris pour les erreurs 4xx."""
    try:
        return 200, json.loads(http_get(url))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {}


def load_dotenv() -> None:
    """Charge un éventuel fichier .env (clé=valeur) sans écraser l'environnement."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def now_paris() -> datetime:
    return datetime.now(PARIS) if PARIS else datetime.now()


# --------------------------------------------------------------------------- #
# API Maiia
# --------------------------------------------------------------------------- #

def resolve_ids(profile_url: str) -> dict:
    """Extrait practitionerId, centerId et le nom depuis la page publique Maiia."""
    html = http_get(profile_url).decode("utf-8", errors="replace")
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        raise RuntimeError("__NEXT_DATA__ introuvable sur la fiche Maiia")

    data = json.loads(match.group(1))
    items = data["props"]["pageProps"]["cards"]["items"]
    if not items:
        raise RuntimeError("aucune fiche praticien trouvée sur la page Maiia")

    card = items[0]
    professional = card.get("professional", {})
    name = f'{professional.get("firstName", "")} {professional.get("lastName", "")}'.strip()
    return {
        "name": name or profile_url,
        "practitioner_id": card["professional"]["practitionerId"],
        "center_id": card["center"]["id"],
    }


def fetch_availability(center_id: str, practitioner_id: str) -> dict:
    """Retourne le nombre de créneaux ouverts et la liste des prochains créneaux."""
    start = now_paris().replace(tzinfo=None)
    end = start + timedelta(days=SEARCH_DAYS)
    fmt = "%Y-%m-%dT%H:%M:%S"

    common = urllib.parse.urlencode(
        {"centerId": center_id, "practitionerId": practitioner_id}
    )

    closest = json.loads(
        http_get(f"{API_BASE}/availability-closests?{common}&from={start.strftime(fmt)}")
    )
    count = int(closest.get("availabilityCount", 0))

    slots: list[str] = []
    if count > 0:
        listing = json.loads(
            http_get(
                f"{API_BASE}/availabilities?{common}"
                f"&from={start.strftime(fmt)}&to={end.strftime(fmt)}&page=0&limit=10"
            )
        )
        for item in listing.get("items", []):
            slot = (
                item.get("startDateTime")
                or item.get("startDate")
                or json.dumps(item, ensure_ascii=False)
            )
            slots.append(str(slot))

    return {"count": count, "slots": slots}


# --------------------------------------------------------------------------- #
# API Doctolib
# --------------------------------------------------------------------------- #

def doctolib_slug(profile_url: str) -> str:
    """Extrait le slug du praticien depuis n'importe quelle URL Doctolib.

    Exemples acceptés :
      https://www.doctolib.fr/psychiatre/ville/prenom-nom
      https://www.doctolib.fr/psychiatre/ville/prenom-nom/booking?source=…
    """
    segments = [s for s in urllib.parse.urlsplit(profile_url).path.split("/") if s]
    if "booking" in segments:
        return segments[segments.index("booking") - 1]
    return segments[-1]


def fetch_doctolib_availability(profile_url: str, new_patients_only: bool = False) -> dict:
    """Vérifie l'état de réservation Doctolib d'un praticien, sans configuration.

    1. info.json du tunnel de réservation : HTTP 410 « profile_not_bookable »
       tant que la prise de rendez-vous en ligne est fermée ; HTTP 200 avec les
       motifs et agendas quand elle est ouverte.
    2. Si ouverte : availabilities.json compte les créneaux réels.

    Avec new_patients_only, seuls comptent les motifs accessibles aux nouveaux
    patients : un centre dont tous les motifs sont réservés aux patients déjà
    suivis (champ new_patient_restrictions couvrant tous ses agendas) est
    considéré comme fermé.
    """
    slug = doctolib_slug(profile_url)
    status, info = http_get_json(
        f"{DOCTOLIB_BASE}/online_booking/api/slot_selection_funnel/v1/info.json"
        f"?profile_slug={urllib.parse.quote(slug)}&locale=fr"
    )

    if status != 200:
        codes = [e.get("error_code") for e in info.get("errors", [])]
        if status == 410 or "profile_not_bookable" in codes:
            return {"bookable": False, "count": 0, "slots": []}
        raise RuntimeError(f"Doctolib info.json a répondu HTTP {status} ({codes})")

    data = info.get("data", {})
    agendas = [a for a in data.get("agendas", []) if not a.get("booking_disabled")]

    # Agendas autorisés par motif.
    allowed: dict[int, set] = {}
    for agenda in agendas:
        for motive_id in agenda.get("visit_motive_ids", []):
            allowed.setdefault(motive_id, set()).add(agenda["id"])

    if new_patients_only:
        for motive in data.get("visit_motives", []):
            restricted: set = set()
            for restriction in motive.get("new_patient_restrictions") or []:
                restricted |= set(restriction.get("agenda_ids", []))
            if motive.get("id") in allowed:
                allowed[motive["id"]] -= restricted

    allowed = {mid: ids for mid, ids in allowed.items() if ids}
    if not allowed:
        # Aucun motif accessible : fermé pour ce que l'on surveille.
        return {"bookable": not new_patients_only, "count": 0, "slots": []}

    agenda_ids = sorted({i for ids in allowed.values() for i in ids})
    motive_ids = sorted(allowed)
    practice_ids = sorted(
        {a["practice_id"] for a in agendas if a["id"] in set(agenda_ids) and a.get("practice_id")}
    )

    join = lambda ids: "-".join(str(i) for i in ids)  # noqa: E731
    query = urllib.parse.urlencode(
        {
            "visit_motive_ids": join(motive_ids),
            "agenda_ids": join(agenda_ids),
            "practice_ids": join(practice_ids),
            "start_date": now_paris().strftime("%Y-%m-%d"),
            "limit": 7,
        }
    )
    avail = json.loads(http_get(f"{DOCTOLIB_BASE}/availabilities.json?{query}"))

    total = int(avail.get("total", 0))
    slots: list[str] = []
    for day in avail.get("availabilities", []):
        slots.extend(str(s) for s in day.get("slots", []))
        if len(slots) >= 10:
            break
    next_slot = avail.get("next_slot")
    if not slots and next_slot:
        slots.append(f"prochain créneau : {next_slot}")

    return {"bookable": True, "count": total if total else len(slots), "slots": slots[:10]}


# --------------------------------------------------------------------------- #
# Notification e-mail
# --------------------------------------------------------------------------- #

def send_email(subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("MAIL_TO", user)

    if not user or not password:
        print("  [!] SMTP_USER / SMTP_PASSWORD non configurés : e-mail non envoyé.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(body)

    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"  [+] E-mail envoyé à {recipient} : {subject}")
    return True


def notify_change(name: str, url: str, status: str, result: dict) -> bool:
    """Envoie l'e-mail correspondant au nouvel état du praticien.

    Statuts possibles : "slots" (créneaux disponibles), "open_no_slots"
    (réservation ouverte mais aucun créneau visible, Doctolib uniquement),
    "closed" (réservation en ligne fermée / plus de disponibilité).
    """
    if status == "slots":
        subject = f"[RDV] Créneaux ouverts — {name}"
        lines = [
            f"Bonne nouvelle : {name} propose de nouveau des rendez-vous en ligne.",
            "",
            f"Créneaux détectés : {result['count']}",
        ]
        if result["slots"]:
            lines += ["", "Prochains créneaux :"]
            lines += [f"  - {slot}" for slot in result["slots"]]
        lines += ["", f"Réserver vite : {url}"]
    elif status == "open_no_slots":
        subject = f"[RDV] Réservation en ligne rouverte — {name}"
        lines = [
            f"La prise de rendez-vous en ligne de {name} vient de rouvrir.",
            "Aucun créneau n'est encore visible, mais cela peut arriver d'une minute"
            " à l'autre : le script continue de surveiller et vous préviendra.",
            "",
            f"Page du praticien : {url}",
        ]
    else:
        subject = f"[RDV] Plus de disponibilité en ligne — {name}"
        lines = [
            f"{name} n'a plus de disponibilité en ligne pour le moment.",
            "Le script continue de surveiller et vous préviendra à la réouverture.",
            "",
            f"Page du praticien : {url}",
        ]
    return send_email(subject, "\n".join(lines))


# --------------------------------------------------------------------------- #
# Programme principal
# --------------------------------------------------------------------------- #

def main() -> int:
    load_dotenv()

    # La liste des praticiens vient du secret PRACTITIONERS_JSON (GitHub Actions)
    # ou, à défaut, du fichier local practitioners.json (jamais committé) : ainsi
    # aucun nom de praticien n'apparaît dans le dépôt ni dans les logs publics.
    raw = os.environ.get("PRACTITIONERS_JSON", "").strip()
    if raw:
        practitioners = json.loads(raw)
    elif PRACTITIONERS_FILE.exists():
        practitioners = json.loads(PRACTITIONERS_FILE.read_text(encoding="utf-8"))
    else:
        print(
            "Aucune liste de praticiens : définir le secret PRACTITIONERS_JSON"
            " ou créer practitioners.json (voir practitioners.example.json)."
        )
        return 1

    state: dict = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    errors = 0
    for index, entry in enumerate(practitioners, start=1):
        url = entry["url"]
        label = entry.get("label")
        # Les logs GitHub Actions d'un dépôt public sont visibles par tous :
        # on n'y écrit ni nom ni URL de praticien.
        print(f"— Vérification du praticien {index}/{len(practitioners)}")

        platform = entry.get("platform") or (
            "doctolib" if "doctolib" in url else "maiia"
        )

        try:
            previous = state.get(url, {})
            ids: dict = {}

            if platform == "doctolib":
                result = fetch_doctolib_availability(
                    url, new_patients_only=bool(entry.get("new_patients"))
                )
                name = label or url
            else:
                # Les identifiants Maiia sont stables : cache dans state.json.
                if "practitioner_id" in previous and "center_id" in previous:
                    ids = {
                        "name": previous.get("name", label or url),
                        "practitioner_id": previous["practitioner_id"],
                        "center_id": previous["center_id"],
                    }
                else:
                    ids = resolve_ids(url)
                result = fetch_availability(ids["center_id"], ids["practitioner_id"])
                name = label or ids["name"]

            if result["count"] > 0:
                status = "slots"
            elif result.get("bookable"):
                status = "open_no_slots"
            else:
                status = "closed"

            print(f"  État : {status} ({result['count']} créneau(x))")

            was_status = previous.get("status")
            stored_status = status
            if was_status is None:
                print("  Première vérification : état enregistré, pas de notification.")
            elif was_status != status:
                print(f"  Changement détecté : {was_status} → {status}")
                try:
                    sent = notify_change(name, url, status, result)
                except (smtplib.SMTPException, OSError) as exc:
                    print(f"  [!] Envoi de l'e-mail échoué : {exc}")
                    sent = False
                if not sent:
                    # E-mail non parti : on conserve l'ancien état pour que le
                    # changement soit re-détecté (et l'envoi retenté) au prochain run.
                    stored_status = was_status
            else:
                print("  Pas de changement.")

            state[url] = {
                **ids,
                "status": stored_status,
                "count": result["count"],
                "last_check": now_paris().isoformat(timespec="seconds"),
            }
        except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as exc:
            errors += 1
            print(f"  [!] Erreur : {exc}")

    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"État sauvegardé dans {STATE_FILE.name}.")
    return 1 if errors and errors == len(practitioners) else 0


if __name__ == "__main__":
    sys.exit(main())
