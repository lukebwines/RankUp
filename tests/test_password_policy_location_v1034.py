"""RankUP 1.0.34: policy password in Gestione utenti, registro sola consultazione."""
from pathlib import Path

INDEX=(Path(__file__).resolve().parents[1]/"rankup/app/static/index.html")


def test_password_policy_is_editable_only_in_admin_users_page():
    source=INDEX.read_text(encoding="utf-8")
    registry=source.split("async function renderAuditAdmin(){",1)[1].split(
        "async function renderDiagnostica(){",1)[0]
    users=source.split("async function renderUtenti(){",1)[1].split(
        "async function ",1)[0]
    policy_helpers=source.split("function operatorPasswordPolicyMarkup(){",1)[1].split(
        "async function renderUtenti(){",1)[0]

    assert 'id="operator-password-policy"' in policy_helpers
    assert 'id="users-password-policy"' in policy_helpers
    assert 'id="users-password-policy-save"' in policy_helpers
    assert "admin/sicurezza/politica" in policy_helpers
    assert 'state.role!=="admin"' in policy_helpers
    assert "operatorPasswordPolicyMarkup()" in users
    assert "bindOperatorPasswordPolicy()" in users
    assert "scrollIntoView" in users
    assert 'id="audit-policy"' not in registry
    assert 'id="audit-policy-save"' not in registry
    assert 'id="audit-policy-link"' in registry
    assert 'go("utenti")' in registry
    assert "scrollToPasswordPolicy=true" in registry
    assert "admin/sicurezza/politica" not in registry


def test_password_policy_has_safe_default_and_explicit_confirmation():
    source=INDEX.read_text(encoding="utf-8")
    policy=source.split("function operatorPasswordPolicyMarkup(){",1)[1].split(
        "async function renderUtenti(){",1)[0]
    assert '<option value="0">Nessuna scadenza automatica</option>' in policy
    assert '<option value="180">Ogni 180 giorni' in policy
    assert 'days===180&&!confirm(' in policy
    assert '"PUT"' in policy


def test_server_keeps_admin_guard_for_password_policy():
    source=(INDEX.parents[1]/"server.py").read_text(encoding="utf-8")
    for name in ("def leggi_politica_password(","def modifica_politica_password("):
        block=source.split(name,1)[1].split("\n\n",1)[0]
        assert "require_admin(request)" in block
