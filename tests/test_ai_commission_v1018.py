"""RankUP 1.0.18: nessuna idoneità inventata, stato da schede e graduatorie.

Dati e nominativi qui presenti sono totalmente sintetici.
"""
import hashlib
import json

from ai_commission_status import describe_commission
from test_ai_queue_v109 import _prepare


def test_label_from_validated_commission_and_unique_candidate():
    base={"decisione":"idoneo","valutazione_id":7,
          "punteggio":"12.50","posizione":1,"verifica_parita":False}
    provisional=describe_commission(base)
    assert provisional["stato_graduatoria"]=="inserito"
    assert provisional["stato_commissione"]=="In graduatoria · provvisoria"
    assert provisional["posizione"]==1
    assert provisional["punteggio"]=="12.50"
    final=describe_commission(base,True)
    assert final["stato_commissione"]=="Ammesso in graduatoria"
    assert final["graduatoria_definitiva"] is True
    assert describe_commission(None)["stato_graduatoria"]=="da_valutare"
    assert describe_commission(None,pending_sheet=True)["stato_commissione"]=="In verifica"
    assert describe_commission({**base,"decisione":"non_idoneo"})["stato_commissione"]=="Non idoneo"
    assert describe_commission({**base,"decisione":"escluso"})["stato_graduatoria"]=="non_inserito"
    assert describe_commission({**base,"posizione":None})["stato_graduatoria"]=="da_valutare"
    tie=describe_commission({**base,"verifica_parita":True})
    assert tie["stato_graduatoria"]=="inserito"
    assert tie["posizione"] is None


def _add_sheet(db,bid:int,cid:int,decision:str,score:float|None):
    db.execute("""
        INSERT INTO valutazioni_pdf
        (bando_id,candidatura_id,stato,versione,attiva,sha256,nome_originale,
         percorso,modello,punteggi_json,totale_calcolato,totale_pdf,esito,
         note,motivo,confermata_da,confermata_il,decisione,metodo)
        VALUES(?,?,'Acquisita',1,1,?,'scheda_sintetica.pdf','',
               'ordinario','{}',?,'',?,'nota sintetica','','admin',
               '2026-11-13T08:00:00',?,'manuale')
    """,(bid,cid,"SYNTHETIC-SHA-"+str(cid),score,decision.upper(),decision))


def test_admin_filters_and_report_use_same_actual_decisions(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        original=client.get("/api/admin/ai/verifiche",params={"code":"26-02"})
        assert original.status_code==200,original.text
        assert all(x["commissione"]["stato_commissione"]=="Da valutare"
                   for x in original.json()["casi"])
        with server.connect() as db:
            bid,candidates=server._ai_scope(db,"26-02","","")
            first,second=candidates[:2]
            _add_sheet(db,bid,first["id"],"idoneo",13.75)
            _add_sheet(db,bid,second["id"],"non_idoneo",None)
            db.commit()
        source=client.get("/api/admin/ai/verifiche",params={"code":"26-02"})
        assert source.status_code==200,source.text
        mapped={r["id"]:r for r in source.json()["casi"]}
        assert mapped[first["id"]]["commissione"]["stato_graduatoria"]=="inserito"
        assert mapped[first["id"]]["commissione"]["stato_commissione"]=="In graduatoria · provvisoria"
        assert mapped[first["id"]]["commissione"]["posizione"]==1
        assert mapped[first["id"]]["commissione"]["punteggio"]=="13.75"
        assert mapped[second["id"]]["commissione"]["stato_commissione"]=="Non idoneo"
        assert mapped[second["id"]]["commissione"]["punteggio"] is None
        only=client.get("/api/admin/ai/verifiche",params={
            "code":"26-02","esito":"DA APPROFONDIRE","graduatoria":"inserito"})
        assert only.status_code==200,only.text
        assert only.json()["visualizzati"]==1
        assert only.json()["casi"][0]["id"]==first["id"]
        assert client.get("/api/admin/ai/verifiche",params={
            "code":"26-02","graduatoria":"non_inserito"}).json()["visualizzati"]==1
        assert client.get("/api/admin/ai/verifiche",params={
            "code":"26-02","graduatoria":"da_valutare"}).json()["visualizzati"]==1
        assert client.get("/api/admin/ai/verifiche",params={
            "code":"26-02","graduatoria":"invalido"}).status_code==400
        pdf=client.get("/api/admin/ai/report.pdf",params={
            "code":"26-02","graduatoria":"inserito"})
        assert pdf.status_code==200,pdf.text if pdf.status_code!=200 else ""
        assert pdf.headers["content-type"]=="application/pdf"
        assert client.get("/api/admin/ai/report.pdf",params={
            "code":"26-02","graduatoria":"invalido"}).status_code==400


def test_official_snapshot_shows_definitive_then_reopen_returns_provisional(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            bid,candidates=server._ai_scope(db,"26-02","","")
            sample=candidates[0]
            _add_sheet(db,bid,sample["id"],"idoneo",21.0)
            from graduatorie import build_graduatoria
            ranked=build_graduatoria(db,bid,sample["cds"],sample["sede"])
            snap=json.dumps(ranked,ensure_ascii=False,sort_keys=True)
            digest=hashlib.sha256(snap.encode()).hexdigest()
            cur=db.execute("""
                INSERT INTO graduatorie_cds
                  (bando_id,cds,sede,stato,dati_json,hash_dati,
                   confermata_da,confermata_il)
                VALUES(?,?,?,'definitiva',?,?,'admin','2026-11-13T09:00:00')
            """,(bid,sample["cds"],sample["sede"],snap,digest))
            grad_id=cur.lastrowid
            db.commit()
        shown=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()
        row=next(r for r in shown["casi"] if r["id"]==sample["id"])
        assert row["commissione"]["stato_commissione"]=="Ammesso in graduatoria"
        assert row["commissione"]["graduatoria_definitiva"] is True
        with server.connect() as db:
            revision=db.execute("""
                INSERT INTO graduatorie_cds_revisioni
                (bando_id,graduatoria_id,cds,sede,dati_json,hash_dati,
                 pdf_storico,hash_pdf,motivazione,riferimento,autore,quando)
                VALUES(?,?,?,?,?,?,'PDF-SINTETICO','SHA-SINTETICO',
                       'Riapertura di prova','Riferimento di test','admin',
                       '2026-11-13T10:00:00')
            """,(bid,grad_id,sample["cds"],sample["sede"],snap,digest))
            db.execute("""
                INSERT INTO graduatorie_cds_riaperte
                (graduatoria_id,bando_id,revisione_id) VALUES(?,?,?)
            """,(grad_id,bid,revision.lastrowid))
            db.commit()
        reread=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()
        row=next(r for r in reread["casi"] if r["id"]==sample["id"])
        assert row["commissione"]["stato_commissione"]=="In graduatoria · provvisoria"
        assert row["commissione"]["graduatoria_definitiva"] is False


def test_visual_matches_approved_filter_and_column_structure():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    html=(root/"rankup/app/static/index.html").read_text("utf-8")
    css=(root/"rankup/app/static/workspace.css").read_text("utf-8")
    for selector in ['id="ai-esito"','id="ai-tipo"','id="ai-graduatoria"','id="ai-ricerca"']:
        assert selector in html
    assert "Esito Commissione</th>" in html
    assert "Verifiche e suggerimenti</th>" not in html
    assert "ru-ai-commission-pill" in html
    assert "ru-ai-filters" in css
    assert "position:static!important" in css
