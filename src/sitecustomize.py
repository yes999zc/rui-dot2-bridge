"""Register the dot2 adapter inside rui's processes, without editing rui.

rui starts its audio plane as ``python -m house_ear.audio.process``, and Python
imports ``sitecustomize`` automatically when it is importable. Mounting this
directory on ``PYTHONPATH`` therefore registers the adapter in the parent and in
the plane child with no change to the rui repository.

Failures are swallowed on purpose: a broken adapter must degrade to "unknown
adapter: dot2" in rui's console, not take the assistant down at import time.
"""

try:
    from house_ear.device import registry

    registry.register("dot2", "rui_dot2_bridge.adapter:build")
except Exception:  # noqa: BLE001 - a plugin must never break its host
    pass
