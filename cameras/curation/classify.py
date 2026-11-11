"""Map a raw camera to a TV-friendly category, or reject it as off-theme.

Traffic and airport cameras are rejected outright: they are plentiful, but they
are not what someone wants on a living-room screen.
"""

from __future__ import annotations

import re

from ..models import Camera

# Order matters: the first matching rule wins.
_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("marine", frozenset({"aquarium", "reef", "underwater", "coral", "dive", "whale", "penguin"})),
    ("beaches", frozenset({"beach", "surf", "shore", "playa", "sand", "lagoon", "island"})),
    ("mountains", frozenset({"mountain", "ski", "peak", "alpine", "volcano", "glacier", "summit"})),
    (
        "nature",
        frozenset(
            {"waterfall", "falls", "forest", "river", "lake", "park", "wildlife", "canyon",
             "geyser", "nature", "valley", "meadow"}
        ),
    ),
    ("ports", frozenset({"harbor", "harbour", "port", "marina", "pier", "lighthouse"})),
    ("cities", frozenset({"city", "skyline", "downtown", "square", "bridge", "tower", "plaza"})),
)

REJECTED_KINDS = frozenset({"traffic", "airport"})

CATEGORIES = tuple(name for name, _ in _RULES) + ("landscapes", "weather")


def classify(camera: Camera) -> str | None:
    """Return a category id, or ``None`` when the camera is off-theme."""
    if camera.kind in REJECTED_KINDS:
        return None

    words = set(re.findall(r"[a-z]+", f"{camera.name} {' '.join(camera.tags)}".lower()))
    for category, keywords in _RULES:
        if words & keywords:
            return category

    if camera.kind == "weather":
        return "weather"
    if camera.kind == "scenic":
        return "landscapes"
    if camera.kind == "port":
        return "ports"
    return None
