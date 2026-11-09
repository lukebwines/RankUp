"""Lettura sicura delle schede PDF restituite dalle commissioni.

Nessuno script PDF viene eseguito. Il totale viene calcolato usando i valori
numerici delle singole voci, non i testi delle attività selezionate.
Il modulo non scrive nel DB: il server esegue la conferma separatamente.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import re
import unicodedata
from pypdf import PdfReader

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_BATCH_BYTES = 40 * 1024 * 1024
MAX_BATCH_FILES = 120

ORDINARY_MAX = {
    **dict(zip((f"P{i:02d}" for i in range(1, 9)), (2, 2, 3, 2, 1, 1, 2, 2))),
    "P09": 10, "P10": 10, "P11": 2, "P12": 3,
    "P13": 10, "P14": 3, "P15": 7,
}
ENGLISH_MAX = {
    **dict(zip((f"P{i:02d}" for i in range(1,8)),(2,3,2,1,1,2,2))),
    "P08":10, "P09":5, "P10":2, "P11":3,
    "P12":10, "P13":7,
}
PROFESSIONAL_MAX = {
    **dict(zip((f"P{i:02d}" for i in range(1, 7)), (3, 1, 2, 2, 3, 4))),
    "P14": 10, "P15": 2, "P16": 3, "P17": 7,
    "P18": 3, "P19": 10,
}
# Voci effettive delle schede RankUp. Nessuna deduzione di idoneità
# dal solo punteggio (nemmeno quando è superiore a zero).
# Esiti correnti della Commissione: cinque voci comuni alle tre schede.
ESITI_IDONEI = ("CANDIDATO IDONEO",)
ESITI_NON_IDONEI = (
    "CANDIDATO NON IDONEO - PROFILO PROFESSIONALE NON COERENTE",
    "CANDIDATO NON IDONEO - VALUTAZIONE NEGATIVA DELLA COMMISSIONE",
)
ESITI_ESCLUSI = (
    "CANDIDATO NON AMMISSIBILE - REQUISITO ENTE NON SODDISFATTO",
    "CANDIDATO NON AMMISSIBILE - REQUISITI GENERALI NON SODDISFATTI",
)
# Gli esiti delle vecchie schede restano interpretabili senza riscrivere
# originali PDF, decisioni della Commissione o storico.
ESITI_STORICI_IDONEI = (
    "CANDIDATO AMMESSO IN GRADUATORIA", "CANDIDATO UNICO IDONEO",
)
ESITI_STORICI_NON_IDONEI = (
    "CANDIDATO CON CURRICULUM NON VALUTABILE O NON IDONEO",
    "CANDIDATO NON IDONEO",
)
ESITI_STORICI_ESCLUSI = (
    "CANDIDATO CON REQUISITO ENTE DI APPARTENENZA MANCANTE",
    "CANDIDATO CON TITOLO DI STUDIO RICHIESTO PER L'ACCESSO MANCANTE",
    "CANDIDATO CON TITOLO DI STUDIO RICHIESTO PER L'ACCESSO NON PERTINENTE",
)
ESITI_IDONEI += ESITI_STORICI_IDONEI
ESITI_NON_IDONEI += ESITI_STORICI_NON_IDONEI
ESITI_ESCLUSI += ESITI_STORICI_ESCLUSI
ESITI_AMMESSI = ESITI_IDONEI + ESITI_NON_IDONEI + ESITI_ESCLUSI


def classify_esito(value: str) -> str:
    """Restituisce la decisione solo per gli esiti selezionabili nel PDF."""
    if value in ESITI_IDONEI:
        return "idoneo"
    if value in ESITI_NON_IDONEI:
        return "non_idoneo"
    if value in ESITI_ESCLUSI:
        return "escluso"
    raise ValueError("Esito della commissione non selezionato o non riconosciuto")


def norm(value: object) -> str:
    val = unicodedata.normalize("NFKD", str(value or ""))
    val = "".join(x for x in val if not unicodedata.combining(x))
    return re.sub(r"\s+", " ", val).strip().casefold()


def dec(value: object) -> Decimal:
    original = str(value or "").strip().replace(",", ".")
    if not original:
        raise ValueError("Punteggio non valorizzato")
    try:
        number = Decimal(original)
    except InvalidOperation as error:
        raise ValueError("Punteggio non numerico") from error
    if not number.is_finite() or number.as_tuple().exponent < -2:
        raise ValueError("Punteggio non valido")
    return number


def field_value(fields: dict, key: str) -> str:
    data = fields.get(key)
    if not data:
        raise ValueError("Campo obbligatorio mancante: " + key)
    value = data.get("/V")
    if value is None:
        raise ValueError("Campo privo di valore: " + key)
    return str(value)


def evaluate_pdf(contents: bytes, expected_code: str, candidates: list[dict],
                 already: dict[int, dict]) -> dict:
    """Restituisce una scheda classificata senza persistere alcun punteggio.

    candidates: DB read-only PICA + modello SSD, include la chiave integra.
    already: ultima versione acquisita e SHA di ogni PDF del bando.
    """
    digest = sha256(contents).hexdigest()
    result = {
        "sha256": digest, "stato": "da_verificare", "motivo": "",
        "candidatura_id": None, "id_pica": "", "cds": "", "sede": "",
        "incarico": "", "modello": "", "punteggio": None,
        "totale_pdf": None, "esito": "", "note": "", "punteggi": {},
        "versione_proposta": 1, "decisione": "da_verificare",
    }
    if digest in already.get("hashes", set()):
        # Recupera CdS e sede della scheda già conservata senza rianalizzare
        # gli stessi bytes. Serve per non superare il filtro CdS con duplicati.
        owner=already.get("hash_owner",{}).get(digest)
        candidate=next((c for c in candidates if c["id"]==owner),None)
        if candidate:
            result.update(candidatura_id=candidate["id"],id_pica=candidate["id_pica"],
                          cds=candidate["cds"],sede=candidate["sede"],
                          incarico=candidate["insegnamento"]+" / "+candidate["modulo"],
                          modello=candidate["modello"])
        result.update(stato="duplicato",motivo="PDF già acquisito o conservato in precedenza")
        return result
    if len(contents) > MAX_PDF_BYTES or not contents.startswith(b"%PDF-"):
        result["motivo"] = "Il file non è un PDF valido o supera 5 MB"
        return result
    try:
        reader = PdfReader(BytesIO(contents), strict=False)
        fields = reader.get_fields() or {}
        if not fields or len(reader.pages) != 1:
            raise ValueError("Scheda non editabile, campi mancanti oppure numero di pagine inatteso")
        marker = field_value(fields, "RANKUP-ID").strip()
        possible = [
            c for c in candidates if
            marker == "RU-" + expected_code + "-" + c["id_pica"] + "-" + c["key"][:10]
        ]
        if len(possible) != 1:
            raise ValueError("Identificativo RankUp non riconosciuto o non univoco per il bando")
        candidate = possible[0]
        result.update({
            "candidatura_id": candidate["id"],
            "id_pica": candidate["id_pica"],
            "cds": candidate["cds"], "sede": candidate["sede"],
            "incarico": candidate["insegnamento"] + " / " + candidate["modulo"],
            "modello": candidate["modello"],
        })
        for key, expected in (
            ("ID-DOMANDA", candidate["id_pica"]), ("COGNOME", candidate["cognome"]),
            ("NOME", candidate["nome"]), ("CF", candidate["codice_fiscale"]),
            ("CDS", candidate["cds"]), ("SEDE", candidate["sede"]),
            ("INSEGNAMENTO", candidate["insegnamento"]), ("MODULO", candidate["modulo"]),
        ):
            value = field_value(fields, key)
            if norm(value) != norm(expected):
                raise ValueError("Dati della candidatura discordanti: " + key)
        if candidate["stato"] != "Attiva":
            raise ValueError("Candidatura non attiva: " + candidate["stato"])
        model = candidate["modello"]
        if model not in ("professionalizzante","ordinario","inglese"):
            raise ValueError("SSD non classificato per la generazione PDF")
        maximums = (PROFESSIONAL_MAX if model=="professionalizzante"
                    else ENGLISH_MAX if model=="inglese" else ORDINARY_MAX)
        marker = str((fields.get("RANKUP-MODEL") or {}).get("/V") or "").strip()
        if model == "inglese":
            if marker != "inglese" or "P13" not in fields or "P14" in fields:
                raise ValueError("Il modello PDF non corrisponde allo SSD di Inglese")
        elif marker == "inglese":
            raise ValueError("Il modello PDF di Inglese non corrisponde allo SSD")
        elif model == "professionalizzante" and "P19" not in fields:
            raise ValueError("Il modello PDF non corrisponde allo SSD professionalizzante")
        elif model == "ordinario" and "P19" in fields:
            raise ValueError("Il modello PDF non corrisponde allo SSD non professionalizzante")
        result["esito"] = field_value(fields, "STATUS").strip()
        result["note"] = field_value(fields, "NOTE-COMM").strip()
        # L'assenza dell'esito NON deve impedire di leggere i punti:
        # il totale viene estratto come valore provvisorio, ma la scheda
        # rimane Da verificare finché la commissione non decide.
        esito_negativo = result["esito"] in (ESITI_NON_IDONEI + ESITI_ESCLUSI)
        if esito_negativo:
            result["decisione"] = classify_esito(result["esito"])
            if not result["note"]:
                raise ValueError("Motivazione della non idoneità o esclusione obbligatoria")
            # Le decisioni negative non ricevono un punteggio fittizio (0).
            # Il PDF resta acquisibile anche senza campi numerici leggibili.
            result["punteggio"] = None
            result["punteggi"] = {}
            result["totale_pdf"] = ""
        else:
            scores = {}
            for name, maximum in maximums.items():
                value = dec(field_value(fields, name))
                if value < 0 or value > Decimal(maximum):
                    raise ValueError(f"{name} supera il massimale previsto ({maximum})")
                if model == "inglese" and name in ("P02", "P05") and value not in (
                    Decimal("0"), Decimal(maximum)
                ):
                    raise ValueError(
                        f"{name}: punteggio fisso previsto dal modello ({maximum}); ammessi solo 0 o {maximum}"
                    )
                if model != "inglese" and name in ("P14", "P18") and maximum == 3 and value not in (0, 3):
                    raise ValueError(f"{name} ammette soltanto 0 o 3 punti")
                scores[name] = value
            if candidate["modello"] == "professionalizzante":
                for i in range(5):
                    activity = dec(field_value(fields, f"attivita{i}"))
                    extra = dec(field_value(fields, f"plus{i}"))
                    if activity not in (Decimal("0"),Decimal("0.25"),Decimal("0.5"),
                                        Decimal("1"),Decimal("1.5")) or extra not in (Decimal("0"),Decimal("0.5")):
                        raise ValueError("Punteggio attività professionale non ammesso")
                    subtotal = activity + extra
                    reported_subtotal = dec(field_value(fields, f"extraField{i}"))
                    if subtotal != reported_subtotal:
                        raise ValueError(f"Subtotale dell'anno {i + 1} non aggiornato nel PDF")
                    scores[f"attivita{i}"] = activity
                    scores[f"plus{i}"] = extra
                    scores[f"extraField{i}"] = subtotal
                areas = [
                    sum(scores[f"P{i:02d}"] for i in range(1, 7)),
                    sum(scores[f"extraField{i}"] for i in range(5)),
                    sum(scores[f"P{i:02d}"] for i in range(14, 17)),
                    scores["P17"], scores["P18"], scores["P19"],
                ]
                limits = (15, 10, 15, 7, 3, 10)
            elif model=="inglese":
                areas = [
                    sum(scores[f"P{i:02d}"] for i in range(1,8)),
                    sum(scores[f"P{i:02d}"] for i in range(8,12)),
                    scores["P12"], scores["P13"],
                ]
                limits = (13,20,10,7)
            else:
                areas = [
                    sum(scores[f"P{i:02d}"] for i in range(1, 9)),
                    scores["P09"],
                    sum(scores[f"P{i:02d}"] for i in range(10, 13)),
                    scores["P13"], scores["P14"], scores["P15"],
                ]
                limits = (15, 10, 15, 10, 3, 7)
            for idx, (amount, limit) in enumerate(zip(areas, limits), 1):
                if amount > limit:
                    raise ValueError(f"L'area {idx} supera il massimale di {limit} punti")
            total = sum(areas, Decimal("0"))
            ceiling = 50 if model=="inglese" else 60
            if total > ceiling:
                raise ValueError("Punteggio totale superiore a "+str(ceiling))
            result["punteggio"] = float(total)
            result["punteggi"] = {k: str(v) for k, v in scores.items()}
            result["totale_pdf"] = field_value(fields, "TOT").strip()
            if dec(result["totale_pdf"]) != total:
                raise ValueError("Totale PDF non aggiornato: ricalcolato " + str(total) +
                                 " contro " + result["totale_pdf"])
            # Solo l'esito espresso nel PDF trasforma un numero in valutazione
            # convalidabile. Un totale di 3 senza esito resta 3 provvisorio.
            result["decisione"] = classify_esito(result["esito"])
        if digest in already.get("hashes", set()):
            result.update(stato="duplicato",motivo="PDF già acquisito o conservato in precedenza")
        elif candidate["id"] in already.get("active", {}):
            old = already["active"][candidate["id"]]
            result.update(stato="rettifica",motivo="Esiste già una scheda acquisita: nuova versione da confermare",
                          versione_proposta=old["versione"] + 1)
        else:
            result.update(stato="regolare",motivo="Controlli superati")
    except Exception as error:
        # I documenti illegibili sono conservati come Da verificare in fase di conferma.
        result["stato"] = "da_verificare"
        result["motivo"] = str(error)[:400] if isinstance(error, (ValueError, KeyError)) else "PDF non leggibile o danneggiato"
    return result
