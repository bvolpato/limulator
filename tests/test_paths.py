from limulator.lifecycle import Paths


def test_xdg_data_home_and_private_prefix_match_the_bootstrap_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("LIMULATOR_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIMULATOR_PREFIX", str(tmp_path / "native-tools"))
    paths = Paths()
    assert paths.data == tmp_path / "data/limulator"
    assert paths.prefix == tmp_path / "native-tools"


def test_explicit_home_wins_over_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LIMULATOR_HOME", str(tmp_path / "chosen"))
    assert Paths().data == tmp_path / "chosen"
