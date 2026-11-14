"""Dashboard KPI RankUP 1.0.25: coerenza del contatore e dei collegamenti.

Tutti i dati del test sono sintetici e non riferiti a bandi reali.
"""
from pathlib import Path

from test_pdf_import import _setup


ROOT = Path(__file__).resolve().parents[1]


def test_commissioni_in_lavorazione_matches_dashboard_and_archived_exclusion(tmp_path, monkeypatch):
    server, client, _ = _setup(tmp_path, monkeypatch)
    with client:
        endpoint = "/api/bandi/26-02/commissioni/in-lavorazione"
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        rows = response.json()["commissioni"]
        initial = response.json()["totale"]
        assert initial == len(rows) > 0
        current = next(b for b in client.get("/api/bandi").json()["bandi"]
                       if b["codice"] == "26-02")
        assert current["cds_in_lavorazione"] == initial

        first = rows[0]
        with server.connect() as db:
            bid = db.execute("SELECT id FROM bandi WHERE codice='26-02'").fetchone()[0]
            db.execute("""
                INSERT INTO graduatorie_cds
                (bando_id,cds,sede,stato,dati_json,hash_dati,confermata_da,confermata_il)
                VALUES (?,?,?,'definitiva','{}','hash-di-test','test-admin','data-test')
            """, (bid, first["cds"], first["sede"]))
        changed = client.get(endpoint).json()
        assert changed["totale"] == initial - 1
        assert not any(r["cds"] == first["cds"] and r["sede"] == first["sede"]
                       for r in changed["commissioni"])
        current = next(b for b in client.get("/api/bandi").json()["bandi"]
                       if b["codice"] == "26-02")
        assert current["cds_in_lavorazione"] == initial - 1

        with server.connect() as db:
            db.execute("UPDATE bandi SET stato='Archiviato' WHERE codice='26-02'")
        assert client.get(endpoint).json() == {"commissioni": [], "totale": 0}
        current = next(b for b in client.get("/api/bandi").json()["bandi"]
                       if b["codice"] == "26-02")
        assert current["cds_in_lavorazione"] == 0
        assert client.get("/api/bandi/26-99/commissioni/in-lavorazione").status_code == 404


def test_dashboard_card_links_and_version_are_in_ui():
    html = (ROOT / "rankup/app/static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "rankup/app/static/workspace.css").read_text(encoding="utf-8")
    config = (ROOT / "rankup/config.yaml").read_text(encoding="utf-8")
    server = (ROOT / "rankup/app/server.py").read_text(encoding="utf-8")
    assert 'version: "1.0.25"' in config
    assert 'version="1.0.25"' in server
    assert 'workspace.css?v=1.0.25' in html
    assert 'RANKUP · v1.0.25' in html
    assert 'var metrics=["bandi","candidature","incarichi","valutazioni","commissioni"]' in html
    for function in ("dashboardTotal", "dashboardMetricPanel", "openDashboardMetric",
                     "bindDashboardMetrics"):
        assert "function " + function + "(" in html
    assert "data-dashboard-metric" in html
    assert "data-dashboard-open" in html
    assert "state.commissioniInLavorazioneOnly=metric===\"commissioni\"" in html
    assert 'data-page="commissioni"' in html
    assert '.ru-workspace .grid.ru-dashboard-metrics{' in css
    assert '.ru-workspace .card.ru-kpi{' in css
