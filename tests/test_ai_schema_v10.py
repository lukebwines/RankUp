"""Migrazione SQLite v9->v10: bug HTTP 500 nella rianalisi mirata.

Riproduce un database persistente che dispone della coda AI v9, ma non
delle colonne aggiunte in RankUP 1.0.17. Dati interamente sintetici.
"""
import sqlite3

from db_lifecycle import SCHEMA_VERSION
from test_ai_queue_v109 import _prepare
from test_pdf_import import _post


NEW_COLS=("modello_attivo","modello_riserva",
          "failover_attivo","limiti_modelli_json")


def _ai_result(key,model,payload,guidance=None):
    return {"indizio":"INDIZI COERENTI",
            "motivo":"Ente sintetico coerente",
            "verifiche":"Verificare rapporto individuale",
            "origine":"groqcloud","token_usati":82}


def test_existing_v9_database_is_backed_up_and_migrated_non_destructively(tmp_path,monkeypatch):
    assert SCHEMA_VERSION==10
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        created=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert created.status_code==200,created.text
        for _ in range(5):
            server.ai_process_one(server.connect,server._ai_worker_candidates,
                                  _ai_result,server.GROQ.key,server.GROQ.limits)
        previous=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()["lavoro"]
        assert previous["stato"]=="completata"
        previous_id=previous["id"]
        with server.connect() as db:
            outcomes=db.execute(
                "SELECT id,bando_id,candidatura_id,incarico_id,indizio FROM ai_ente_ai_esiti ORDER BY id"
            ).fetchall()
            assert outcomes
            expected=[tuple(r) for r in outcomes]
            # Questa è la situazione dell'installazione reale: PRAGMA v9,
            # senza colonne aggiunte dalla release 1.0.17.
            for col in NEW_COLS:
                db.execute("ALTER TABLE ai_ente_lavori DROP COLUMN "+col)
            db.execute("PRAGMA user_version=9")
            db.commit()
            before={r["name"] for r in db.execute("PRAGMA table_info(ai_ente_lavori)")}
            assert not set(NEW_COLS)&before
            assert db.execute("PRAGMA user_version").fetchone()[0]==9
        server.initialize()  # la release nuova deve correggere il DB prima di qualsiasi API
        backups=list((tmp_path/"schema_backups").glob("rankup_pre_schema_v9_*.sqlite3"))
        assert len(backups)==1
        with sqlite3.connect(backups[0]) as backup:
            before={r[1] for r in backup.execute("PRAGMA table_info(ai_ente_lavori)")}
            assert "modello_attivo" not in before
            assert backup.execute("PRAGMA user_version").fetchone()[0]==9
        with server.connect() as db:
            after={r["name"] for r in db.execute("PRAGMA table_info(ai_ente_lavori)")}
            assert set(NEW_COLS).issubset(after)
            assert db.execute("PRAGMA user_version").fetchone()[0]==10
            historical=db.execute(
                "SELECT * FROM ai_ente_lavori WHERE id=?",(previous_id,)).fetchone()
            assert historical["stato"]=="completata"
            assert historical["modello_attivo"]==""
            assert historical["modello_riserva"]==""
            assert historical["failover_attivo"]==0
            actual=[tuple(r) for r in db.execute(
                "SELECT id,bando_id,candidatura_id,incarico_id,indizio FROM ai_ente_ai_esiti ORDER BY id"
            )]
            assert actual==expected
        chosen=[r["id"] for r in client.get("/api/admin/ai/verifiche",params={
            "code":"26-02"}).json()["casi"][:2]]
        assert len(chosen)==2
        retry=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True,
            "ids_candidature":chosen,"forza":True})
        assert retry.status_code==200,retry.text
        assert retry.json()["totale_candidature"]==2
        # Non creare un secondo backup allo stesso schema con riavvii successivi.
        server.initialize()
        assert len(list((tmp_path/"schema_backups").glob("rankup_pre_schema_v9_*.sqlite3")))==1
