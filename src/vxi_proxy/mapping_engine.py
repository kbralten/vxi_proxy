"""Command mapping engine for MODBUS devices.

This module translates SCPI-style ASCII commands into MODBUS function calls
using a rule-based pattern matching system configured via YAML.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional


class MappingError(Exception):
    """Raised when command mapping fails."""


@dataclass
class ModbusAction:
    """Represents a MODBUS operation translated from a command string."""

    function_code: int
    address: int
    count: int = 1
    values: Optional[List[int]] = None  # For write operations (register values)
    data_type: str = "uint16"
    # Optional scaling to apply to numeric responses (e.g., 100 for x100 -> float)
    response_scale: Optional[float] = None


# MODBUS function code constants
FC_READ_COILS = 0x01
FC_READ_DISCRETE_INPUTS = 0x02
FC_READ_HOLDING_REGISTERS = 0x03
FC_READ_INPUT_REGISTERS = 0x04
FC_WRITE_SINGLE_COIL = 0x05
FC_WRITE_SINGLE_REGISTER = 0x06
FC_WRITE_MULTIPLE_COILS = 0x0F
FC_WRITE_MULTIPLE_REGISTERS = 0x10

# Action name to function code mapping
ACTION_MAP: Dict[str, int] = {
    "read_coils": FC_READ_COILS,
    "read_discrete_inputs": FC_READ_DISCRETE_INPUTS,
    "read_holding_registers": FC_READ_HOLDING_REGISTERS,
    "read_input_registers": FC_READ_INPUT_REGISTERS,
    "write_single_coil": FC_WRITE_SINGLE_COIL,
    "write_single_register": FC_WRITE_SINGLE_REGISTER,
    "write_multiple_coils": FC_WRITE_MULTIPLE_COILS,
    "write_holding_registers": FC_WRITE_MULTIPLE_REGISTERS,
}


@lru_cache(maxsize=128)
def _compile_pattern(pattern: str) -> re.Pattern:
    """Compile and cache regex patterns for command matching."""
    return re.compile(pattern, re.IGNORECASE)


def encode_value(value: Any, data_type: str) -> List[int]:
    """Encode a Python value to MODBUS register values based on data type.
    
    Args:
        value: Python value to encode (int, float, bool)
        data_type: Type specifier (uint16, int16, uint32_be, float32_be, etc.)
    
    Returns:
        List of 16-bit register values
    
    Raises:
        MappingError: If data type is unknown or value cannot be encoded
    """
    try:
        if data_type == "uint16":
            val = int(value)
            if not (0 <= val <= 65535):
                raise MappingError(f"uint16 value {val} out of range [0, 65535]")
            return [val]
        
        elif data_type == "int16":
            val = int(value)
            if not (-32768 <= val <= 32767):
                raise MappingError(f"int16 value {val} out of range [-32768, 32767]")
            # Convert to unsigned for transmission
            return [val if val >= 0 else (65536 + val)]
        
        elif data_type == "uint32_be":
            val = int(value)
            if not (0 <= val <= 4294967295):
                raise MappingError(f"uint32 value {val} out of range")
            hi = (val >> 16) & 0xFFFF
            lo = val & 0xFFFF
            return [hi, lo]
        
        elif data_type == "uint32_le":
            val = int(value)
            if not (0 <= val <= 4294967295):
                raise MappingError(f"uint32 value {val} out of range")
            lo = val & 0xFFFF
            hi = (val >> 16) & 0xFFFF
            return [lo, hi]
        
        elif data_type == "float32_be":
            val = float(value)
            packed = struct.pack(">f", val)
            return list(struct.unpack(">HH", packed))
        
        elif data_type == "float32_le":
            val = float(value)
            packed = struct.pack("<f", val)
            return list(struct.unpack("<HH", packed))
        
        elif data_type == "bool":
            return [1 if value else 0]
        
        else:
            raise MappingError(f"Unknown data type: {data_type}")
    
    except (ValueError, struct.error) as exc:
        raise MappingError(f"Cannot encode value {value!r} as {data_type}: {exc}") from exc


def decode_registers(registers: List[int], data_type: str) -> Any:
    """Decode MODBUS register values to a Python value.
    
    Args:
        registers: List of 16-bit register values
        data_type: Type specifier
    
    Returns:
        Decoded Python value (int, float, bool)
    
    Raises:
        MappingError: If data type is unknown or registers cannot be decoded
    """
    try:
        if data_type == "uint16":
            if len(registers) < 1:
                raise MappingError("Need at least 1 register for uint16")
            return registers[0]
        
        elif data_type == "int16":
            if len(registers) < 1:
                raise MappingError("Need at least 1 register for int16")
            val = registers[0]
            # Convert from unsigned to signed
            return val if val < 32768 else (val - 65536)
        
        elif data_type == "uint32_be":
            if len(registers) < 2:
                raise MappingError("Need at least 2 registers for uint32")
            return (registers[0] << 16) | registers[1]
        
        elif data_type == "uint32_le":
            if len(registers) < 2:
                raise MappingError("Need at least 2 registers for uint32")
            return (registers[1] << 16) | registers[0]
        
        elif data_type == "float32_be":
            if len(registers) < 2:
                raise MappingError("Need at least 2 registers for float32")
            packed = struct.pack(">HH", registers[0], registers[1])
            return struct.unpack(">f", packed)[0]
        
        elif data_type == "float32_le":
            if len(registers) < 2:
                raise MappingError("Need at least 2 registers for float32")
            packed = struct.pack("<HH", registers[0], registers[1])
            return struct.unpack("<f", packed)[0]
        
        elif data_type == "bool":
            if len(registers) < 1:
                raise MappingError("Need at least 1 register for bool")
            return bool(registers[0])
        
        else:
            raise MappingError(f"Unknown data type: {data_type}")
    
    except (struct.error, IndexError) as exc:
        raise MappingError(f"Cannot decode registers as {data_type}: {exc}") from exc


def translate_command(command: str, rules: List[Dict[str, Any]]) -> ModbusAction:
    """Translate a SCPI-style command to a MODBUS action using mapping rules.
    
    Args:
        command: ASCII command string from VXI-11 client
        rules: List of mapping rule dicts with pattern/action/params keys
    
    Returns:
        ModbusAction ready for MODBUS adapter to execute
    
    Raises:
        MappingError: If no pattern matches or rule params are invalid
    """
    if not rules:
        raise MappingError(f"No mapping rules configured for command: {command!r}")
    
    # Strip whitespace and try each rule in order
    cmd = command.strip()
    
    for rule in rules:
        pattern = rule.get("pattern")
        if not pattern:
            continue
        
        # Compile and match pattern
        regex = _compile_pattern(str(pattern))
        match = regex.match(cmd)
        
        if not match:
            continue
        
        # Found a match; extract action and params
        action_name = rule.get("action")
        if not action_name:
            raise MappingError(f"Rule missing 'action' field for pattern {pattern!r}")
        
        function_code = ACTION_MAP.get(action_name)
        if function_code is None:
            raise MappingError(f"Unknown action: {action_name!r}")
        
        params = rule.get("params", {})
        address = params.get("address")
        if address is None:
            raise MappingError(f"Rule missing 'address' in params for pattern {pattern!r}")
        
        address = int(address)
        count = int(params.get("count", 1))
        data_type = str(params.get("data_type", "uint16"))
        # Optional response scaling (prefer params, fall back to top-level for convenience)
        raw_scale = params.get("response_scale") if isinstance(params, dict) else None
        if raw_scale is None:
            raw_scale = rule.get("response_scale")
        response_scale: Optional[float]
        try:
            response_scale = float(raw_scale) if raw_scale is not None else None
        except Exception:
            response_scale = None
        
        # For write operations, extract value (may use regex capture groups)
        values: Optional[List[int]] = None
        if function_code in (FC_WRITE_SINGLE_COIL, FC_WRITE_SINGLE_REGISTER, FC_WRITE_MULTIPLE_REGISTERS):
            value_template = params.get("value")
            if value_template is None:
                raise MappingError("Write action missing 'value' in params")
            
            # Substitute captured groups ($1, $2, etc.)
            value_str = str(value_template)
            for i, group in enumerate(match.groups(), start=1):
                if group is not None:
                    value_str = value_str.replace(f"${i}", group)
            
            # Handle special bool values
            if value_str.lower() in ("true", "on", "1"):
                value = True
            elif value_str.lower() in ("false", "off", "0"):
                value = False
            else:
                # Try to parse as number
                try:
                    value = float(value_str) if "." in value_str else int(value_str)
                except ValueError as exc:
                    raise MappingError(f"Cannot parse value: {value_str!r}") from exc

            # Optional input scaling for numeric writes (e.g., 12.34 V * 100 -> 1234)
            raw_wscale = params.get("scale") if isinstance(params, dict) else None
            if raw_wscale is None:
                raw_wscale = rule.get("scale")
            try:
                wscale = float(raw_wscale) if raw_wscale is not None else None
            except Exception:
                wscale = None
            if wscale is not None and isinstance(value, (int, float)):
                try:
                    value = int(round(float(value) * wscale))
                except Exception:
                    pass
            
            # Encode to register values
            values = encode_value(value, data_type)
            
            # Adjust count for multi-register writes
            if function_code == FC_WRITE_MULTIPLE_REGISTERS:
                count = len(values)
        
        return ModbusAction(
            function_code=function_code,
            address=address,
            count=count,
            values=values,
            data_type=data_type,
            response_scale=response_scale,
        )
    
    # No rule matched
    raise MappingError(f"No mapping rule matched command: {command!r}")
