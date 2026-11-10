"""Graduatoria generale compatta: indice, metadati PICA e dati approvati."""
from copy import deepcopy
from io import BytesIO
import sqlite3

from pypdf import PdfReader
from graduatorie import render_graduatoria_pdf, enrich_print_metadata


def _scope(number, with_second=False):
    winner={
        "candidatura_id":number*100+1,"id_pica":str(800000+number),
        "nome":"Primo","cognome":"Candidato",
        "decisione":"idoneo","posizione":1,"punteggio":"12.00",
        "esito":"CANDIDATO AMMESSO IN GRADUATORIA","note":"",
    }
    candidates=[winner]
    if with_second:
        candidates.append({
            "candidatura_id":number*100+2,"id_pica":str(900000+number),
            "nome":"Secondo","cognome":"Candidato",
            "decisione":"idoneo","posizione":2,"punteggio":"10.00",
            "esito":"CANDIDATO AMMESSO IN GRADUATORIA","note":"",
        })
    return {
        "cds":"Corso sintetico "+str(number).zfill(2)+
              " (ABILITANTE ALLA PROFESSIONE SANITARIA)",
        "sede":"Sede sintetica",
        "incarichi":[{
            "key":"chiave-"+str(number),"insegnamento":"Laboratori professionali",
            "modulo":"Laboratori professionali","ssd":"MED/50",
            "ul":"UL "+str(number) if number==1 else "",
            "partizione_studenti":"Canale A" if number==1 else "",
            "codice_partizione":"A1" if number==1 else "",
            "vincitore_id":winner["candidatura_id"],
            "candidature":candidates,
        }],
    }


def _text(content):
    return "\n".join(x.extract_text() or "" for x in PdfReader(BytesIO(content)).pages)


def test_general_header_partial_index_compact_and_identifications():
    snapshot={"selezioni":[_scope(1,True),_scope(2)],
              "totale_ambiti_bando":4}
    public=render_graduatoria_pdf(
        "DEMO-26",snapshot,general=True,approved=True,
        reference="RIFERIMENTO SINTETICO DI COLLAUDO")
    text=_text(public)
    assert text.startswith("Graduatoria\n")
    assert "Bando: DEMO-26 — Graduatoria parziale" in text
    assert "Stato: ATTI APPROVATI" in text
    assert "Indice CdS e sedi" in text
    assert "CdS: Corso sintetico 01" in text
    assert "ABILITANTE ALLA PROFESSIONE SANITARIA" not in text
    assert "UL: UL 1" in text
    assert "Partizione: Canale A (cod. A1)" in text
    assert "Insegnamento / Modulo:" in text
    assert "Candidato Primo (ID PICA 800001)" in text
    assert "ID PICA 900001" in text
    assert "Candidato Secondo" not in text
    assert "Estremi del provvedimento" not in text
    assert "Documento redatto esclusivamente" not in text
    assert "RIFERIMENTO SINTETICO" not in text
    pdf=PdfReader(BytesIO(public))
    annotations=[a.get_object() for page in pdf.pages
                 for a in (page.get("/Annots") or [])]
    assert any(x.get("/Subtype")=="/Link" for x in annotations)

    private=render_graduatoria_pdf("DEMO-26",snapshot,general=True,
                                   approved=True,internal=True)
    assert "Candidato Secondo" in _text(private)


def test_general_complete_has_no_partial_label():
    pdf=render_graduatoria_pdf(
        "DEMO-26",{"selezioni":[_scope(1),_scope(2)],
                  "totale_ambiti_bando":2},
        general=True,approved=True)
    text=_text(pdf)
    assert "Bando: DEMO-26" in text
    assert "Graduatoria parziale" not in text
    assert text.startswith("Graduatoria\n")


def test_many_cds_keep_index_compact_and_last_course():
    scopes=[_scope(i) for i in range(1,44)]
    pdf=render_graduatoria_pdf(
        "DEMO-26",{"selezioni":scopes,"totale_ambiti_bando":43},
        general=True,approved=True)
    pages=PdfReader(BytesIO(pdf)).pages
    text="\n".join(page.extract_text() or "" for page in pages)
    assert "Indice CdS e sedi" in text
    assert "43. Corso sintetico 43" in text
    assert "CdS: Corso sintetico 43" in text
    assert len(pages)<20  # Guardrail contro spaziature eccessive.


def test_old_snapshot_enrichment_readonly_and_preserves_provided_values():
    db=sqlite3.connect(":memory:")
    db.row_factory=sqlite3.Row
    db.execute("""CREATE TABLE incarichi (
        bando_id INTEGER, key TEXT, cds TEXT, sede TEXT, ul TEXT,
        codice_partizione TEXT, partizione_studenti TEXT)""")
    db.executemany("INSERT INTO incarichi VALUES (?,?,?,?,?,?,?)",[
        (1,"chiave-1","Corso 1","Verona","UL 1","P-A","Partizione A"),
        (1,"chiave-2","Corso 2","Trento","","",""),
    ])
    old={"selezioni":[{"cds":"Corso 1","sede":"Verona",
                      "incarichi":[{"key":"chiave-1","insegnamento":"Lab",
                                   "modulo":"Lab","ssd":"MED/50","candidature":[]}]}]}
    pristine=deepcopy(old)
    enriched=enrich_print_metadata(db,1,old)
    assert old==pristine
    assert enriched["totale_ambiti_bando"]==2
    job=enriched["selezioni"][0]["incarichi"][0]
    assert job["ul"]=="UL 1"
    assert job["partizione_studenti"]=="Partizione A"
    assert job["codice_partizione"]=="P-A"
    old["selezioni"][0]["incarichi"][0]["ul"]="UL storica"
    assert enrich_print_metadata(db,1,old)["selezioni"][0]["incarichi"][0]["ul"]=="UL storica"
    db.close()


def test_individual_cds_pdf_shows_ul_and_partition_when_present():
    data=_scope(1,True)
    data["stato"]="pronta"
    pdf=render_graduatoria_pdf("DEMO-26",data)
    content=_text(pdf)
    assert "Graduatoria della Commissione per Corso di Studio" in content
    assert "UL: UL 1" in content
    assert "Partizione: Canale A (cod. A1)" in content
    assert "Candidato Secondo" in content
    assert "Insegnamento / Modulo:" in content
    no_fields=_text(render_graduatoria_pdf("DEMO-26",_scope(2)))
    assert "UL:" not in no_fields
    assert "Partizione:" not in no_fields


def test_individual_legacy_snapshot_fills_only_source_metadata():
    db=sqlite3.connect(":memory:")
    db.row_factory=sqlite3.Row
    db.execute("""CREATE TABLE incarichi (
        bando_id INTEGER,key TEXT,ul TEXT,
        codice_partizione TEXT,partizione_studenti TEXT)""")
    db.execute("INSERT INTO incarichi VALUES (?,?,?,?,?)",
               (1,"chiave-1","Unità sintetica","B","Canale B"))
    old=_scope(1)
    old["incarichi"][0].pop("ul")
    old["incarichi"][0].pop("partizione_studenti")
    old["incarichi"][0].pop("codice_partizione")
    original=deepcopy(old)
    printable=enrich_print_metadata(db,1,old)
    assert old==original
    text=_text(render_graduatoria_pdf("DEMO-26",printable))
    assert "UL: Unità sintetica" in text
    assert "Partizione: Canale B (cod. B)" in text
    db.close()
