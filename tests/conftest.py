import pytest

from authz_pilot.world import load_world


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep every test away from the real API key, settings, Keychain and custom scenarios."""
    monkeypatch.setenv("AUTHZ_PILOT_HOME", str(tmp_path / "authz-home"))
    monkeypatch.setenv("AUTHZ_PILOT_KEYCHAIN", "0")
    monkeypatch.setenv("AUTHZ_PILOT_CUSTOM", str(tmp_path / "custom" / "scenarios.toml"))
    monkeypatch.setenv("AUTHZ_PILOT_RESULTS", str(tmp_path / "results"))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def world():
    return load_world()
