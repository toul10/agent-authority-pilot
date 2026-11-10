import json
import os
import stat

import anthropic
import pytest
from mock_api import CAPABLE, MOCK_KEY, MockAPI, model_info, sent_key

from authz_pilot.__main__ import main
from authz_pilot.models import check_models
from authz_pilot.settings import (
    Settings,
    home,
    key_path,
    key_problem,
    load_settings,
    mask,
    parse_settings,
    remove_key,
    resolve_key,
    save_key,
    save_settings,
)

FAKE = "sk-ant-fake-key-for-tests-0123456789abcd"


def test_tests_never_see_a_real_key(tmp_path):
    assert resolve_key() == (None, "none")
    assert str(home()).startswith(str(tmp_path))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_saved_key_is_private_from_the_start():
    path = save_key(f"  {FAKE}\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.read_text(encoding="utf-8") == FAKE + "\n"
    path.chmod(0o644)  # an older file with wider permissions is tightened on the next save
    save_key(FAKE)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_key_lookup_order(monkeypatch):
    save_key(FAKE)
    assert resolve_key() == (FAKE, "saved")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-the-environment-000000")
    assert resolve_key() == ("sk-ant-from-the-environment-000000", "environment")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert remove_key() and not remove_key()
    assert resolve_key() == (None, "none")


def test_keychain_is_off_in_tests_and_skipped_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: calls.append(a))
    from authz_pilot.settings import keychain_key

    assert keychain_key() is None and calls == []


def test_key_problems_and_mask():
    assert key_problem("") and key_problem("short") and key_problem("sk-ant-has space-0000000000000")
    assert key_problem("sk-ant-ünïcode-00000000000000000")
    assert key_problem(FAKE) is None
    with pytest.raises(ValueError):
        save_key("not a key")
    assert mask(FAKE) == "sk-ant-…abcd" and FAKE not in mask(FAKE)
    assert mask(None) == ""


def test_settings_validation_and_round_trip():
    s, problems = parse_settings(
        {"model": "gpt-4", "effort": "huge", "max_cost": "-1", "max_steps": 2.5, "workers": True, "unknown": 1}
    )
    assert set(problems) == {"model", "effort", "max_cost", "max_steps", "workers"}
    assert s == Settings()  # nothing invalid was applied
    s, problems = parse_settings(
        {
            "model": "claude-haiku-5-5",
            "effort": "low",
            "monitor_model": "claude-fable-5-1",
            "max_cost": "2.5",
            "max_steps": "6",
            "workers": 2,
        }
    )
    assert problems == {}
    save_settings(s)
    assert load_settings() == s and load_settings().max_cost == 2.5 and load_settings().max_steps == 6


def test_broken_settings_file_falls_back_to_defaults():
    save_settings(Settings())
    key_path().parent.joinpath("settings.json").write_text("{not json", encoding="utf-8")
    assert load_settings() == Settings()


def test_check_models_against_the_mock():
    with MockAPI() as api:
        client = anthropic.Anthropic(api_key=MOCK_KEY, base_url=api.url, max_retries=0)
        result = check_models(
            {"assistant": ("claude-opus-5-5", "medium"), "monitor": ("claude-sonnet-5-5", "medium")}, client=client
        )
        assert result["ok"] and [r["name"] for r in result["models"]] == ["Claude Opus 5.5", "Claude Sonnet 5.5"]
        assert all(r["method"] == "GET" for r in api.requests)  # free calls only, no messages

        wrong = anthropic.Anthropic(api_key="sk-ant-wrong-key-000000000000", base_url=api.url, max_retries=0)
        result = check_models({"assistant": ("claude-opus-5-5", "medium")}, client=wrong)
        assert not result["ok"] and "401" in result["error"]

    no_structured = {**CAPABLE, "structured_outputs": {"supported": False}}
    with MockAPI(
        models={
            "claude-opus-5-5": model_info("claude-opus-5-5"),
            "claude-haiku-5-5": model_info("claude-haiku-5-5", no_structured),
        }
    ) as api:
        client = anthropic.Anthropic(api_key=MOCK_KEY, base_url=api.url, max_retries=0)
        result = check_models(
            {"assistant": ("claude-haiku-5-5", "medium"), "monitor": ("claude-haiku-5-5", "medium")}, client=client
        )
        assistant, monitor = result["models"]
        assert assistant["ok"] and not monitor["ok"] and "structured outputs" in monitor["problems"][0]
        result = check_models({"assistant": ("claude-fable-5-1", "medium")}, client=client)
        assert not result["ok"] and "404" in result["models"][0]["problems"][0]


def test_key_commands(monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": FAKE)
    assert main(["key", "set"]) == 0
    assert main(["key", "status"]) == 0
    out = capsys.readouterr().out
    assert "sk-ant-…abcd" in out and FAKE not in out

    monkeypatch.setattr("getpass.getpass", lambda prompt="": "oops")
    assert main(["key", "set"]) == 1
    assert resolve_key() == (FAKE, "saved")  # a bad paste does not overwrite the saved key

    assert main(["key", "remove"]) == 0
    assert main(["key", "status"]) == 1
    assert main(["key", "test"]) == 2


def test_key_test_and_a_real_run_use_the_saved_key(tmp_path, monkeypatch, capsys):
    with MockAPI() as api:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", api.url)
        save_key(MOCK_KEY)
        save_settings(Settings(model="claude-sonnet-5-5", max_cost=1.0))
        assert main(["key", "test"]) == 0
        assert "The key works" in capsys.readouterr().out

        out = tmp_path / "run"
        assert main(["run", "--scenarios", "L03", "--conditions", "A,C", "--yes", "--out", str(out)]) == 0
        posts = [r for r in api.requests if r["method"] == "POST"]
        assert posts and all(sent_key(r["headers"]) == MOCK_KEY for r in posts)
        # The assistant from the settings; the monitor's default.
        assert {r["body"]["model"] for r in posts} == {"claude-sonnet-5-5"}
        config = json.loads((out / "config.json").read_text(encoding="utf-8"))
        assert config["agent"] == "claude-sonnet-5-5" and config["monitor"] == "claude-sonnet-5-5"
        printed = capsys.readouterr().out
        assert MOCK_KEY not in printed and "sk-ant-…0000" in printed


def test_run_flags_are_validated(tmp_path):
    with pytest.raises(SystemExit) as info:
        main(["run", "--dry-run", "--max-steps", "0", "--out", str(tmp_path / "x"), "--scenarios", "L01"])
    assert "--max-steps" in str(info.value)
