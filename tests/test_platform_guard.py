import io

import pytest

from vupai.platform_guard import require_supported_platform, supported


def test_apple_silicon_is_supported():
    assert supported("darwin", "arm64")


@pytest.mark.parametrize(
    "platform_name,machine",
    [
        ("darwin", "x86_64"),  # Intel Mac: no MLX
        ("linux", "x86_64"),
        ("linux", "aarch64"),
        ("win32", "AMD64"),
    ],
)
def test_other_platforms_unsupported(platform_name, machine):
    assert not supported(platform_name, machine)


def test_require_passes_silently_on_apple_silicon():
    out = io.StringIO()
    require_supported_platform("darwin", "arm64", out=out)
    assert out.getvalue() == ""


def test_require_exits_with_message_on_unsupported():
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        require_supported_platform("linux", "x86_64", out=out)
    assert exc.value.code == 1
    msg = out.getvalue()
    assert "Apple Silicon" in msg
    assert "linux/x86_64" in msg
