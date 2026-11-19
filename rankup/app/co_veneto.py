"""CO Veneto / UNILAV: generazione locale e conservazione, mai invio telematico.

I valori dell'Ateneo sono configurati dall'Admin. Nessun dato di una ricevuta
preesistente, identificativo protocollato o anagrafica esemplificativa è incluso.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET
import hashlib
import json
import re
import unicodedata
import zipfile

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from incarichi_fields import normalize_excel_date
from co_veneto_codes import comune_nascita, comune_domicilio

NS = "http://servizi.lavoro.gov.it/unilav"
VERSIONE_TRACCIATO = "CO260429R1"
CODICE_NUOVA_COMUNICAZIONE = "0" * 16

# Campi configurabili: i dati della singola ricevuta non diventano valori generali.
CONFIG_FIELDS = (
    "codice_fiscale", "denominazione", "legale_cognome", "legale_nome",
    "legale_sesso", "legale_cittadinanza", "legale_comune_nascita",
    "legale_data_nascita", "legale_soggiornante",
    "sede_legale_comune", "sede_legale_cap", "sede_legale_indirizzo",
    "sede_legale_telefono", "sede_legale_fax", "sede_legale_email",
    "sede_lavoro_comune", "sede_lavoro_cap", "sede_lavoro_indirizzo",
    "sede_lavoro_telefono", "sede_lavoro_fax", "sede_lavoro_email",
    "settore_ateco", "cls_settore", "pubblica_amministrazione",
    "email_delegato", "ente_previdenziale", "pat_inail",
    "qualifica_professionale", "ccnl", "livello_inquadramento", "tipo_orario",
    "ore_ordinario", "ore_professionalizzante",
    "tariffa_verona", "tariffa_legnago", "tariffa_vicenza",
    "tariffa_trento", "tariffa_rovereto", "tariffa_bolzano",
)
CONFIG_DEFAULTS = {
    "legale_soggiornante": "SI", "pubblica_amministrazione": "SI",
    "ente_previdenziale": "01", "ccnl": "ND",
    "livello_inquadramento": "ND", "tipo_orario": "N",
    # Valori proposti dall'Ateneo: la generazione resta bloccata finché Admin
    # non li conferma esplicitamente in Amministrazione > CO Veneto.
    "ore_ordinario": "10", "ore_professionalizzante": "12",
    "tariffa_verona": "50", "tariffa_legnago": "64",
    "tariffa_vicenza": "64", "tariffa_trento": "85",
    "tariffa_rovereto": "85", "tariffa_bolzano": "100",
}
CONFIG_REQUIRED = (
    "codice_fiscale", "denominazione", "legale_cognome", "legale_nome",
    "legale_sesso", "legale_cittadinanza", "legale_comune_nascita",
    "legale_data_nascita", "legale_soggiornante", "sede_legale_comune",
    "sede_legale_cap", "sede_legale_indirizzo", "sede_legale_telefono",
    "sede_lavoro_comune", "sede_lavoro_cap", "sede_lavoro_indirizzo",
    "sede_lavoro_telefono", "settore_ateco", "cls_settore",
    "pubblica_amministrazione", "email_delegato", "ente_previdenziale",
    "pat_inail", "qualifica_professionale", "ccnl",
    "livello_inquadramento", "tipo_orario",
    "ore_ordinario", "ore_professionalizzante",
    "tariffa_verona", "tariffa_legnago", "tariffa_vicenza",
    "tariffa_trento", "tariffa_rovereto", "tariffa_bolzano",
)
CONTRACT_FIELDS = (
    "retribuzione", "domicilio_indirizzo", "domicilio_citta",
    "domicilio_cap", "domicilio_comune_codice", "nascita_comune_codice",
    "cittadinanza_codice", "livello_istruzione", "sesso", "note",
)
ITALIAN_MARKERS = {"ITALIA", "ITALIANA", "ITALIANO", "SI", "SÌ", "000"}
CAT_CODE = re.compile(r"^[A-Z][0-9]{3}$")
ZIP_MIME = "application/zip"
XML_MIME = "application/xml"


def create_schema(db):
    """Tabelle aggiuntive versionate con il DB RankUP, nessuna cancellazione."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS co_veneto_config (
            id INTEGER PRIMARY KEY CHECK(id=1),
            dati_json TEXT NOT NULL,
            aggiornato_il TEXT NOT NULL,
            autore TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS co_veneto_pratiche (
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            approvazione_id INTEGER NOT NULL REFERENCES graduatorie_approvazioni(id) ON DELETE CASCADE,
            incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
            candidatura_id INTEGER NOT NULL REFERENCES candidature(id),
            dati_json TEXT NOT NULL,
            autore TEXT NOT NULL,
            aggiornato_il TEXT NOT NULL,
            PRIMARY KEY(approvazione_id,incarico_id)
        );
        CREATE TABLE IF NOT EXISTS co_veneto_xml (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            approvazione_id INTEGER NOT NULL REFERENCES graduatorie_approvazioni(id) ON DELETE CASCADE,
            incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
            candidatura_id INTEGER NOT NULL REFERENCES candidature(id),
            revisione INTEGER NOT NULL,
            nome_file TEXT NOT NULL,
            xml BLOB NOT NULL,
            sha256 TEXT NOT NULL,
            motivazione TEXT NOT NULL,
            autore TEXT NOT NULL,
            creato_il TEXT NOT NULL,
            UNIQUE(approvazione_id,incarico_id,revisione)
        );
        CREATE INDEX IF NOT EXISTS ix_co_xml_approvazione
            ON co_veneto_xml(approvazione_id,incarico_id,revisione DESC);
        CREATE TABLE IF NOT EXISTS co_veneto_lotti (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            approvazione_id INTEGER NOT NULL REFERENCES graduatorie_approvazioni(id) ON DELETE CASCADE,
            nome_file TEXT NOT NULL,
            zip BLOB NOT NULL,
            sha256 TEXT NOT NULL,
            documenti_json TEXT NOT NULL,
            autore TEXT NOT NULL,
            creato_il TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS co_veneto_eventi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            approvazione_id INTEGER NOT NULL REFERENCES graduatorie_approvazioni(id) ON DELETE CASCADE,
            incarico_id INTEGER,
            evento TEXT NOT NULL,
            dettagli_json TEXT NOT NULL,
            autore TEXT NOT NULL,
            quando TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS co_veneto_config_eventi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prima_json TEXT NOT NULL,
            dopo_json TEXT NOT NULL,
            confermata INTEGER NOT NULL,
            autore TEXT NOT NULL,
            quando TEXT NOT NULL
        );
    """)
    config_columns = {row["name"] for row in db.execute(
        "PRAGMA table_info(co_veneto_config)"
    )}
    if "parametri_economici_confermati" not in config_columns:
        db.execute("ALTER TABLE co_veneto_config ADD COLUMN "
                   "parametri_economici_confermati INTEGER NOT NULL DEFAULT 0")


def fail(message, status=400):
    raise HTTPException(status_code=status, detail=message)


def timestamp():
    return datetime.now(ZoneInfo("Europe/Rome")).isoformat(timespec="microseconds")


def cfg_values(db):
    row = db.execute("SELECT dati_json FROM co_veneto_config WHERE id=1").fetchone()
    values = dict(CONFIG_DEFAULTS)
    if row:
        try:
            values.update({k: str(v or "") for k, v in json.loads(row["dati_json"]).items()
                           if k in CONFIG_FIELDS})
        except (TypeError, ValueError, AttributeError):
            pass
    return {k: values.get(k, "") for k in CONFIG_FIELDS}


ECONOMIC_HOUR_FIELDS = ("ore_ordinario", "ore_professionalizzante")
ECONOMIC_RATE_FIELDS = (
    "tariffa_verona", "tariffa_legnago", "tariffa_vicenza",
    "tariffa_trento", "tariffa_rovereto", "tariffa_bolzano",
)
SEAT_TARIFF_FIELDS = {
    "VERONA": "tariffa_verona",
    "LEGNAGO": "tariffa_legnago",
    "VICENZA": "tariffa_vicenza",
    "TRENTO": "tariffa_trento",
    "ROVERETO": "tariffa_rovereto",
    "BOLZANO": "tariffa_bolzano",
}


def _positive_decimal(raw):
    try:
        value = Decimal(str(raw or "").strip().replace(",", "."))
        return value if value.is_finite() and value > 0 else None
    except (InvalidOperation, ValueError):
        return None


def _italian_decimal(value, places=None):
    if places is not None:
        value = value.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP)
        return format(value, f".{places}f").replace(".", ",")
    text = format(value.normalize(), "f")
    return text.replace(".", ",")


def _seat_key(raw):
    value = unicodedata.normalize("NFKD", _clean(raw))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[().,;]", " ", value.upper())
    value = " ".join(value.split())
    # Accetta la denominazione PICA con provincia finale, ad es. VERONA (VR).
    value = re.sub(r"\s+[A-Z]{2}$", "", value)
    return value


def calculate_retribuzione(peso_copertura, sede, tipo_ssd, config):
    """Calcolo deterministico; non deduce la sede o la categoria in caso di dati anomali."""
    weight = _positive_decimal(peso_copertura)
    if weight is None:
        return {"ok": False, "problema": "Peso Copertura PICA mancante o non valido"}
    seat = _seat_key(sede)
    tariff_field = SEAT_TARIFF_FIELDS.get(seat)
    if not tariff_field:
        return {"ok": False, "problema": "Sede PICA non riconosciuta per la tariffa: " + _clean(sede)}
    model = _clean(tipo_ssd).lower()
    if model == "professionalizzante":
        hours_field = "ore_professionalizzante"
    elif model == "ordinario":
        hours_field = "ore_ordinario"
    else:
        return {"ok": False, "problema":
                "Classificazione SSD non utilizzabile per il calcolo delle ore: " +
                (_clean(tipo_ssd) or "[DA VERIFICARE]")}
    try:
        hours_value = Decimal(str(config.get(hours_field, "")).strip().replace(",", "."))
        if not hours_value.is_finite() or hours_value <= 0 or hours_value != hours_value.to_integral_value():
            raise ValueError
        hours = int(hours_value)
    except (InvalidOperation, ValueError):
        return {"ok": False, "problema": "Ore di riferimento non valide in Amministrazione CO Veneto"}
    rate = _positive_decimal(config.get(tariff_field))
    if rate is None or rate.as_tuple().exponent < -2:
        return {"ok": False, "problema": "Tariffa oraria non valida in Amministrazione CO Veneto"}
    amount = (weight * Decimal(hours) * rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return {
        "ok": True, "problema": "", "peso_copertura": _clean(peso_copertura),
        "sede": _clean(sede), "tipo_ssd": model, "ore": hours,
        "tariffa": format(rate.quantize(Decimal("0.01")), ".2f"),
        "importo": format(amount, ".2f"),
        "formula": (_italian_decimal(weight) + " × " + str(hours) +
                    " ore × " + _italian_decimal(rate, 2) + " €/ora = " +
                    _italian_decimal(amount, 2) + " €"),
        "manuale": False, "motivazione": "", "importo_applicato": format(amount, ".2f"),
    }


def _same_amount(left, right):
    a = _positive_decimal(left)
    b = _positive_decimal(right)
    return a is not None and b is not None and a == b


def economic_config_confirmed(db):
    row = db.execute(
        "SELECT parametri_economici_confermati FROM co_veneto_config WHERE id=1"
    ).fetchone()
    return bool(row and row["parametri_economici_confermati"])


def missing_config(values):
    """Anche codifiche palesemente errate sono da verificare, non solo i vuoti."""
    problems = [key for key in CONFIG_REQUIRED
                if not str(values.get(key) or "").strip()]
    for key in ("legale_comune_nascita", "sede_legale_comune", "sede_lavoro_comune"):
        if values.get(key) and not cat_code(values[key]):
            problems.append(key)
    if values.get("legale_data_nascita") and not iso_date(values["legale_data_nascita"]):
        problems.append("legale_data_nascita")
    if values.get("legale_sesso") and values["legale_sesso"] not in ("F", "M"):
        problems.append("legale_sesso")
    if values.get("legale_cittadinanza") and not re.fullmatch(r"[0-9]{3}", values["legale_cittadinanza"]):
        problems.append("legale_cittadinanza")
    if values.get("codice_fiscale") and not re.fullmatch(r"[A-Z0-9]{11,16}", values["codice_fiscale"].upper()):
        problems.append("codice_fiscale")
    for key in ("sede_legale_cap", "sede_lavoro_cap"):
        if values.get(key) and not re.fullmatch(r"[0-9]{5}", values[key]):
            problems.append(key)
    for key in ECONOMIC_HOUR_FIELDS:
        raw = str(values.get(key) or "").strip()
        if raw and not re.fullmatch(r"[0-9]+", raw):
            problems.append(key)
        elif raw and int(raw) <= 0:
            problems.append(key)
    for key in ECONOMIC_RATE_FIELDS:
        rate = _positive_decimal(values.get(key))
        if rate is None or rate.as_tuple().exponent < -2:
            problems.append(key)
    return sorted(set(problems))


def iso_date(raw):
    return normalize_excel_date(str(raw or ""))


def cat_code(raw):
    value = str(raw or "").strip().upper()
    return value if CAT_CODE.fullmatch(value) else ""


def citizenship_from_pica(source):
    italian = str(source.get("cittadinanza_italiana") or "").strip().upper()
    country = str(source.get("cittadinanza") or "").strip().upper()
    # "NO" e una cittadinanza estera dichiarata non possono diventare 000:
    # il codice fiscale italiano NON dimostra la cittadinanza.
    if italian in ("NO", "FALSE", "N", "0"):
        return ""
    if italian in ("SI", "SÌ", "YES", "TRUE", "Y", "1"):
        if country and country not in ITALIAN_MARKERS:
            return ""  # Dati PICA discordanti: richiedono verifica.
        return "000"
    return "000" if country in ("ITALIA", "ITALIANA", "ITALIANO", "000") else ""


def academic_end(academic_year):
    text = str(academic_year or "").strip()
    if not re.fullmatch(r"\d{4}-\d{4}", text):
        return ""
    a, b = [int(x) for x in text.split("-")]
    return f"{b:04d}-09-30" if b == a + 1 else ""


def _clean(value):
    return str(value or "").strip()



def note_from_approval(scope, job, academic_year, bando_code):
    """Riferimenti operativi da PICA e approvazione, mai dalla ricevuta esempio.

    La Nota PDF di CO Veneto NON compare nell'XML UNILAV restituito dal
    portale: questa informazione resta locale finché non esiste un
    campo XSD verificato per trasmetterla.
    """
    pieces = [_clean(scope.get("cds")), _clean(scope.get("sede")),
              _clean(job.get("insegnamento")), _clean(job.get("modulo"))]
    # Nessun separatore vuoto (" - - ") e nessuna duplicazione dello
    # stesso insegnamento indicato anche come modulo in PICA.
    normalized = []
    for part in pieces:
        if part and (not normalized or normalized[-1].casefold() != part.casefold()):
            normalized.append(part)
    aa = _clean(academic_year).replace("-", "/")
    bando = _clean(bando_code)
    if aa and bando:
        normalized.append("A.A. " + aa + "; BANDO " + bando)
    return " - ".join(normalized)

def defaults_from_pica(source):
    return {
        "retribuzione": "",
        "domicilio_indirizzo": _clean(source.get("residenza_indirizzo")),
        "domicilio_citta": _clean(source.get("residenza_citta")),
        "domicilio_cap": _clean(source.get("residenza_cap")),
        "domicilio_comune_codice": comune_domicilio(source),
        "nascita_comune_codice": comune_nascita(source),
        "cittadinanza_codice": citizenship_from_pica(source),
        # Valore predefinito confermato per gli incarichi RankUP.
        # Resta modificabile in caso di eccezione documentata.
        "livello_istruzione": "70",
        "sesso": _clean(source.get("sesso")).upper()[:1],
    }


def work_values(source, saved):
    values = defaults_from_pica(source)
    # I vecchi record possono contenere campi vuoti salvati prima
    # dell'autocompilazione: non devono cancellare i nuovi valori verificati.
    automatic = {"domicilio_comune_codice", "nascita_comune_codice",
                 "cittadinanza_codice", "livello_istruzione"}
    values.update({key: _clean(saved.get(key)) for key in CONTRACT_FIELDS
                   if key != "retribuzione" and key in saved
                   and (key not in automatic or _clean(saved.get(key)))})
    # Se il Comune di domicilio è stato cambiato manualmente rispetto a PICA,
    # non mantenere in automatico il codice del vecchio Comune.
    city_from_pica = _clean(source.get("residenza_citta"))
    city_override = _clean(saved.get("domicilio_citta"))
    if city_override and city_override.casefold() != city_from_pica.casefold():
        if values["domicilio_comune_codice"] == defaults_from_pica(source)["domicilio_comune_codice"]:
            values["domicilio_comune_codice"] = ""
    return values


def _has_valid_compenso(raw):
    try:
        amount = Decimal(str(raw).replace(",", "."))
        return amount.is_finite() and amount > 0 and amount.as_tuple().exponent >= -2
    except (InvalidOperation, ValueError):
        return False


def validate_worker(source, work, start, end):
    problems = []
    if not _clean(source.get("nome")) or not _clean(source.get("cognome")):
        problems.append("nominativo PICA")
    if not re.fullmatch(r"[A-Z0-9]{16}", _clean(source.get("codice_fiscale")).upper()):
        problems.append("codice fiscale")
    if not iso_date(source.get("data_nascita")):
        problems.append("data di nascita")
    if work.get("sesso") not in ("F", "M"):
        problems.append("sesso")
    if not cat_code(work.get("nascita_comune_codice")):
        problems.append("codice catastale comune di nascita")
    if not re.fullmatch(r"[0-9]{3}", work.get("cittadinanza_codice", "")):
        problems.append("codice cittadinanza")
    elif work["cittadinanza_codice"] != "000":
        # Il generatore base replica la comunicazione di una lavoratrice
        # italiana. Per cittadinanze estere servono verifiche e, in casi
        # specifici, informazioni di soggiorno non gestite dal modulo.
        problems.append("cittadinanza estera: verificare i campi UNILAV specifici (non gestiti)")
    if not cat_code(work.get("domicilio_comune_codice")):
        problems.append("codice catastale comune di domicilio")
    if not _clean(work.get("domicilio_indirizzo")) or not _clean(work.get("domicilio_cap")):
        problems.append("indirizzo/CAP del domicilio")
    if not re.fullmatch(r"[0-9]{5}", work.get("domicilio_cap", "")):
        problems.append("CAP domicilio")
    if not re.fullmatch(r"[0-9]{2}", work.get("livello_istruzione", "")):
        problems.append("codice livello di istruzione")
    if not _has_valid_compenso(work.get("retribuzione", "")):
        problems.append("retribuzione annua lorda")
    if not start or not end or start > end:
        problems.append("date contrattuali PICA / A.A.")
    return problems


def make_xml(config, source, work, start, end, when=None):
    """Il tracciato riproduce i nodi osservati nell'UNILAV verificato nel portale.

    Questo controllo NON sostituisce la verifica ministeriale XSD/CO Veneto.
    """
    absent = missing_config(config)
    problems = validate_worker(source, work, start, end)
    if absent or problems:
        raise ValueError("Configurazione mancante: " + ", ".join(absent) +
                         "; dati lavoratore da verificare: " + ", ".join(problems))
    # La ricevuta XML CO Veneto utilizza un dateTime locale SENZA offset.
    # Gli eventi di audit conservano invece il timestamp con fuso orario.
    try:
        invio = datetime.fromisoformat(when or timestamp()).strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        raise ValueError("Data invio XML non valida")
    ET.register_namespace("", NS)
    q = lambda name: "{" + NS + "}" + name
    element = lambda parent, name, value: ET.SubElement(parent, q(name)).__setattr__("text", str(value))
    root = ET.Element(q("UniLav"), {
        "assunzioneForzaMaggiore": "NO",
        "codiceComunicazione": CODICE_NUOVA_COMUNICAZIONE,
        "dataInvio": invio,
        "e-mailDelegato": config["email_delegato"],
        "versione": VERSIONE_TRACCIATO,
    })
    datore = ET.SubElement(root, q("DatoreLavoro"), {
        "codiceFiscale": config["codice_fiscale"],
        "denominazione": config["denominazione"],
    })
    legale = ET.SubElement(datore, q("DatoreAnagraficaCompleta"), {
        "SoggiornanteInItalia": config["legale_soggiornante"],
    })
    for tag, field in (("cognome", "legale_cognome"), ("nome", "legale_nome"),
                       ("cittadinanza", "legale_cittadinanza"),
                       ("sesso", "legale_sesso")):
        element(legale, tag, config[field])
    nascita = ET.SubElement(legale, q("nascita"))
    element(nascita, "comune", config["legale_comune_nascita"])
    element(nascita, "data", iso_date(config["legale_data_nascita"]))
    for sede, prefix in (("SedeLegale", "sede_legale"), ("SedeLavoro", "sede_lavoro")):
        node = ET.SubElement(datore, q(sede))
        for tag, field in (("Comune", "comune"), ("cap", "cap"),
                           ("Indirizzo", "indirizzo"), ("e-mail", "email"),
                           ("telefono", "telefono"), ("fax", "fax")):
            value = _clean(config.get(prefix + "_" + field))
            if value:
                element(node, tag, value)
    element(datore, "Settore", config["settore_ateco"])
    element(datore, "CLSSettore", config["cls_settore"])
    element(datore, "PubblicaAmministrazione", config["pubblica_amministrazione"])
    lavoratore = ET.SubElement(root, q("Lavoratore"))
    person = ET.SubElement(lavoratore, q("AnagraficaCompleta"))
    element(person, "cognome", source["cognome"])
    element(person, "nome", source["nome"])
    element(person, "codice-fiscale", source["codice_fiscale"].upper())
    element(person, "cittadinanza", work["cittadinanza_codice"])
    element(person, "sesso", work["sesso"])
    birth = ET.SubElement(person, q("nascita"))
    element(birth, "comune", work["nascita_comune_codice"])
    element(birth, "data", iso_date(source["data_nascita"]))
    home = ET.SubElement(lavoratore, q("IndirizzoLavoratore"))
    element(home, "Comune", work["domicilio_comune_codice"])
    element(home, "cap", work["domicilio_cap"])
    element(home, "Indirizzo", work["domicilio_indirizzo"])
    element(lavoratore, "LivelloIstruzione", work["livello_istruzione"])
    rapporto = ET.SubElement(root, q("InizioRapporto"), {
        "TipologiaContrattuale": "B.03.00",
        "assunzioneObbligatoria": "NO",
        "dataFine": end, "dataInizio": start,
        "entePrevidenziale": config["ente_previdenziale"],
        "lavInMobilita": "NO", "lavoroInAgricoltura": "NO",
        "lavoroStagionale": "NO", "socioLavoratore": "NO",
    })
    element(rapporto, "ccnl", config["ccnl"])
    element(rapporto, "livelloInquadramento", config["livello_inquadramento"])
    ET.SubElement(rapporto, q("tipoOrario"), {"codice": config["tipo_orario"]})
    element(rapporto, "qualificaProfessionale", config["qualifica_professionale"])
    amount = Decimal(work["retribuzione"].replace(",", "."))
    element(rapporto, "RetribuzioneCompenso", format(amount.normalize(), "f"))
    element(rapporto, "PatINAIL", config["pat_inail"])
    element(root, "TipoComunicazione", "01")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class ConfigBody(BaseModel):
    dati: dict[str, str]
    conferma_parametri_economici: bool = False


class WorkBody(BaseModel):
    dati: dict[str, str]


class GenerateBody(BaseModel):
    incarichi_ids: list[int] = Field(min_length=1, max_length=100)
    motivazione: str = Field(default="", max_length=1000)


def _approval(db, code, approval_id, valid_code):
    b = db.execute("SELECT id,stato FROM bandi WHERE codice=?",
                   (valid_code(code),)).fetchone()
    if not b:
        fail("Bando non trovato", 404)
    a = db.execute("""
        SELECT a.id,a.dati_json,a.stato,r.approvazione_id AS regredita,
               COALESCE(ref.anno_accademico,'') AS anno
        FROM graduatorie_approvazioni a
        LEFT JOIN graduatorie_approvazioni_regressioni r ON r.approvazione_id=a.id
        LEFT JOIN bandi_riferimenti ref ON ref.bando_id=a.bando_id
        WHERE a.id=? AND a.bando_id=?
    """, (approval_id, b["id"])).fetchone()
    if not a:
        fail("Approvazione non trovata", 404)
    active = (a["stato"] == "approvata" and a["regredita"] is None
              and b["stato"] != "Archiviato")
    return b["id"], a, active


def _assignments(db, code, bando_id, approval, personal_data):
    payload = json.loads(approval["dati_json"])
    personal = personal_data(db, bando_id, code, payload)
    year_end = academic_end(approval["anno"])
    result = []
    for scope in payload.get("selezioni", []):
        for job in scope.get("incarichi", []):
            winner_id = job.get("vincitore_id")
            if winner_id is None:
                continue
            inc = db.execute("SELECT id,key,inizio_iso FROM incarichi WHERE id=? AND bando_id=?",
                             (job["id"], bando_id)).fetchone()
            person = db.execute("""
                SELECT id,incarico_id,id_pica,nome,cognome,codice_fiscale,stato
                FROM candidature WHERE id=? AND bando_id=?
            """, (winner_id, bando_id)).fetchone()
            if (not inc or inc["key"] != job.get("key") or not person
                    or person["incarico_id"] != inc["id"] or person["stato"] != "Attiva"):
                continue
            raw = dict(personal.get(winner_id, {}))
            for key in ("id_pica", "nome", "cognome", "codice_fiscale"):
                if not raw.get(key):
                    raw[key] = person[key]
            saved = db.execute("""
                SELECT candidatura_id,dati_json FROM co_veneto_pratiche
                WHERE approvazione_id=? AND incarico_id=?
            """, (approval["id"], inc["id"])).fetchone()
            overrides = {}
            if saved and saved["candidatura_id"] == winner_id:
                overrides = json.loads(saved["dati_json"])
            work = work_values(raw, overrides)
            # Per i record creati prima della funzione Note, il testo viene
            # derivato senza modificare gli originali PICA/atti esistenti.
            if "note" not in overrides:
                work["note"] = note_from_approval(scope, job, approval["anno"], code)
            warnings = validate_worker(raw, work, inc["inizio_iso"], year_end)
            if not work.get("note"):
                warnings.append("note operative da verificare")
            elif len(work["note"]) > 1000:
                warnings.append("note operative oltre 1000 caratteri")
            latest = db.execute("""
                SELECT id,revisione,nome_file,autore,creato_il FROM co_veneto_xml
                WHERE approvazione_id=? AND incarico_id=?
                ORDER BY revisione DESC LIMIT 1
            """, (approval["id"], inc["id"])).fetchone()
            result.append({
                "incarico_id": inc["id"], "candidatura_id": winner_id,
                "id_pica": person["id_pica"], "nome": person["nome"],
                "cognome": person["cognome"], "cds": scope["cds"],
                "sede": scope["sede"], "insegnamento": job["insegnamento"],
                "modulo": job["modulo"], "inizio": inc["inizio_iso"],
                "fine": year_end, "anno_accademico": approval["anno"],
                "residenza_pica": {
                    "indirizzo": raw.get("residenza_indirizzo", ""),
                    "citta": raw.get("residenza_citta", ""),
                    "cap": raw.get("residenza_cap", "")
                },
                "work": work,
                "mancanti": warnings,
                "ultimo_file": dict(latest) if latest else None,
                "_source": raw,
            })
    return result



def approval_zip_state(db, approval_id, snapshot):
    """Stato degli ZIP prodotti per la procedura (mai stato di invio a CoVeneto).

    Verde solo quando ogni incarico vincitore ha un XML conservato e nessuno
    dei dati CO Veneto interessati e' stato salvato dopo quel file.
    """
    if not db.execute(
        "SELECT 1 FROM co_veneto_lotti WHERE approvazione_id=? LIMIT 1",
        (approval_id,),
    ).fetchone():
        return "non_generato"

    expected = {
        int(job["id"])
        for scope in snapshot.get("selezioni", [])
        for job in scope.get("incarichi", [])
        if job.get("vincitore_id") is not None
    }
    if not expected:
        return "parziale"

    rows = db.execute("""
        SELECT incarico_id,creato_il FROM co_veneto_xml
        WHERE approvazione_id=? ORDER BY id DESC
    """, (approval_id,)).fetchall()
    latest = {}
    for row in rows:
        latest.setdefault(row["incarico_id"], row["creato_il"])
    if not expected.issubset(latest):
        return "parziale"

    configuration = db.execute(
        "SELECT aggiornato_il FROM co_veneto_config WHERE id=1"
    ).fetchone()
    edits = {
        row["incarico_id"]: row["aggiornato_il"]
        for row in db.execute("""
            SELECT incarico_id,aggiornato_il FROM co_veneto_pratiche
            WHERE approvazione_id=?
        """, (approval_id,)).fetchall()
    }

    def newer(edited, generated):
        if not edited:
            return False
        return datetime.fromisoformat(edited) > datetime.fromisoformat(generated)

    for job_id in expected:
        generated = latest[job_id]
        if (configuration and newer(configuration["aggiornato_il"], generated)
                or newer(edits.get(job_id), generated)):
            return "da_rigenerare"
    return "generato"


def _event(db, bando_id, approval_id, job_id, action, details, actor):
    db.execute("""INSERT INTO co_veneto_eventi
        (bando_id,approvazione_id,incarico_id,evento,dettagli_json,autore,quando)
        VALUES(?,?,?,?,?,?,?)""",
        (bando_id, approval_id, job_id, action,
         json.dumps(details, ensure_ascii=False), actor, timestamp()))


def register_routes(app, connect, require_authenticated, require_admin,
                    valid_code, personal_data, request_actor):
    """Endpoint collegati all'autenticazione e alle approvazioni già esistenti."""

    @app.get("/api/admin/co-veneto/config")
    def config_get(request: Request):
        require_admin(request)
        with connect() as db:
            row = db.execute("SELECT aggiornato_il,autore FROM co_veneto_config WHERE id=1").fetchone()
            return {"dati": cfg_values(db), "mancanti": missing_config(cfg_values(db)),
                    "aggiornato_il": row["aggiornato_il"] if row else None,
                    "autore": row["autore"] if row else None}

    @app.put("/api/admin/co-veneto/config")
    def config_put(body: ConfigBody, request: Request):
        require_admin(request)
        if set(body.dati) != set(CONFIG_FIELDS):
            fail("Configurazione incompleta o contiene campi non previsti")
        values = {k: _clean(v) for k, v in body.dati.items()}
        if any(len(v) > 250 for v in values.values()):
            fail("Campo della configurazione troppo lungo")
        for field in ("legale_data_nascita",):
            if values[field] and not iso_date(values[field]):
                fail("Data di nascita del rappresentante non riconosciuta")
        with connect() as db:
            db.execute("""INSERT INTO co_veneto_config
                (id,dati_json,aggiornato_il,autore) VALUES(1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET dati_json=excluded.dati_json,
                aggiornato_il=excluded.aggiornato_il,autore=excluded.autore""",
                (json.dumps(values, ensure_ascii=False), timestamp(), request_actor()))
            db.commit()
        return {"ok": True, "mancanti": missing_config(values)}

    @app.get("/api/bandi/{code}/co-veneto/approvazioni/{approval_id}")
    def approval_get(code: str, approval_id: int, request: Request):
        require_authenticated(request)
        code = valid_code(code)
        with connect() as db:
            bando_id, approval, active = _approval(db, code, approval_id, valid_code)
            jobs = _assignments(db, code, bando_id, approval, personal_data)
            config = cfg_values(db)
            lotti = db.execute("""SELECT id,nome_file,sha256,autore,creato_il
                FROM co_veneto_lotti WHERE approvazione_id=? ORDER BY id DESC""",
                (approval_id,)).fetchall()
        for job in jobs:
            job.pop("_source", None)
        return {"approvazione_id": approval_id, "attiva": active,
                "anno_accademico": approval["anno"], "incarichi": jobs,
                "configurazione_mancante": missing_config(config),
                "lotti": [dict(lotto) for lotto in lotti],
                "tipo_contratto": "B.03.00 - Collaborazione coordinata e continuativa",
                "avvertenza": "La generazione non equivale all'invio: verificare lo ZIP in CO Veneto."}

    @app.put("/api/bandi/{code}/co-veneto/approvazioni/{approval_id}/incarichi/{job_id}")
    def work_put(code: str, approval_id: int, job_id: int, body: WorkBody,
                 request: Request):
        principal = require_authenticated(request)
        code = valid_code(code)
        if set(body.dati) != set(CONTRACT_FIELDS):
            fail("Dati contrattuali incompleti o non previsti")
        values = {k: _clean(v) for k, v in body.dati.items()}
        if any(len(v) > (1000 if k == "note" else 250)
               for k, v in values.items()):
            fail("Campo contrattuale troppo lungo")
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            bando_id, approval, active = _approval(db, code, approval_id, valid_code)
            if not active:
                fail("La procedura non ha più atti approvati attivi", 409)
            jobs = _assignments(db, code, bando_id, approval, personal_data)
            job = next((j for j in jobs if j["incarico_id"] == job_id), None)
            if not job:
                fail("Vincitore non presente nell'approvazione", 404)
            if job["ultimo_file"] and principal["role"] != "admin":
                fail("File già generato: la modifica è riservata all'Admin", 403)
            db.execute("""INSERT INTO co_veneto_pratiche
                (bando_id,approvazione_id,incarico_id,candidatura_id,dati_json,autore,aggiornato_il)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(approvazione_id,incarico_id) DO UPDATE SET
                candidatura_id=excluded.candidatura_id,dati_json=excluded.dati_json,
                autore=excluded.autore,aggiornato_il=excluded.aggiornato_il""",
                (bando_id, approval_id, job_id, job["candidatura_id"],
                 json.dumps(values, ensure_ascii=False), request_actor(), timestamp()))
            _event(db, bando_id, approval_id, job_id, "dati_contrattuali",
                   {"campi": sorted(values)}, request_actor())
            db.commit()
        return {"ok": True}

    @app.post("/api/bandi/{code}/co-veneto/approvazioni/{approval_id}/genera")
    def generate(code: str, approval_id: int, body: GenerateBody, request: Request):
        principal = require_authenticated(request)
        code = valid_code(code)
        ids = list(dict.fromkeys(body.incarichi_ids))
        if len(ids) != len(body.incarichi_ids):
            fail("Incarichi duplicati nella selezione")
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            bando_id, approval, active = _approval(db, code, approval_id, valid_code)
            if not active:
                fail("La generazione è consentita solo con atti formalmente approvati", 409)
            config = cfg_values(db)
            if missing_config(config):
                fail("Completare Amministrazione → CO Veneto: " +
                     ", ".join(missing_config(config)), 409)
            jobs = {x["incarico_id"]: x for x in
                    _assignments(db, code, bando_id, approval, personal_data)}
            if any(job_id not in jobs for job_id in ids):
                fail("Uno o più incarichi non appartengono all'approvazione", 409)
            selected = [jobs[job_id] for job_id in ids]
            if any(job["mancanti"] for job in selected):
                problems = [str(j["id_pica"]) + ": " + ", ".join(j["mancanti"])
                            for j in selected if j["mancanti"]]
                fail("Dati da verificare: " + "; ".join(problems)[:800], 409)
            # Evita comunicazioni identiche per uno stesso lavoratore/data
            # quando si aggiudica più moduli: richiede istruttoria manuale.
            event_keys = [(j["_source"]["codice_fiscale"].upper(), j["inizio"])
                          for j in selected]
            if len(event_keys) != len(set(event_keys)):
                fail("Stesso lavoratore e stessa data di inizio per più incarichi: "
                     "verificare se sia necessaria un'unica comunicazione", 409)
            for job in selected:
                if job["ultimo_file"]:
                    if principal["role"] != "admin":
                        fail("File già generato: solo Admin può rigenerarlo", 403)
                    if len(body.motivazione.strip()) < 10:
                        fail("Motivazione obbligatoria (almeno 10 caratteri) per rigenerare", 400)
            generated_at = timestamp()
            built = []
            for job in selected:
                raw = job["_source"]
                try:
                    payload = make_xml(config, raw, job["work"], job["inizio"],
                                       job["fine"], when=generated_at)
                except ValueError as ex:
                    fail("UNILAV non generabile: " + str(ex), 409)
                revision = (job["ultimo_file"]["revisione"] + 1) if job["ultimo_file"] else 1
                name = ("UNILAV_" + code + "_PICA_" +
                        re.sub(r"[^A-Za-z0-9_-]", "_", str(job["id_pica"]))[:40] +
                        "_INCARICO_" + str(job["incarico_id"]) + "_v" + str(revision) + ".xml")
                digest = hashlib.sha256(payload).hexdigest()
                cursor = db.execute("""INSERT INTO co_veneto_xml
                    (bando_id,approvazione_id,incarico_id,candidatura_id,
                     revisione,nome_file,xml,sha256,motivazione,autore,creato_il)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (bando_id, approval_id, job["incarico_id"], job["candidatura_id"],
                     revision, name, payload, digest,
                     body.motivazione.strip() if job["ultimo_file"] else "",
                     request_actor(), generated_at))
                built.append((cursor.lastrowid, name, payload))
                _event(db, bando_id, approval_id, job["incarico_id"],
                       "rigenerazione_xml" if job["ultimo_file"] else "generazione_xml",
                       {"revisione": revision, "sha256": digest, "xml_id": cursor.lastrowid,
                        "note": job["work"]["note"], "note_in_xml": False,
                        "motivazione": body.motivazione.strip() if job["ultimo_file"] else ""},
                       request_actor())
            out = BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zipped:
                for _, name, data in built:
                    zipped.writestr(name, data)
            archive_bytes = out.getvalue()
            with zipfile.ZipFile(BytesIO(archive_bytes)) as zipped:
                if zipped.testzip() is not None:
                    fail("Archivio ZIP non integro", 500)
            zipname = "RankUP_CO_Veneto_" + code + "_approvazione_" + str(approval_id) + ".zip"
            cursor = db.execute("""INSERT INTO co_veneto_lotti
                (bando_id,approvazione_id,nome_file,zip,sha256,documenti_json,autore,creato_il)
                VALUES(?,?,?,?,?,?,?,?)""",
                (bando_id, approval_id, zipname, archive_bytes,
                 hashlib.sha256(archive_bytes).hexdigest(),
                 json.dumps([id for id, _, _ in built]), request_actor(), generated_at))
            db.commit()
            lotto_id = cursor.lastrowid
        return {"ok": True, "lotto_id": lotto_id, "file": len(built),
                "avvertenza": "ZIP generato, non trasmesso. Le Note sono conservate in RankUP, NON sono contenute nell'XML standard: riportarle nel portale se necessario. Usare Verifica file su CO Veneto."}

    @app.get("/api/bandi/{code}/co-veneto/approvazioni/{approval_id}/lotti/{lotto_id}/zip")
    def download_batch(code: str, approval_id: int, lotto_id: int, request: Request):
        require_authenticated(request)
        code = valid_code(code)
        with connect() as db:
            bando_id, _, _ = _approval(db, code, approval_id, valid_code)
            row = db.execute("""SELECT nome_file,zip,sha256 FROM co_veneto_lotti
                WHERE id=? AND approvazione_id=? AND bando_id=?""",
                (lotto_id, approval_id, bando_id)).fetchone()
        if not row:
            fail("Lotto non trovato", 404)
        content = bytes(row["zip"])
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            fail("Integrità ZIP non verificata", 409)
        return Response(content=content, media_type=ZIP_MIME,
                        headers={"Content-Disposition": 'attachment; filename="' +
                                 row["nome_file"] + '"', "Cache-Control": "no-store"})

    @app.get("/api/bandi/{code}/co-veneto/approvazioni/{approval_id}/xml/{file_id}")
    def download_xml(code: str, approval_id: int, file_id: int, request: Request):
        require_authenticated(request)
        code = valid_code(code)
        with connect() as db:
            bando_id, _, _ = _approval(db, code, approval_id, valid_code)
            row = db.execute("""SELECT nome_file,xml,sha256 FROM co_veneto_xml
                WHERE id=? AND approvazione_id=? AND bando_id=?""",
                (file_id, approval_id, bando_id)).fetchone()
        if not row:
            fail("XML non trovato", 404)
        content = bytes(row["xml"])
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            fail("Integrità XML non verificata", 409)
        return Response(content=content, media_type=XML_MIME,
                        headers={"Content-Disposition": 'attachment; filename="' +
                                 row["nome_file"] + '"', "Cache-Control": "no-store"})
