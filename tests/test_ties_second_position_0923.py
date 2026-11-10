"""Parità alla seconda posizione e invalidazione dell'ordine dopo rettifica.

Tutti i dati sono esclusivamente sintetici, senza documenti reali.
"""
import json
import sqlite3

from graduatorie import build_graduatoria


def _db():
    db=sqlite3.connect(":memory:")
    db.row_factory=sqlite3.Row
    db.executescript("""
        CREATE TABLE incarichi (
            id INTEGER PRIMARY KEY, bando_id INTEGER, key TEXT,
            cds TEXT, sede TEXT, insegnamento TEXT, modulo TEXT,
            ssd TEXT, id_copertura TEXT
        );
        CREATE TABLE candidature (
            id INTEGER PRIMARY KEY, incarico_id INTEGER, id_pica TEXT,
            nome TEXT, cognome TEXT, stato TEXT
        );
        CREATE TABLE commissioni (
            bando_id INTEGER, cds TEXT, sede TEXT,
            presidente TEXT, componenti TEXT
        );
        CREATE TABLE valutazioni_pdf (
            id INTEGER PRIMARY KEY,candidatura_id INTEGER,stato TEXT,
            attiva INTEGER,decisione TEXT,esito TEXT,
            totale_calcolato REAL,note TEXT
        );
        CREATE TABLE graduatorie_vincitori_proposti (
            bando_id INTEGER,incarico_id INTEGER,candidatura_id INTEGER,
            motivazione TEXT,riferimento TEXT
        );
        CREATE TABLE graduatorie_parita_risoluzioni (
            bando_id INTEGER,incarico_id INTEGER,punteggio TEXT,
            ordine_json TEXT,valutazioni_json TEXT,
            motivazione TEXT,riferimento TEXT
        );
        INSERT INTO incarichi VALUES (1,1,'J1','CdS fittizio','Sede fittizia',
            'Insegnamento sintetico','Modulo sintetico','SSD TEST','C1');
        INSERT INTO commissioni VALUES (1,'CdS fittizio','Sede fittizia',
            'Presidente fittizio','Componente fittizio');
        INSERT INTO candidature VALUES (1,1,'A','Nome A','Cognome A','Attiva');
        INSERT INTO candidature VALUES (2,1,'B','Nome B','Cognome B','Attiva');
        INSERT INTO candidature VALUES (3,1,'C','Nome C','Cognome C','Attiva');
        INSERT INTO valutazioni_pdf VALUES (1,1,'Acquisita',1,'idoneo','IDONEO',50,'');
        INSERT INTO valutazioni_pdf VALUES (2,2,'Acquisita',1,'idoneo','IDONEO',45,'');
        INSERT INTO valutazioni_pdf VALUES (3,3,'Acquisita',1,'idoneo','IDONEO',45,'');
    """)
    return db


def test_second_position_tie_is_detected_ordered_then_invalidated_by_new_sheet():
    with _db() as db:
        before=build_graduatoria(db,1,"CdS fittizio","Sede fittizia")
        group=before["incarichi"][0]
        assert group["vincitore_id"]==1
        assert not before["pronta"]
        assert len(group["parita"])==1
        assert group["parita"][0]["posizione"]==2
        assert {c["posizione"] for c in group["candidature"] if c["candidatura_id"] in (2,3)}=={2}
        db.execute("""INSERT INTO graduatorie_parita_risoluzioni
            (bando_id,incarico_id,punteggio,ordine_json,valutazioni_json,
             motivazione,riferimento) VALUES(1,1,'45.00',?,?,?,?)""",
            (json.dumps([3,2]),json.dumps({"2":2,"3":3}),
             "Precedenza definita dalla Commissione","Verbale sintetico"))
        after=build_graduatoria(db,1,"CdS fittizio","Sede fittizia")
        assert after["pronta"]
        group=after["incarichi"][0]
        assert group["vincitore_id"]==1
        assert group["parita"]==[]
        positions={c["candidatura_id"]:c["posizione"] for c in group["candidature"]}
        assert positions=={1:1,2:3,3:2}
        # Una nuova scheda cambia l'identificativo di valutazione: l'ordine
        # precedente non può essere applicato automaticamente.
        db.execute("UPDATE valutazioni_pdf SET id=30 WHERE id=3")
        stale=build_graduatoria(db,1,"CdS fittizio","Sede fittizia")
        assert not stale["pronta"]
        assert stale["incarichi"][0]["parita"][0]["posizione"]==2
