"""
Offline calibration and tuning tools.
"""

from typing import Any

__all__ = [
    "compute_brier_score",
    "compute_ece",
    "CalibrationReport",
    "OfflineCalibrator",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        import decidex.tools.calibrate as cal
        return getattr(cal, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
