from types import SimpleNamespace

from swing_trader import system_identity


def test_primary_identity_is_monthly_not_the_faster_rejected_experiment(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/default.toml").write_text("test = true\n")
    monkeypatch.setattr(system_identity, "load_config", lambda path: SimpleNamespace(strategy=SimpleNamespace(top_n=3)))
    value = system_identity.identity(tmp_path)
    assert value["primary"] == "monthly_12m_top3"
    assert not value["action_authorized"]
