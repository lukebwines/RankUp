"""Ricerca storica tramite collegamento candidature → schede e CF PICA."""
from io import BytesIO
from openpyxl import load_workbook
from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all
from test_import import pica_fixture


def _search(client,term,criterion="tutti",page=1):
    return _post(client,"/api/archivio/ricerca-candidati",
                 json={"testo":term,"criterio":criterion,"pagina":page})


def _import_other_bando(client):
    workbook=load_workbook(BytesIO(pica_fixture()))
    workbook.active["A1"]="univr 26-03 - Esportazione dati sintetici"
    out=BytesIO()
    workbook.save(out)
    preview=_post(client,"/api/pica/preview",
        files={"file":("secondo_bando_sintetico.xlsx",out.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert preview.status_code==200,preview.text
    imported=_post(client,"/api/pica/commit",json={"token":preview.json()["token"]})
    assert imported.status_code==200,imported.text


def test_cf_pica_joins_multiple_candidatures_and_sheets(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        _complete_all(client,sheets)
        cf=_search(client,"CODICEFISCALE","cf")
        assert cf.status_code==200,cf.text
        rows=cf.json()["risultati"]
        assert len(rows)==2
        assert {r["id_pica"] for r in rows}=={"1001"}
        assert {r["codice_bando"] for r in rows}=={"26-02"}
        assert all(r["valutazione_id"] is not None and r["scheda_corrente"] for r in rows)
        assert all(r["cf_mascherato"].endswith("CALE") for r in rows)
        assert "CODICEFISCALE" not in cf.text
        assert all(r["incarico_key"] for r in rows)
        assert _search(client,"codicefiscale","cf").status_code==200
        assert len(_search(client,"Candidato","cognome").json()["risultati"])==3
        assert len(_search(client,"1001","id_pica").json()["risultati"])==2
        assert _search(client,"A","cf").status_code==400

        _import_other_bando(client)
        across=_search(client,"CODICEFISCALE","cf")
        assert across.status_code==200,across.text
        assert across.json()["candidature_totali"]==4
        assert {r["codice_bando"] for r in across.json()["risultati"]}=={"26-02","26-03"}
        assert sum(r["valutazione_id"] is None for r in across.json()["risultati"])==2
        # La corrispondenza si basa sui CF PICA, non sui singoli ID di domanda.


def test_storico_conserva_versioni_e_omette_CF_intero(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        _complete_all(client,sheets)
        # Simula una versione storica di una scheda della stessa candidatura,
        # senza cancellare né alterare gli originali PDF.
        with server.connect() as db:
            r=db.execute("SELECT * FROM valutazioni_pdf WHERE attiva=1 LIMIT 1").fetchone()
            db.execute("""UPDATE valutazioni_pdf SET attiva=0 WHERE id=?""",(r["id"],))
            db.execute("""INSERT INTO valutazioni_pdf
                (bando_id,candidatura_id,stato,versione,attiva,sha256,
                 nome_originale,percorso,modello,punteggi_json,totale_calcolato,
                 totale_pdf,esito,note,motivo,confermata_da,confermata_il,
                 decisione,metodo,motivazione_tecnica)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["bando_id"],r["candidatura_id"],r["stato"],r["versione"]+1,1,
                 "test-synthetic-revision-hash",r["nome_originale"],r["percorso"],
                 r["modello"],r["punteggi_json"],r["totale_calcolato"],
                 r["totale_pdf"],r["esito"],r["note"],r["motivo"],
                 r["confermata_da"],r["confermata_il"],r["decisione"],
                 r["metodo"],r["motivazione_tecnica"]))
            db.commit()
        found=_search(client,"CODICEFISCALE","cf")
        assert found.status_code==200,found.text
        same=found.json()["risultati"]
        assert len(same)==3
        assert sum(r["scheda_corrente"] for r in same)==2
        assert any(not r["scheda_corrente"] and r["valutazione_id"] for r in same)
        assert all("codice_fiscale" not in r for r in same)
