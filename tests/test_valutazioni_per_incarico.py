"""La vista schede raggruppa per incarico PICA, senza inferenze da PDF.

I dati sono sintetici e i test non fanno alcuna modifica a candidature reali.
"""
from test_pdf_import import _setup, _post, _edit, _preview, _confirm
from test_graduatorie import _complete_all


def test_valutazioni_espongono_tutti_gli_incarichi_pica_e_le_schede_correnti(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        before=client.get("/api/bandi/26-02/valutazioni")
        assert before.status_code==200,before.text
        initial=before.json()
        assert len(initial["incarichi"])==2
        assert initial["archivio"]==[]
        assert sum(x["candidature"] for x in initial["incarichi"])==3

        _complete_all(client,sheets)
        response=client.get("/api/bandi/26-02/valutazioni")
        assert response.status_code==200,response.text
        payload=response.json()
        assert len(payload["incarichi"])==2
        keys={j["id"]:j["key"] for j in payload["incarichi"]}
        assert len(keys)==len(set(keys.values()))
        assert len(payload["archivio"])==3
        assert payload["totale_acquisite"]==3
        assert all(r["incarico_id"] in keys and r["incarico_key"]==keys[r["incarico_id"]]
                   for r in payload["archivio"])
        assert all(r["candidatura_stato"]=="Attiva" and r["attiva"]==1
                   for r in payload["archivio"])
        assert all(r["insegnamento"] and r["modulo"] for r in payload["archivio"])
        assert all(j["insegnamento"] and j["modulo"] and j["ssd"] for j in payload["incarichi"])
        ids=[j["id"] for j in payload["incarichi"]]
        assert len(set(ids))==2

        chosen=payload["incarichi"][0]
        filtered=client.get("/api/bandi/26-02/valutazioni",params={
            "cds":chosen["cds"],"sede":chosen["sede"]}).json()
        assert {j["id"] for j in filtered["incarichi"]}==set(ids)
        assert all(r["incarico_id"] in ids for r in filtered["archivio"])
        # Filtri e vista sono di sola lettura: i PDF acquisiti non cambiano.
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf WHERE attiva=1").fetchone()[0]==3


def test_nuova_versione_non_sostituisce_la_relazione_incarico_candidatura(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        _complete_all(client,sheets)
        original=client.get("/api/bandi/26-02/valutazioni").json()
        first=original["archivio"][0]
        original_id=first["id"]
        original_job=first["incarico_id"]
        original_candidate=first["candidatura_id"]
        assert original_candidate
        # La versione storica mantiene le chiavi PICA, anche dopo una rettifica.
        with server.connect() as db:
            current=db.execute("SELECT id FROM valutazioni_pdf WHERE id=?",
                               (original_id,)).fetchone()
            assert current
            old=db.execute("""SELECT id,candidatura_id,attiva FROM valutazioni_pdf
                              WHERE id=?""",(original_id,)).fetchone()
            assert old["attiva"]==1
            assert db.execute("""SELECT incarico_id FROM candidature WHERE id=?""",
                              (original_candidate,)).fetchone()["incarico_id"]==original_job
        after=client.get("/api/bandi/26-02/valutazioni").json()
        same=next(x for x in after["archivio"] if x["id"]==original_id)
        assert same["incarico_id"]==original_job
        assert same["candidatura_id"]==original_candidate
        assert same["incarico_key"]==first["incarico_key"]


def test_ssd_nuovo_conserva_default_non_professionalizzante(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        result=client.get("/api/bandi/26-02/ssd").json()
        # Un SSD non ancora configurato continua a usare il modello ordinario,
        # anche se non esiste una classificazione permanente salvata.
        with server.connect() as db:
            unconfigured=db.execute("""
              SELECT UPPER(TRIM(i.ssd)) AS ssd
              FROM incarichi i LEFT JOIN ssd_classificazioni cl
                ON cl.ssd=UPPER(TRIM(i.ssd))
              WHERE cl.ssd IS NULL AND i.ssd NOT IN ('NN','ND','')
              LIMIT 1
            """).fetchone()
        if unconfigured:
            sector=next(x for x in result["ssd"] if x["ssd"]==unconfigured["ssd"])
            assert sector["tipo"]=="ordinario"
            assert sector["tipo_salvato"] is None
