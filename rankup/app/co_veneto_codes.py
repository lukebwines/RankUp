"""Codici catastali dei soli Comuni italiani per le comunicazioni CO Veneto.

L'archivio locale ANPR è incluso in python-codicefiscale==0.12.1.
Il matching è conservativo: nessuna ricerca approssimativa e nessun codice
attribuito automaticamente per nazioni o località estere.
"""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata

from codicefiscale import codicefiscale
from codicefiscale.data import get_municipalities_data

CAT_CODE = re.compile(r"^[A-Z][0-9]{3}$")
ITALIAN_COUNTRIES = {"", "ITALIA", "ITALY", "IT"}


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "").strip().upper())
    return re.sub(r"[^A-Z0-9]", "", "".join(
        letter for letter in value if not unicodedata.combining(letter)
    ))


def code(value):
    value = str(value or "").strip().upper()
    return value if CAT_CODE.fullmatch(value) else ""


@lru_cache(maxsize=1)
def comuni():
    """Fotografia locale dei Comuni: nessuna comunicazione esterna."""
    return tuple(get_municipalities_data())


def comune_nascita(source):
    """Codice validato dal codice fiscale e dal Comune dichiarato in PICA."""
    city = str(source.get("comune_nascita") or "").strip()
    fiscal = str(source.get("codice_fiscale") or "").strip().upper()
    if not city or not fiscal:
        return ""
    try:
        decoded = codicefiscale.decode(fiscal)
        place = decoded.get("birthplace") or {}
        if not isinstance(place, dict) or place.get("province") == "EE":
            return ""
        cod = code(place.get("code"))
        if not cod or norm(place.get("name")) != norm(city):
            return ""
        declared_sex = str(source.get("sesso") or "").strip().upper()
        if declared_sex in ("M", "F") and decoded.get("gender") != declared_sex:
            return ""
        # Il codice fiscale è un controllo, non una fonte alternativa che
        # prevale su dichiarazioni PICA discordanti.
        return cod
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return ""


def comune_domicilio(source):
    """Comune italiano attuale individuato da città, provincia e nazione PICA.

    Se il campo provincia contiene una sigla (es. VR), questa è obbligatoria.
    Se è esteso o mancante, si richiede comunque un'unica corrispondenza
    nazionale: nessun toponimo ambiguo viene precompilato.
    """
    city = str(source.get("residenza_citta") or "").strip()
    province = str(source.get("residenza_provincia") or "").strip().upper()
    nation = norm(source.get("residenza_nazione"))
    # Alcune esportazioni PICA riportano "VERONA (VR)" come unico valore
    # nel campo Comune. La sigla provinciale e' parte del dato dichiarato,
    # non un codice ipotizzato: va estratta prima del matching anagrafico.
    embedded = re.fullmatch(r"(.+?)\\s*\\(\\s*([A-Z]{2})\\s*\\)", city, re.IGNORECASE)
    if embedded:
        declared_province = embedded.group(2).upper()
        # Segnalare l'incongruenza, non scegliere arbitrariamente fra le due.
        if re.fullmatch(r"[A-Z]{2}", province) and province != declared_province:
            return ""
        city = embedded.group(1).strip()
        province = declared_province
    # Se manca lo Stato, occorre almeno la provincia italiana: senza
    # entrambe le fonti non si può presumere che la località sia in Italia.
    if (not city or nation not in ITALIAN_COUNTRIES or
            (not nation and not re.fullmatch(r"[A-Z]{2}", province))):
        return ""
    if re.match(r"^[A-Z][0-9]{3}$", city.upper()):
        return ""
    matches = set()
    for item in comuni():
        if item.get("date_deleted"):
            continue
        if norm(item.get("name")) != norm(city):
            continue
        if re.fullmatch(r"[A-Z]{2}", province) and item.get("province", "").upper() != province:
            continue
        value = code(item.get("code"))
        if value:
            matches.add(value)
    return next(iter(matches)) if len(matches) == 1 else ""
