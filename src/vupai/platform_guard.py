"""Fail-fast platform guard.

vupai's speech-to-text core runs on parakeet-mlx (Apple's MLX), which only
exists on macOS / Apple Silicon. parakeet-mlx is imported at module load in
asr.py, so on any other platform `import vupai.cli` would die with a raw
ImportError before main() ran. This guard runs from the package __init__ and
turns that into a clear, fail-fast message instead.
"""
from __future__ import annotations

import platform as _platform
import sys
from typing import TextIO

SUPPORTED: tuple[str, str] = ("darwin", "arm64")

_MESSAGE = (
    "vupai requires macOS on Apple Silicon (arm64).\n"
    "Its speech-to-text core runs on parakeet-mlx, which is MLX-only and has no\n"
    "cross-platform fallback. Detected platform: {platform}/{machine}."
)


def supported(platform_name: str, machine: str) -> bool:
    """True only on the one platform where the ASR core can run."""
    return (platform_name, machine) == SUPPORTED


def require_supported_platform(
    platform_name: str | None = None,
    machine: str | None = None,
    *,
    out: TextIO | None = None,
) -> None:
    """Exit with a clear message when not on macOS / Apple Silicon.

    Args default to the live interpreter values; they are injectable for tests.
    """
    platform_name = sys.platform if platform_name is None else platform_name
    machine = _platform.machine() if machine is None else machine
    if supported(platform_name, machine):
        return
    print(
        _MESSAGE.format(platform=platform_name, machine=machine),
        file=sys.stderr if out is None else out,
    )
    raise SystemExit(1)
