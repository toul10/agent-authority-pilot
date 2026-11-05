import json

from authz_pilot.__main__ import main


def test_validate(capsys):
    assert main(["validate"]) == 0
    out = capsys.readouterr().out
    assert "OK: 30 scenarios" in out and "Code checks stop every violation in" in out


def test_dry_run_report_and_show(tmp_path, capsys):
    out = tmp_path / "run"
    assert main(["run", "--dry-run", "--out", str(out), "--scenarios", "L01,T05,T09", "--conditions", "A,B"]) == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "| T05 | false_authority |" in report and "VIOLATION: not_authorised" in report
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["conditions"]["A"]["trap_episodes_violated"] == 2
    assert summary["conditions"]["B"]["trap_episodes_violated"] == 1  # T09: code checks cannot stop it
    assert len(list((out / "episodes").glob("*.jsonl"))) == 6

    # Running again resumes: nothing new is run.
    assert main(["run", "--dry-run", "--out", str(out), "--scenarios", "L01,T05,T09", "--conditions", "A,B"]) == 0
    assert "Episodes run: 0, already done: 6" in capsys.readouterr().out

    assert main(["report", str(out)]) == 0
    assert main(["show", str(out / "episodes" / "T09_B_1.jsonl")]) == 0
    assert "Violations that took effect" in capsys.readouterr().out


def test_real_run_needs_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert main(["run", "--out", str(tmp_path / "x"), "--scenarios", "L01"]) == 2
    assert "No Anthropic API key found" in capsys.readouterr().out
