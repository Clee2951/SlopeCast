"""Reads .env and config/*.json once at startup and hands one Settings object to everything.

Environment variables (see .env.example) override values in .env. Missing or broken
configuration stops startup with a clear message instead of failing later.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.schemas import NodeConfig

ROOT = Path(__file__).resolve().parents[1]


class SettingsError(RuntimeError):
    pass


@dataclass
class Settings:
    root: Path
    site: dict
    site_id: str
    nodes: dict                       # node_id -> NodeConfig
    rainfall: dict
    hazard: dict
    evacuation: dict
    messages: dict
    calibration_path: Path
    db_path: Path
    data_paths: dict                  # name -> Path
    serial_ports: list = field(default_factory=list)   # explicit ports, or ["auto"]
    anthropic_api_key: Optional[str] = None
    anthropic_workspace_id: Optional[str] = None
    apify_token: Optional[str] = None
    mode: str = "live"                # live | replay
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    extra_nodes: str = "calm"         # calm | off : fill absent stakes with simulated readings

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def _read_env_file(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _load_json(path: Path, what: str) -> dict:
    if not path.exists():
        raise SettingsError(f"{what} not found: {path}")
    try:
        return json.loads(path.read_text())
    except ValueError as e:
        raise SettingsError(f"{what} is not valid JSON ({path}): {e}") from None


def load_settings(root: Optional[Path] = None, env: Optional[dict] = None) -> Settings:
    root = Path(root or ROOT)
    values = _read_env_file(root / ".env")
    values.update({k: v for k, v in (env if env is not None else os.environ).items() if k.startswith(("SLOPECAST_", "ANTHROPIC_", "APIFY_"))})

    cfg = root / "config"
    site = _load_json(cfg / "site.json", "Site config")
    for key in ("site_id", "center", "nodes", "data"):
        if key not in site:
            raise SettingsError(f"config/site.json is missing '{key}'")
    nodes = {}
    for nid, n in site["nodes"].items():
        try:
            nodes[nid] = NodeConfig(nid, float(n["lat"]), float(n["lon"]), n.get("sector", ""),
                                    n.get("kind", "live"), n.get("serial_port", "auto"))
        except (KeyError, TypeError, ValueError):
            raise SettingsError(f"config/site.json: node {nid} needs numeric lat and lon") from None

    ports_raw = values.get("SLOPECAST_SERIAL_PORTS", "auto").strip()
    ports = [p.strip() for p in ports_raw.split(",") if p.strip()] or ["auto"]

    s = Settings(
        root=root,
        site=site,
        site_id=site["site_id"],
        nodes=nodes,
        rainfall=_load_json(cfg / "rainfall-policy.json", "Rainfall policy"),
        hazard=_load_json(cfg / "hazard-policy.json", "Hazard policy"),
        evacuation=_load_json(cfg / "evacuation-policy.json", "Evacuation policy"),
        messages=_load_json(cfg / "messages.json", "Message templates"),
        calibration_path=cfg / "calibration.json",
        db_path=root / values.get("SLOPECAST_DB", site.get("database", "var/slopecast.db")),
        data_paths={k: root / v for k, v in site["data"].items()},
        serial_ports=ports,
        anthropic_api_key=values.get("ANTHROPIC_API_KEY") or None,
        anthropic_workspace_id=values.get("ANTHROPIC_WORKSPACE_ID") or None,
        apify_token=values.get("APIFY_TOKEN") or None,
        mode=values.get("SLOPECAST_MODE", "live"),
        http_host=values.get("SLOPECAST_HOST", "127.0.0.1"),
        http_port=int(values.get("SLOPECAST_HTTP_PORT", "8000")),
        extra_nodes=values.get("SLOPECAST_EXTRA_NODES", "calm"),
    )
    for name, p in s.data_paths.items():
        if not p.exists():
            raise SettingsError(f"Data file for '{name}' not found: {p}")
    if s.mode not in ("live", "replay"):
        raise SettingsError("SLOPECAST_MODE must be 'live' or 'replay'")
    return s
