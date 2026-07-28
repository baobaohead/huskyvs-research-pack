"""Unambiguous D1 bridge version dispatch."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
try:
    from src.d1_signal_bridge_v1 import BridgeError
    from src import d1_signal_bridge_v1 as v1
    from src import d1_signal_bridge_v2 as v2
except ModuleNotFoundError:  # pragma: no cover
    from d1_signal_bridge_v1 import BridgeError
    import d1_signal_bridge_v1 as v1
    import d1_signal_bridge_v2 as v2

def _version_from_output(output_dir: Path) -> str:
    try: return str(json.loads((Path(output_dir)/"bridge_manifest.json").read_text(encoding="utf-8")).get("bridge_version", ""))
    except Exception as exc: raise BridgeError("VALUE_BUNDLE_VERSION_UNKNOWN", "bridge manifest version unavailable") from exc

def verify_bridge_output_dispatch(output_dir: Path) -> dict[str, Any]:
    version=_version_from_output(output_dir)
    if version == v1.BRIDGE_VERSION: return v1.verify_bridge_output(output_dir)
    if version == v2.BRIDGE_VERSION: return v2.verify_bridge_output(output_dir)
    raise BridgeError("VALUE_BUNDLE_VERSION_UNKNOWN", "unknown bridge version", {"bridge_version":version})

def convert_bundle_dispatch(weather: dict[str, Any], value: dict[str, Any], output_root: Path, **kwargs: Any) -> dict[str, Any]:
    version=value.get("schema_version")
    if version == "2.0": return v2.convert_bundles(weather, value, output_root, **kwargs)
    if version in (None, "1.0"): return v1.convert_bundles(weather, value, output_root, **kwargs)
    raise BridgeError("VALUE_BUNDLE_VERSION_UNKNOWN", "unknown value bundle version", {"schema_version":version})
