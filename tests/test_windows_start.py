import os
import subprocess
import sys

import pytest

from ozon_forecast.windows_start import (
    ChildJob,
    TunnelAccessDenied,
    run_tunnel,
    start_zrok,
    store_ngrok_path,
    supervise_tunnel,
    validate_url,
    zrok_command,
)


def test_startup_url_is_https_origin():
    assert validate_url("https://test.ngrok-free.dev/") == "https://test.ngrok-free.dev"
    for value in ("http://test.test", "https://user:secret@test.test", "https://test.test/path",
                  "https://test.test?token=x", "https://test.test#fragment", "https://test.test:443",
                  'https://test".test', 'https://test name.test'):
        with pytest.raises(ValueError):
            validate_url(value)


def test_store_alias_is_used_without_standalone_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # A legacy file must not be selected when the Store installation is missing.
    (tmp_path / "ngrok.exe").write_bytes(b"legacy fixture, not executable")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Microsoft Store"):
        store_ngrok_path()
    alias = tmp_path / "Microsoft" / "WindowsApps" / "ngrok.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"store alias fixture, not executable")
    assert store_ngrok_path() == alias


def test_store_alias_requires_user_profile(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(ValueError, match="LOCALAPPDATA"):
        store_ngrok_path()


@pytest.mark.skipif(os.name != "nt", reason="Windows process containment")
def test_tunnel_exit_code_and_job_cleanup(tmp_path):
    assert run_tunnel([sys.executable, "-c", "raise SystemExit(7)"], tmp_path) == 7


@pytest.mark.skipif(os.name != "nt", reason="Windows process containment")
def test_tunnel_logs_only_diagnostic_codes(tmp_path, caplog):
    output = "authtoken=private-fixture ERR_NGROK_1234 account=private@example.test"
    assert run_tunnel([sys.executable, "-c", f"print({output!r}); raise SystemExit(1)"],
                      tmp_path) == 1
    assert "ERR_NGROK_1234" in caplog.text
    assert "private-fixture" not in caplog.text
    assert "private@example.test" not in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="Windows process containment")
def test_provider_ip_refusal_is_not_retried(tmp_path, monkeypatch, caplog):
    pauses = []
    command = [sys.executable, "-c", "print('ERR_NGROK_9040'); raise SystemExit(1)"]
    with pytest.raises(TunnelAccessDenied):
        run_tunnel(command, tmp_path)
    monkeypatch.setattr("ozon_forecast.windows_start.time.sleep", pauses.append)
    assert supervise_tunnel(command, tmp_path) == 0
    assert pauses == []
    assert "retries stopped" in caplog.text


def test_supervisor_retries_worker_exit_but_stops_os_denial(tmp_path, monkeypatch, caplog):
    calls = []
    pauses = []

    def worker(command, root):
        calls.append((command, root))
        if len(calls) < 3:
            return 1
        raise PermissionError(13, "private-path-must-not-be-logged")

    monkeypatch.setattr("ozon_forecast.windows_start.run_tunnel", worker)
    monkeypatch.setattr("ozon_forecast.windows_start.time.sleep", pauses.append)
    assert supervise_tunnel(["fixture"], tmp_path) == 0
    assert len(calls) == 3
    assert pauses == [60, 60]
    assert "manual check required" in caplog.text
    assert "private-path" not in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="Windows process containment")
def test_closing_job_terminates_attached_child():
    job = ChildJob()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        job.attach(child.pid)
        assert child.poll() is None
        job.close()
        # Windows job closure may return zero; the 30-second sleeper must stop now.
        assert child.wait(timeout=5) is not None
    finally:
        job.close()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_zrok_uses_pinned_installation_and_headless_reserved_share(tmp_path):
    executable = tmp_path / "tools" / "zrok-1.1.11" / "zrok.exe"
    with pytest.raises(ValueError, match="Missing installed zrok"):
        zrok_command(tmp_path, "testshare")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture, not executable")
    assert zrok_command(tmp_path, "testshare") == [
        str(executable), "share", "reserved", "testshare", "--headless",
    ]
    for share in ("", "abc", "a" * 33, "test;run", "--help", "a b c", "ABCD",
                  "https://test.share.zrok.io"):
        with pytest.raises(ValueError, match="reserved zrok share name"):
            zrok_command(tmp_path, share)


@pytest.mark.parametrize("result", [PermissionError(13, "secret-path"),
                                    TunnelAccessDenied("private-detail")])
def test_zrok_stops_after_launch_denial_without_retries(tmp_path, monkeypatch, caplog, result):
    calls = []

    def worker(command, root):
        calls.append((command, root))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("ozon_forecast.windows_start.run_tunnel", worker)
    assert start_zrok(["fixture"], tmp_path) == 0
    assert calls == [(["fixture"], tmp_path)]
    assert "manual check required" in caplog.text
    assert "secret-path" not in caplog.text
    assert "private-detail" not in caplog.text


def test_zrok_restarts_after_worker_exit(tmp_path, monkeypatch, caplog):
    calls = []
    pauses = []

    def worker(command, root):
        calls.append((command, root))
        if len(calls) < 3:
            return len(calls) - 1
        raise PermissionError(13, "private-path-must-not-be-logged")

    monkeypatch.setattr("ozon_forecast.windows_start.run_tunnel", worker)
    monkeypatch.setattr("ozon_forecast.windows_start.time.sleep", pauses.append)
    assert start_zrok(["fixture"], tmp_path) == 0
    assert calls == [(["fixture"], tmp_path)] * 3
    assert pauses == [60, 60]
    assert "retry in 60 seconds" in caplog.text
    assert "private-path" not in caplog.text


def test_zrok_preflight_does_not_start_tunnel_or_query_store(tmp_path, monkeypatch, capsys):
    from ozon_forecast.windows_start import main

    (tmp_path / ".env.forecast").write_text("", encoding="utf-8")
    executable = tmp_path / "tools" / "zrok-1.1.11" / "zrok.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture, not executable")
    monkeypatch.setattr(sys, "argv", ["windows_start", "zrok-tunnel", "--app-dir", str(tmp_path),
                                    "--zrok-share", "testshare", "--check"])

    def forbidden(*args, **kwargs):
        raise AssertionError("No process should start in check-only mode")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert main() == 0
    assert capsys.readouterr().out == "STARTUP_CHECK_OK\n"
    assert not (tmp_path / "logs").exists()
