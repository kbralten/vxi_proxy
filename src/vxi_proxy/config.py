"""Configuration loading utilities for the VXI proxy."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping

import yaml


@dataclass(slots=True)
class ServerSettings:
    """Configuration for the VXI-11 façade listener."""

    host: str = "0.0.0.0"
    port: int = 0
    portmapper_enabled: bool = False


@dataclass(slots=True)
class DeviceDefinition:
    """Definition for a logical instrument mapped to a backend adapter."""

    name: str
    type: str
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MappingRule:
    """Mapping rule translating SCPI-like commands into backend operations."""

    pattern: str
    action: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Config:
    """Top-level configuration container."""

    server: ServerSettings
    devices: Dict[str, DeviceDefinition] = field(default_factory=dict)
    mappings: Dict[str, List[MappingRule]] = field(default_factory=dict)


class ConfigurationError(RuntimeError):
    """Raised when configuration parsing fails."""


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in configuration file: {path}") from exc

    if not isinstance(data, dict):
        raise ConfigurationError("Configuration root must be a mapping")

    return data


def load_config(path: Path) -> Config:
    """Load and validate configuration from a YAML file."""

    raw = _load_yaml(path)

    server_raw = raw.get("server", {})
    if not isinstance(server_raw, dict):
        raise ConfigurationError("server section must be a mapping")

    server = ServerSettings(
        host=str(server_raw.get("host", "0.0.0.0")),
        port=int(server_raw.get("port", 0)),
        portmapper_enabled=bool(server_raw.get("portmapper_enabled", False)),
    )

    devices_raw = raw.get("devices", {})
    if not isinstance(devices_raw, dict):
        raise ConfigurationError("devices section must be a mapping")

    devices: Dict[str, DeviceDefinition] = {}
    for name, body in devices_raw.items():
        if not isinstance(body, dict):
            raise ConfigurationError(f"Device definition for {name!r} must be a mapping")
        device_type = body.get("type")
        if not isinstance(device_type, str):
            raise ConfigurationError(f"Device {name!r} must define a string 'type'")
        settings = {k: v for k, v in body.items() if k != "type"}
        devices[name] = DeviceDefinition(name=name, type=device_type, settings=settings)

    mappings_raw = raw.get("mappings", {})
    if not isinstance(mappings_raw, dict):
        raise ConfigurationError("mappings section must be a mapping")

    mappings: Dict[str, List[MappingRule]] = {}
    for device_name, rules in mappings_raw.items():
        if not isinstance(rules, list):
            raise ConfigurationError(
                f"Mappings for device {device_name!r} must be provided as a list"
            )
        mapping_rules: List[MappingRule] = []
        for idx, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise ConfigurationError(
                    f"Mapping rule #{idx} for {device_name!r} must be a mapping"
                )
            pattern = rule.get("pattern")
            action = rule.get("action")
            params = rule.get("params", {})
            if not isinstance(pattern, str) or not pattern:
                raise ConfigurationError(
                    f"Mapping rule #{idx} for {device_name!r} must include a non-empty 'pattern'"
                )
            if not isinstance(action, str) or not action:
                raise ConfigurationError(
                    f"Mapping rule #{idx} for {device_name!r} must include a non-empty 'action'"
                )
            if not isinstance(params, dict):
                raise ConfigurationError(
                    f"Mapping rule #{idx} for {device_name!r} must supply params as a mapping"
                )
            mapping_rules.append(
                MappingRule(pattern=pattern, action=action, params=params)
            )
        mappings[device_name] = mapping_rules

    return Config(server=server, devices=devices, mappings=mappings)
