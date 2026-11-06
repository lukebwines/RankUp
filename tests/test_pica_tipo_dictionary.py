"""Regole della colonna L PICA: solo corrispondenze esplicitamente approvate."""
from io import BytesIO
from openpyxl import load_workbook
from test_import import pica_fixture
from test_pdf_import import _post
from test_users import _empty_setup


def _pica_with_type(label):
    workbook = load_workbook(BytesIO(pica_fixture()))
    sheet = workbook.active
    headers = [cell.value for cell in sheet[2]]
    column = headers.index("Tipo CDS") + 1
    for row in range(3, sheet.max_row + 1):
        sheet.cell(row, column).value = label
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_initial_mapping_and_provenance(tmp_path, monkeypatch):
    server, admin = _empty_setup(tmp_path, monkeypatch)
    with admin:
        rows = admin.get("/api/pica/tipologie").json()["tipologie"]
        mapping = {r["dicitura"]: r["requisito"] for r in rows}
        assert mapping["contratto esterno oneroso CSA"] == "NESSUNA RISERVA"
        assert mapping["contratto esterno oneroso UGOV C"] == "NESSUNA RISERVA"
        assert mapping["contratto esterno oneroso a carico APSS TN CSA"] == "SSN"
        assert mapping["contratto esterno oneroso dip. SSN CSA"] == "SSN"
        assert mapping["supplenza esterna retribuita"] == "ALTRO ATENEO"
        preview = _post(admin, "/api/pica/preview",
                        files={"file": ("sintetico.xlsx", pica_fixture(), "application/octet-stream")})
        assert preview.status_code == 200, preview.text
        kind = preview.json()["tipologie"]
        assert len(kind) == 1
        assert kind[0]["nuova"] is False
        assert kind[0]["incarichi"] == 2
        assert kind[0]["requisito"] == "NESSUNA RISERVA"
        commit = _post(admin, "/api/pica/commit", json={"token": preview.json()["token"]})
        assert commit.status_code == 200, commit.text
        assignments = admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        assert all(x["ente_richiesto"] == "NESSUNA RISERVA" for x in assignments)
        assert all(x["stato_requisito"] == "DIZIONARIO PICA" for x in assignments)
        assert all(x["tipo_cds"] == "contratto esterno oneroso CSA" for x in assignments)


def test_new_type_requires_exact_classification(tmp_path, monkeypatch):
    server, admin = _empty_setup(tmp_path, monkeypatch)
    with admin:
        file = _pica_with_type("NUOVA TIPOLOGIA DI COLLAUDO")
        preview = _post(admin, "/api/pica/preview",
                        files={"file": ("nuovo.xlsx", file, "application/octet-stream")})
        assert preview.status_code == 200, preview.text
        info = preview.json()["tipologie"]
        assert len(info) == 1 and info[0]["nuova"]
        token = preview.json()["token"]
        missing = _post(admin, "/api/pica/commit", json={"token": token})
        assert missing.status_code == 400, missing.text
        wrong = _post(admin, "/api/pica/commit", json={"token": token,
                      "tipologie": [{"chiave": info[0]["chiave"], "requisito": "INVENTATO"}]})
        assert wrong.status_code == 400, wrong.text
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM bandi").fetchone()[0] == 0
        approved = _post(admin, "/api/pica/commit", json={
            "token": token,
            "tipologie": [{"chiave": info[0]["chiave"], "requisito": "SSN"}]})
        assert approved.status_code == 200, approved.text
        current = admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        assert all(x["ente_richiesto"] == "SSN" for x in current)
        with server.connect() as db:
            events = db.execute("SELECT COUNT(*) FROM pica_tipo_cds_eventi").fetchone()[0]
            assert events == 1


def test_manual_override_survives_dictionary_revision(tmp_path, monkeypatch):
    server, admin = _empty_setup(tmp_path, monkeypatch)
    with admin:
        preview = _post(admin, "/api/pica/preview",
                        files={"file": ("sintetico.xlsx", pica_fixture(), "application/octet-stream")})
        assert preview.status_code == 200, preview.text
        assert _post(admin, "/api/pica/commit",
                     json={"token": preview.json()["token"]}).status_code == 200
        rows = admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        first = rows[0]
        manual = admin.put("/api/bandi/26-02/incarichi/" + first["key"] + "/ente",
                           headers={"X-RankUp-Request": "1"},
                           json={"ente_richiesto": "AOUI",
                                 "fonte": "Conferma amministrativa di collaudo"})
        assert manual.status_code == 200, manual.text
        no_reason = admin.put("/api/pica/tipologie",
                              headers={"X-RankUp-Request": "1"},
                              json={"chiave": "contratto esterno oneroso CSA",
                                    "requisito": "SSN"})
        assert no_reason.status_code == 400
        changed = admin.put("/api/pica/tipologie",
                            headers={"X-RankUp-Request": "1"},
                            json={"chiave": "contratto esterno oneroso CSA",
                                  "requisito": "SSN",
                                  "motivo": "Rettifica sintetica di collaudo"})
        assert changed.status_code == 200, changed.text
        updated = admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        by_key = {x["key"]: x for x in updated}
        assert by_key[first["key"]]["ente_richiesto"] == "AOUI"
        assert by_key[first["key"]]["stato_requisito"] == "CONFERMATO MANUALMENTE"
        assert any(x["ente_richiesto"] == "SSN" and x["stato_requisito"] == "DIZIONARIO PICA"
                   for x in updated if x["key"] != first["key"])
        with server.connect() as db:
            event = db.execute("SELECT precedente_json,successivo_json FROM pica_tipo_cds_eventi").fetchone()
            assert event and "SSN" in event["successivo_json"]
