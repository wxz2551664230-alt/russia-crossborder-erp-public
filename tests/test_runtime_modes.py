from erp.core.config import Settings


def test_local_mode_preserves_current_all_in_one_behavior() -> None:
    settings = Settings(runtime_mode="local")

    assert settings.runs_api is True
    assert settings.runs_local_workers is True


def test_server_mode_only_runs_control_plane() -> None:
    settings = Settings(runtime_mode="server")

    assert settings.runs_api is True
    assert settings.runs_local_workers is False


def test_worker_mode_only_runs_local_execution() -> None:
    settings = Settings(runtime_mode="worker")

    assert settings.runs_api is False
    assert settings.runs_local_workers is True
