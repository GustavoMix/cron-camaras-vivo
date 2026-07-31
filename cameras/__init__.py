"""Weekly index of publicly published live cameras.

Only feeds that agencies and platforms publish deliberately for public
consumption are collected. This project does not scan for, probe, or index
privately owned cameras that happen to be reachable.
"""

from .models import SCHEMA_VERSION, Camera, make_camera

__all__ = ["Camera", "make_camera", "SCHEMA_VERSION"]
__version__ = "1.0.0"
