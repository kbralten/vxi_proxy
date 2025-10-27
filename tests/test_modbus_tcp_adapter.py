"""Unit tests for MODBUS-TCP adapter."""

import asyncio
import socket
import struct
import sys
import unittest
from pathlib import Path

# Ensure project src is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vxi_proxy.adapters.modbus_tcp import ModbusTcpAdapter
from vxi_proxy.adapters.base import AdapterError
from vxi_proxy.mapping_engine import FC_READ_HOLDING_REGISTERS, FC_WRITE_SINGLE_REGISTER


class FakeModbusSocket:
    """Fake socket that simulates MODBUS-TCP responses."""
    
    def __init__(self):
        self._request_buf = bytearray()
        self._response_queue = []
        self.timeout = None
        self.closed = False
        self._recv_count = 0
    
    def settimeout(self, t):
        self.timeout = t
    
    def sendall(self, data: bytes):
        """Store request for verification."""
        self._request_buf = bytearray(data)
        self._recv_count = 0  # Reset recv counter for new request
    
    def recv(self, size: int) -> bytes:
        """Return queued response or auto-generate based on request."""
        if self.closed:
            return b""
        
        # Use queued responses if available
        if self._response_queue:
            response = self._response_queue.pop(0)
            return response
        
        # Auto-generate response from last request
        if len(self._request_buf) < 8:  # MBAP + function code
            raise socket.timeout()
        
        # Parse request MBAP header
        tid, pid, length, uid = struct.unpack(">HHHB", bytes(self._request_buf[:7]))
        function_code = self._request_buf[7]
        
        # Generate full response
        response = None
        
        if function_code == FC_READ_HOLDING_REGISTERS:
            # Read holding registers: return 2 registers with value 25.5°C as float32_be
            temp_bytes = struct.pack(">f", 25.5)
            temp_regs = struct.unpack(">HH", temp_bytes)
            
            response_pdu = struct.pack(">BB", function_code, 4)  # byte_count=4 (2 regs)
            response_pdu += struct.pack(">HH", *temp_regs)
            
            response_mbap = struct.pack(">HHHB", tid, pid, len(response_pdu) + 1, uid)
            response = response_mbap + response_pdu
        
        elif function_code == FC_WRITE_SINGLE_REGISTER:
            # Write single register: echo address and value
            address, value = struct.unpack(">HH", bytes(self._request_buf[8:12]))
            response_pdu = struct.pack(">BHH", function_code, address, value)
            response_mbap = struct.pack(">HHHB", tid, pid, len(response_pdu) + 1, uid)
            response = response_mbap + response_pdu
        
        if response is None:
            raise socket.timeout()
        
        # Return portions based on recv count (first call gets header, second gets PDU)
        self._recv_count += 1
        if self._recv_count == 1 and size == 7:
            # Return just MBAP header
            return response[:7]
        else:
            # Return PDU portion
            len(response) - 7
            return response[7:7+size]
    
    def push_response(self, data: bytes):
        """Queue a specific response for next recv()."""
        self._response_queue.append(data)
    
    def get_last_request(self) -> bytes:
        """Get the last request sent."""
        return bytes(self._request_buf)
    
    def close(self):
        self.closed = True


