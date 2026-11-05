"""Regressioni: denominazioni PICA estese, commissioni per sede e collisioni."""
from commission_match import course_key, seat_key, commission_index, resolve_commission


def test_pica_abilitante_matches_commission_short_name():
    rows = [{
        "cds": "LOGOPEDIA",
        "sede": "VERONA",
        "presidente": "Presidente sintetico",
        "componenti": "Componente sintetico",
        "anno": "ANNO DI PROVA",
        "requisiti": "",
    }]
    index = commission_index(rows)
    status, commission = resolve_commission(
        index,
        "LOGOPEDIA (ABILITANTE ALLA PROFESSIONE SANITARIA DI LOGOPEDISTA)",
        "Verona",
    )
    assert status == "associata"
    assert commission["cds"] == "LOGOPEDIA"


def test_each_seat_gets_its_own_commission():
    rows = [
        {"cds": "INFERMIERISTICA", "sede": "VERONA",
         "presidente": "Commissione di prova A"},
        {"cds": "INFERMIERISTICA", "sede": "TRENTO",
         "presidente": "Commissione di prova B"},
    ]
    index = commission_index(rows)
    a = "INFERMIERISTICA (ABILITANTE ALLA PROFESSIONE SANITARIA DI INFERMIERE)"
    assert resolve_commission(index, a, "VERONA")[1]["presidente"] == "Commissione di prova A"
    assert resolve_commission(index, a, "TRENTO")[1]["presidente"] == "Commissione di prova B"
    assert resolve_commission(index, a, "LEGNAGO")[0] == "mancante"


def test_accent_case_and_spaces_are_harmless_but_prefixes_not_used():
    index = commission_index([{"cds": "Scienze Infermieristiche Specialistiche nelle Cure primarie e Infermieristica di famiglia e comunità",
                               "sede": "TRENTO"}])
    assert resolve_commission(
        index,
        "SCIENZE   INFERMIERISTICHE SPECIALISTICHE NELLE CURE PRIMARIE E INFERMIERISTICA DI FAMIGLIA E COMUNITA",
        "Trento",
    )[0] == "associata"
    assert resolve_commission(index, "SCIENZE INFERMIERISTICHE E OSTETRICHE", "TRENTO")[0] == "mancante"


def test_colliding_normalized_commissions_cannot_be_selected_arbitrarily():
    index = commission_index([
        {"cds": "TECNICHE AUDIOPROTESICHE", "sede": "VERONA", "presidente": "A"},
        {"cds": "TECNICHE AUDIOPROTESICHE (ABILITANTE ALLA PROFESSIONE)", "sede": "VERONA", "presidente": "B"},
    ])
    status, value = resolve_commission(
        index, "TECNICHE AUDIOPROTESICHE (ABILITANTE ALLA PROFESSIONE)", "VERONA"
    )
    assert status == "ambigua"
    assert value is None
