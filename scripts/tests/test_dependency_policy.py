from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_cryptography_policy_uses_patched_linux_series_and_macos_wheel_marker():
    requirements = (ROOT / "api" / "requirements.txt").read_text(encoding="utf-8")

    assert 'cryptography>=50,<51; sys_platform != "darwin" or platform_machine == "arm64"' in requirements
    assert 'cryptography>=48.0.1,<49; sys_platform == "darwin" and platform_machine != "arm64"' in requirements
