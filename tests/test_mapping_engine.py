"""Unit tests for the MODBUS mapping engine."""

import sys
import unittest
from pathlib import Path

# Ensure project src is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vxi_proxy.mapping_engine import (
    MappingError,
    ModbusAction,
    decode_registers,
    encode_value,
    translate_command,
    FC_READ_HOLDING_REGISTERS,
    FC_READ_INPUT_REGISTERS,
    FC_WRITE_SINGLE_REGISTER,
    FC_WRITE_MULTIPLE_REGISTERS,
)


class TestMappingEngine(unittest.TestCase):
    """Test command translation and data type conversions."""
    
    def test_simple_pattern_match(self):
        """Test basic pattern matching without capture groups."""
        rules = [
            {
                "pattern": r"MEAS:TEMP\?",
                "action": "read_holding_registers",
                "params": {
                    "address": 0,
                    "count": 2,
                    "data_type": "float32_be",
                },
            }
        ]
        
        action = translate_command("MEAS:TEMP?", rules)
        
        self.assertEqual(action.function_code, FC_READ_HOLDING_REGISTERS)
        self.assertEqual(action.address, 0)
        self.assertEqual(action.count, 2)
        self.assertEqual(action.data_type, "float32_be")
        self.assertIsNone(action.values)
    
    def test_regex_capture_group(self):
        """Test pattern with capture group for write value."""
        rules = [
            {
                "pattern": r"SOUR:VOLT\s+(\d+\.?\d*)",
                "action": "write_holding_registers",
                "params": {
                    "address": 100,
                    "value": "$1",
                    "data_type": "float32_be",
                },
            }
        ]
        
        action = translate_command("SOUR:VOLT 3.14", rules)
        
        self.assertEqual(action.function_code, FC_WRITE_MULTIPLE_REGISTERS)
        self.assertEqual(action.address, 100)
        self.assertEqual(action.count, 2)  # float32 = 2 registers
        self.assertIsNotNone(action.values)
        assert action.values is not None  # Type narrowing for mypy/pylance
        self.assertEqual(len(action.values), 2)
    
    def test_uint16_data_type(self):
        """Test uint16 encoding and decoding."""
        # Encode
        regs = encode_value(12345, "uint16")
        self.assertEqual(regs, [12345])
        
        # Decode
        val = decode_registers([12345], "uint16")
        self.assertEqual(val, 12345)
    
    def test_int16_data_type(self):
        """Test int16 with negative value."""
        # Encode negative value
        regs = encode_value(-100, "int16")
        self.assertEqual(regs, [65436])  # Two's complement
        
        # Decode
        val = decode_registers([65436], "int16")
        self.assertEqual(val, -100)
    
    def test_float32_be_data_type(self):
        """Test float32 big-endian encoding and decoding."""
        # Encode
        regs = encode_value(25.5, "float32_be")
        self.assertEqual(len(regs), 2)
        
        # Decode
        val = decode_registers(regs, "float32_be")
        self.assertAlmostEqual(val, 25.5, places=5)
    
    def test_uint32_be_data_type(self):
        """Test uint32 big-endian encoding and decoding."""
        # Encode
        regs = encode_value(0x12345678, "uint32_be")
        self.assertEqual(regs, [0x1234, 0x5678])
        
        # Decode
        val = decode_registers([0x1234, 0x5678], "uint32_be")
        self.assertEqual(val, 0x12345678)
    
    def test_uint32_le_data_type(self):
        """Test uint32 little-endian encoding and decoding."""
        # Encode
        regs = encode_value(0x12345678, "uint32_le")
        self.assertEqual(regs, [0x5678, 0x1234])
        
        # Decode
        val = decode_registers([0x5678, 0x1234], "uint32_le")
        self.assertEqual(val, 0x12345678)
    
    def test_unmapped_command_raises_error(self):
        """Test that unmapped command raises MappingError."""
        rules = [
            {
                "pattern": r"KNOWN:CMD\?",
                "action": "read_holding_registers",
                "params": {"address": 0, "count": 1},
            }
        ]
        
        with self.assertRaises(MappingError) as ctx:
            translate_command("UNKNOWN:CMD?", rules)
        
        self.assertIn("No mapping rule matched", str(ctx.exception))
    
    def test_invalid_action_raises_error(self):
        """Test that unknown action name raises MappingError."""
        rules = [
            {
                "pattern": r"TEST\?",
                "action": "invalid_action_name",
                "params": {"address": 0},
            }
        ]
        
        with self.assertRaises(MappingError) as ctx:
            translate_command("TEST?", rules)
        
        self.assertIn("Unknown action", str(ctx.exception))
    
    def test_missing_address_raises_error(self):
        """Test that missing address parameter raises error."""
        rules = [
            {
                "pattern": r"TEST\?",
                "action": "read_holding_registers",
                "params": {"count": 1},  # Missing address
            }
        ]
        
        with self.assertRaises(MappingError) as ctx:
            translate_command("TEST?", rules)
        
        self.assertIn("address", str(ctx.exception).lower())
    
    def test_write_without_value_raises_error(self):
        """Test that write action without value raises error."""
        rules = [
            {
                "pattern": r"SET:VAL",
                "action": "write_single_register",
                "params": {"address": 0},  # Missing value
            }
        ]
        
        with self.assertRaises(MappingError) as ctx:
            translate_command("SET:VAL", rules)
        
        self.assertIn("value", str(ctx.exception).lower())
    
    def test_multiple_rules_precedence(self):
        """Test that first matching rule wins."""
        rules = [
            {
                "pattern": r"MEAS:(\w+)\?",
                "action": "read_holding_registers",
                "params": {"address": 0, "count": 1},
            },
            {
                "pattern": r"MEAS:TEMP\?",
                "action": "read_input_registers",
                "params": {"address": 100, "count": 1},
            },
        ]
        
        action = translate_command("MEAS:TEMP?", rules)
        
        # First rule should match
        self.assertEqual(action.function_code, FC_READ_HOLDING_REGISTERS)
        self.assertEqual(action.address, 0)
    
    def test_case_insensitive_matching(self):
        """Test that pattern matching is case-insensitive."""
        rules = [
            {
                "pattern": r"meas:temp\?",
                "action": "read_holding_registers",
                "params": {"address": 0, "count": 1},
            }
        ]
        
        action = translate_command("MEAS:TEMP?", rules)
        self.assertEqual(action.function_code, FC_READ_HOLDING_REGISTERS)
    
    def test_unknown_data_type_encode_raises_error(self):
        """Test that unknown data type raises error on encode."""
        with self.assertRaises(MappingError) as ctx:
            encode_value(123, "unknown_type")
        
        self.assertIn("Unknown data type", str(ctx.exception))
    
    def test_unknown_data_type_decode_raises_error(self):
        """Test that unknown data type raises error on decode."""
        with self.assertRaises(MappingError) as ctx:
            decode_registers([123], "unknown_type")
        
        self.assertIn("Unknown data type", str(ctx.exception))
    
    def test_uint16_overflow_raises_error(self):
        """Test that uint16 overflow raises error."""
        with self.assertRaises(MappingError) as ctx:
            encode_value(70000, "uint16")
        
        self.assertIn("out of range", str(ctx.exception))
    
    def test_int16_overflow_raises_error(self):
        """Test that int16 overflow raises error."""
        with self.assertRaises(MappingError) as ctx:
            encode_value(40000, "int16")
        
        self.assertIn("out of range", str(ctx.exception))
    
    def test_bool_data_type(self):
        """Test boolean encoding and decoding."""
        # Encode True
        regs_true = encode_value(True, "bool")
        self.assertEqual(regs_true, [1])
        
        # Encode False
        regs_false = encode_value(False, "bool")
        self.assertEqual(regs_false, [0])
        
        # Decode
        self.assertTrue(decode_registers([1], "bool"))
        self.assertFalse(decode_registers([0], "bool"))
    
    def test_write_single_vs_multiple(self):
        """Test that single-register writes use correct function code."""
        rules_single = [
            {
                "pattern": r"SET:VAL\s+(\d+)",
                "action": "write_single_register",
                "params": {
                    "address": 0,
                    "value": "$1",
                    "data_type": "uint16",
                },
            }
        ]
        
        rules_multiple = [
            {
                "pattern": r"SET:TEMP\s+(\d+\.?\d*)",
                "action": "write_holding_registers",
                "params": {
                    "address": 0,
                    "value": "$1",
                    "data_type": "float32_be",
                },
            }
        ]
        
        # Single register write
        action_single = translate_command("SET:VAL 123", rules_single)
        self.assertEqual(action_single.function_code, FC_WRITE_SINGLE_REGISTER)
        self.assertEqual(action_single.count, 1)
        
        # Multiple register write
        action_multiple = translate_command("SET:TEMP 25.5", rules_multiple)
        self.assertEqual(action_multiple.function_code, FC_WRITE_MULTIPLE_REGISTERS)
        self.assertEqual(action_multiple.count, 2)


if __name__ == "__main__":
    unittest.main()
