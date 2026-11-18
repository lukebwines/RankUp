"""Regressione CO Veneto v1.0.43: solo codici italiani verificabili.

I dati sono sintetici e non devono essere utilizzati come atti.
"""
from codicefiscale import codicefiscale

from co_veneto import citizenship_from_pica, defaults_from_pica, work_values
from co_veneto_codes import comune_nascita, comune_domicilio


def _italian_source():
    fiscal = codicefiscale.encode(
        lastname="Rossi", firstname="Marta", gender="F",
        birthdate="01/01/1990", birthplace="Verona"
    )
    return {
        "codice_fiscale": fiscal,
        "nome": "Marta", "cognome": "Rossi",
        "sesso": "F", "data_nascita": "01/01/1990",
        "comune_nascita": "Verona",
        "cittadinanza_italiana": "Sì", "cittadinanza": "Italia",
        "residenza_indirizzo": "VIA DI PROVA 1",
        "residenza_citta": "Verona",
        "residenza_provincia": "VR",
        "residenza_cap": "37100",
        "residenza_nazione": "Italia",
    }


def test_codes_and_education_prefilled_from_italian_pica():
    data = _italian_source()
    assert comune_nascita(data) == "L781"
    assert comune_domicilio(data) == "L781"
    output = defaults_from_pica(data)
    assert output["nascita_comune_codice"] == "L781"
    assert output["domicilio_comune_codice"] == "L781"
    assert output["cittadinanza_codice"] == "000"
    assert output["livello_istruzione"] == "70"


def test_ambiguous_or_inconsistent_values_are_not_invented():
    data = _italian_source()
    data["comune_nascita"] = "Vicenza"
    assert comune_nascita(data) == ""
    data["residenza_nazione"] = "Francia"
    assert comune_domicilio(data) == ""
    data["cittadinanza_italiana"] = "No"
    assert citizenship_from_pica(data) == ""


def test_saved_legacy_empty_codes_do_not_erase_new_defaults():
    data = _italian_source()
    saved = {
        "domicilio_comune_codice": "",
        "nascita_comune_codice": "",
        "cittadinanza_codice": "",
        "livello_istruzione": "",
    }
    work = work_values(data, saved)
    assert work["domicilio_comune_codice"] == "L781"
    assert work["nascita_comune_codice"] == "L781"
    assert work["cittadinanza_codice"] == "000"
    assert work["livello_istruzione"] == "70"


def test_changed_domicile_city_requires_matching_code():
    data = _italian_source()
    code_pica = defaults_from_pica(data)["domicilio_comune_codice"]
    override = work_values(data, {
        "domicilio_citta": "Legnago",
        "domicilio_comune_codice": code_pica,
    })
    assert override["domicilio_comune_codice"] == ""
    validated_override = work_values(data, {
        "domicilio_citta": "Legnago",
        "domicilio_comune_codice": "E512",
    })
    assert validated_override["domicilio_comune_codice"] == "E512"
