"""Collaudi delle protezioni amministrative con dati esclusivamente sintetici."""
import hashlib
import json
import sqlite3

from graduatorie import build_graduatoria
from procedural_safety import rettifica_sostanziale, anomalie_approvazione


def _db_parita():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE incarichi (
          id INTEGER,bando_id INTEGER,key TEXT,cds TEXT,sede TEXT,
          insegnamento TEXT,modulo TEXT,ssd TEXT,id_copertura TEXT,
          ul TEXT,codice_partizione TEXT,partizione_studenti TEXT);
        CREATE TABLE candidature (
          id INTEGER,incarico_id INTEGER,id_pica TEXT,cognome TEXT,
          nome TEXT,stato TEXT);
        CREATE TABLE valutazioni_pdf (
          id INTEGER,candidatura_id INTEGER,decisione TEXT,esito TEXT,
          totale_calcolato REAL,note TEXT,stato TEXT,attiva INTEGER);
        CREATE TABLE commissioni (
          bando_id INTEGER,cds TEXT,sede TEXT,presidente TEXT,componenti TEXT);
        CREATE TABLE graduatorie_vincitori_proposti (
          bando_id INTEGER,incarico_id INTEGER,candidatura_id INTEGER,
          motivazione TEXT,riferimento TEXT);
        CREATE TABLE graduatorie_parita_risoluzioni (
          bando_id INTEGER,incarico_id INTEGER,punteggio TEXT,
          ordine_json TEXT,valutazioni_json TEXT,motivazione TEXT,
          riferimento TEXT);
        INSERT INTO incarichi VALUES
          (10,1,'chiave-sintetica','CdS TEST','Sede TEST',
           'Corso sintetico','Modulo sintetico','NN','',
           '','','');
        INSERT INTO candidature VALUES
          (1,10,'PICA-TEST-1','Candidato','Uno','Attiva'),
          (2,10,'PICA-TEST-2','Candidato','Due','Attiva');
        INSERT INTO valutazioni_pdf VALUES
          (11,1,'idoneo','IDONEO',25.0,'Nota sintetica','Acquisita',1),
          (12,2,'idoneo','IDONEO',25.0,'Nota sintetica','Acquisita',1);
        INSERT INTO graduatorie_vincitori_proposti VALUES
          (1,10,1,'Decisione precedente sintetica','Documento test');
    """)
    return db


def test_vecchia_proposta_vincitore_non_risolve_parita():
    with _db_parita() as db:
        view = build_graduatoria(db, 1, "CdS TEST", "Sede TEST")
        incarico = view["incarichi"][0]
        assert incarico["vincitore_id"] is None
        assert not incarico["pronta"]
        assert len(incarico["parita"]) == 1


def test_risoluzione_parita_legata_alle_versioni_delle_schede():
    with _db_parita() as db:
        db.execute("""INSERT INTO graduatorie_parita_risoluzioni VALUES
          (1,10,'25.00',?,?,?,?)""", (
            json.dumps([2,1]), json.dumps({"1":11,"2":12}),
            "Commissione sintetica", "Verbale sintetico"))
        ok = build_graduatoria(db, 1, "CdS TEST", "Sede TEST")
        assert ok["incarichi"][0]["vincitore_id"] == 2
        assert ok["pronta"] is True
        db.execute("UPDATE valutazioni_pdf SET id=13 WHERE id=12")
        new = build_graduatoria(db, 1, "CdS TEST", "Sede TEST")
        assert new["incarichi"][0]["vincitore_id"] is None
        assert not new["pronta"]


def _snapshot(score="25.00", evaluation=11):
    return {"incarichi": [{
        "id": 10, "vincitore_id": 1,
        "candidature": [{
            "candidatura_id": 1, "valutazione_id": evaluation,
            "decisione": "idoneo", "esito": "IDONEO",
            "punteggio": score, "posizione": 1, "note": "Nota sintetica"
        }]
    }]}


def test_rettifica_sostanziale_rileva_modifica_scheda_o_merito():
    old = _snapshot()
    assert not rettifica_sostanziale(old, _snapshot())
    assert rettifica_sostanziale(old, _snapshot(evaluation=22))
    assert rettifica_sostanziale(old, _snapshot(score="24.00"))


def test_preflight_approvazione_verifica_hash_e_dati():
    before = _snapshot()
    db_text = json.dumps(before, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(db_text.encode("utf-8")).hexdigest()
    pdf = b"%PDF sinteticamente non valido, solo test hash"
    pdf_hash = hashlib.sha256(pdf).hexdigest()
    current = dict(_snapshot(), verbale_generabile=True)
    assert anomalie_approvazione(
        before, current, db_text, digest, pdf, pdf_hash) == []
    assert "Integrità del PDF definitivo non verificata" in anomalie_approvazione(
        before, current, db_text, digest, pdf + b"alterato", pdf_hash)
    assert "Le valutazioni attuali differiscono dai dati definitivi" in anomalie_approvazione(
        before, dict(_snapshot(score="24.00"), verbale_generabile=True),
        db_text, digest, pdf, pdf_hash)
    assert "Schede, Commissione o parità non più conformi" in anomalie_approvazione(
        before, dict(_snapshot(), verbale_generabile=False),
        db_text, digest, pdf, pdf_hash)
