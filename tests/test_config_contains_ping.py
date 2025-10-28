from pathlib import Path


def test_config_contains_ping_response() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config_path = repo_root / "config.yaml"
    assert config_path.exists(), "config.yaml must exist in repository root for this test"

    text = config_path.read_text(encoding="utf-8")
    # Check that the static mapping for PING -> PONG is present
    assert "^PING$" in text or "PING" in text
    assert "PONG" in text
