"""Configuration loading utilities for the VXI proxy."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml


@dataclass(slots=True)
class GuiSettings:
    """Configuration for the embedded configuration GUI."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 0


@dataclass(slots=True)
class ServerSettings:
    """Configuration for the VXI-11 façade listener."""

    host: str = "0.0.0.0"
    port: int = 0
    portmapper_enabled: bool = False
    gui: GuiSettings = field(default_factory=GuiSettings)


@dataclass(slots=True)
class DeviceDefinition:
    """Definition for a logical instrument mapped to a backend adapter."""

    name: str
    type: str
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MappingRule:
    """Mapping rule translating SCPI-like commands into backend operations.

    For MODBUS rules, ``action`` is required and ``params`` contains action parameters.
    For regex-based rules, ``action`` is optional and additional, rule-specific keys are
    preserved in ``extras`` to round-trip YAML faithfully.
    """

    pattern: str
    action: Optional[str] = None
    params: Mapping[str, Any] = field(default_factory=dict)
    extras: Mapping[str, Any] = field(default_factory=dict)


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


def parse_config_dict(raw: Mapping[str, Any]) -> Config:
    """Parse configuration from an in-memory mapping."""

    if not isinstance(raw, Mapping):
        raise ConfigurationError("Configuration root must be a mapping")

    server_raw = raw.get("server", {})
    if not isinstance(server_raw, dict):
        raise ConfigurationError("server section must be a mapping")

    gui_raw = server_raw.get("gui", {})
    if gui_raw is None:
        gui_raw = {}
    if not isinstance(gui_raw, dict):
        raise ConfigurationError("server.gui section must be a mapping")

    gui = GuiSettings(
        enabled=bool(gui_raw.get("enabled", True)),
        host=str(gui_raw.get("host", "127.0.0.1")),
        port=int(gui_raw.get("port", 0)),
    )

    server = ServerSettings(
        host=str(server_raw.get("host", "0.0.0.0")),
        port=int(server_raw.get("port", 0)),
        portmapper_enabled=bool(server_raw.get("portmapper_enabled", False)),
        gui=gui,
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
            # Determine device type to validate requirements
            device_def = devices.get(device_name)
            device_type = device_def.type if device_def else ""
            if isinstance(device_type, str) and device_type.lower().startswith("modbus"):
                # Allow either an action (normal MODBUS mapping) or a static response
                has_action = isinstance(action, str) and bool(action)
                # Support 'response' either as a top-level key or inside params
                response_top = rule.get("response") if isinstance(rule, dict) else None
                response_param = params.get("response") if isinstance(params, dict) else None
                has_static = (
                    (isinstance(response_top, str) and bool(response_top))
                    or (isinstance(response_param, str) and bool(response_param))
                )
                if not (has_action or has_static):
                    raise ConfigurationError(
                        f"Mapping rule #{idx} for {device_name!r} must include an 'action' or a 'response'"
                    )
            if not isinstance(params, dict):
                raise ConfigurationError(
                    f"Mapping rule #{idx} for {device_name!r} must supply params as a mapping"
                )
            extras = {k: v for k, v in rule.items() if k not in {"pattern", "action", "params"}}
            mapping_rules.append(
                MappingRule(pattern=pattern, action=action if isinstance(action, str) else None, params=params, extras=extras)
            )
        mappings[device_name] = mapping_rules

    return Config(server=server, devices=devices, mappings=mappings)


def load_config(path: Path) -> Config:
    """Load and validate configuration from a YAML file."""

    raw = _load_yaml(path)
    return parse_config_dict(raw)


def config_to_dict(config: Config) -> Dict[str, Any]:
    """Convert a Config instance back into a serialisable mapping."""

    server_dict: Dict[str, Any] = {
        "host": config.server.host,
        "port": config.server.port,
        "portmapper_enabled": config.server.portmapper_enabled,
        "gui": {
            "enabled": config.server.gui.enabled,
            "host": config.server.gui.host,
            "port": config.server.gui.port,
        },
    }

    devices_dict: Dict[str, Dict[str, Any]] = {}
    for name, definition in config.devices.items():
        settings = dict(definition.settings)
        entry = {"type": definition.type}
        entry.update(settings)
        devices_dict[name] = entry

    mappings_dict: Dict[str, List[Dict[str, Any]]] = {}
    for device_name, rules in config.mappings.items():
        serialised_rules: List[Dict[str, Any]] = []
        for rule in rules:
            entry: Dict[str, Any] = {"pattern": rule.pattern}
            if rule.action:
                entry["action"] = rule.action
            if rule.params:
                entry["params"] = dict(rule.params)
            # Preserve any additional keys for regex or extended mappings
            if getattr(rule, "extras", None):
                entry.update(dict(rule.extras))
            serialised_rules.append(entry)
        mappings_dict[device_name] = serialised_rules

    return {
        "server": server_dict,
        "devices": devices_dict,
        "mappings": mappings_dict,
    }


def save_config(path: Path, raw: Mapping[str, Any]) -> Config:
    """Validate and write configuration data to disk.

    Returns the parsed Config instance on success.
    """

    config = parse_config_dict(raw)
    serialisable = config_to_dict(config)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(serialisable, handle, sort_keys=False)
    return config