class ModbusTcpAdapterTests(unittest.TestCase):
    """Test MODBUS-TCP adapter with fake socket."""
    
    def test_missing_host_raises_error(self):
        """Test that missing host setting raises error."""
        with self.assertRaises(AdapterError) as ctx:
            ModbusTcpAdapter("test")
        
        self.assertIn("host", str(ctx.exception).lower())
    
    def test_requires_lock_default_false(self):
        """Test that requires_lock defaults to False for MODBUS TCP."""
        mappings = [
            {
                "pattern": r"TEST\?",
                "action": "read_holding_registers",
                "params": {"address": 0, "count": 1},
            }
        ]
        
        adapter = ModbusTcpAdapter("test", host="127.0.0.1", mappings=mappings)
        self.assertFalse(adapter.requires_lock)
    
    def test_requires_lock_override(self):
        """Test that requires_lock can be overridden."""
        adapter = ModbusTcpAdapter("test", host="127.0.0.1", requires_lock=True)
        self.assertTrue(adapter.requires_lock)
    
    def test_read_holding_registers(self):
        """Test read holding registers command flow."""
        fake = FakeModbusSocket()
        
        def fake_create_connection(addr, timeout=None):
            return fake
        
        mappings = [
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
        
        adapter = ModbusTcpAdapter("test", host="127.0.0.1", port=502, mappings=mappings)
        
        orig = socket.create_connection
        socket.create_connection = fake_create_connection
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def run():
                    await adapter.connect()
                    await adapter.acquire()
                    
                    # Write query command
                    n = await adapter.write(b"MEAS:TEMP?")
                    self.assertGreater(n, 0)
                    
                    # Verify MODBUS request was sent
                    request = fake.get_last_request()
                    self.assertGreater(len(request), 7)
                    
                    # Verify MBAP header
                    tid, pid, length, uid = struct.unpack(">HHHB", request[:7])
                    self.assertEqual(pid, 0)  # MODBUS protocol
                    self.assertEqual(uid, 1)  # Default unit_id
                    
                    # Verify PDU
                    function_code = request[7]
                    self.assertEqual(function_code, FC_READ_HOLDING_REGISTERS)
                    
                    # Read response
                    data = await adapter.read(1024)
                    response = data.decode("ascii")
                    
                    # Should get 25.5 from fake socket
                    self.assertIn("25.5", response)
                    
                    adapter.release()
                    await adapter.disconnect()
                
                loop.run_until_complete(run())
            finally:
                loop.close()
        finally:
            socket.create_connection = orig
    
    def test_write_single_register(self):
        """Test write single register command flow."""
        fake = FakeModbusSocket()
        
        def fake_create_connection(addr, timeout=None):
            return fake
        
        mappings = [
            {
                "pattern": r"SOUR:VOLT\s+(\d+)",
                "action": "write_single_register",
                "params": {
                    "address": 100,
                    "value": "$1",
                    "data_type": "uint16",
                },
            }
        ]
        
        adapter = ModbusTcpAdapter("test", host="127.0.0.1", mappings=mappings)
        
        orig = socket.create_connection
        socket.create_connection = fake_create_connection
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def run():
                    await adapter.connect()
                    await adapter.acquire()
                    
                    # Write command with value
                    n = await adapter.write(b"SOUR:VOLT 123")
                    self.assertGreater(n, 0)
                    
                    # Verify MODBUS request
                    request = fake.get_last_request()
                    function_code = request[7]
                    self.assertEqual(function_code, FC_WRITE_SINGLE_REGISTER)
                    
                    # Verify address and value
                    address, value = struct.unpack(">HH", request[8:12])
                    self.assertEqual(address, 100)
                    self.assertEqual(value, 123)
                    
                    adapter.release()
                    await adapter.disconnect()
                
                loop.run_until_complete(run())
            finally:
                loop.close()
        finally:
            socket.create_connection = orig
    
    def test_write_multiple_registers_float32(self):
        """Test write multiple registers with float32_be data type."""
        fake = FakeModbusSocket()
        
        def fake_create_connection(addr, timeout=None):
            return fake
        
        mappings = [
            {
                "pattern": r"SOUR:TEMP\s+(\d+\.?\d*)",
                "action": "write_holding_registers",
                "params": {
                    "address": 100,
                    "value": "$1",
                    "data_type": "float32_be",
                },
            }
        ]
        
        adapter = ModbusTcpAdapter("test", host="127.0.0.1", mappings=mappings)
        
        orig = socket.create_connection
        socket.create_connection = fake_create_connection
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def run():
                    await adapter.connect()
                    await adapter.acquire()
                    
                    # Queue response for write multiple
                    tid = 1
                    function_code = 0x10
                    address = 100
                    count = 2
                    response_pdu = struct.pack(">BHH", function_code, address, count)
                    response_mbap = struct.pack(">HHHB", tid, 0, len(response_pdu) + 1, 1)
                    fake.push_response(response_mbap)
                    fake.push_response(response_pdu)
                    
                    # Write command with float value
                    n = await adapter.write(b"SOUR:TEMP 25.5")
                    self.assertGreater(n, 0)
                    
                    # Verify MODBUS request
                    request = fake.get_last_request()
                    function_code = request[7]
                    self.assertEqual(function_code, 0x10)  # Write multiple registers
                    
                    # Verify address and count
                    req_address, req_count, byte_count = struct.unpack(">HHB", request[8:13])
                    self.assertEqual(req_address, 100)
                    self.assertEqual(req_count, 2)  # float32 = 2 registers
                    self.assertEqual(byte_count, 4)  # 2 regs * 2 bytes
                    
                    adapter.release()
                    await adapter.disconnect()
                
                loop.run_until_complete(run())
            finally:
                loop.close()
        finally:
            socket.create_connection = orig
    
    def test_exception_response_raises_error(self):
        """Test that MODBUS exception response raises AdapterError."""
        fake = FakeModbusSocket()
        
        def fake_create_connection(addr, timeout=None):
            return fake
        
        mappings = [
            {
                "pattern": r"TEST\?",
                "action": "read_holding_registers",
                "params": {"address": 0, "count": 1, "data_type": "uint16"},
            }
        ]
        
        adapter = ModbusTcpAdapter("test", host="127.0.0.1", mappings=mappings)
        
        # Create exception response (function_code = 0x83, exception_code = 0x02)
        # Must be queued BEFORE we set up the connection
        tid = 1
        exception_pdu = struct.pack(">BB", 0x83, 0x02)
        exception_mbap = struct.pack(">HHHB", tid, 0, len(exception_pdu) + 1, 1)
        
        orig = socket.create_connection
        socket.create_connection = fake_create_connection
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def run():
                    await adapter.connect()
                    await adapter.acquire()
                    
                    # Queue the exception response before writing
                    fake.push_response(exception_mbap)
                    fake.push_response(exception_pdu)
                    
                    # Write should raise error due to exception response
                    with self.assertRaises(AdapterError) as ctx:
                        await adapter.write(b"TEST?")
                    
                    self.assertIn("exception", str(ctx.exception).lower())
                    
                    adapter.release()
                    await adapter.disconnect()
                
                loop.run_until_complete(run())
            finally:
                loop.close()
        finally:
            socket.create_connection = orig


if __name__ == "__main__":
    unittest.main()
