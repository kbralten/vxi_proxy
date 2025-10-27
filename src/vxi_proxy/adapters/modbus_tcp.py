"""MODBUS-TCP adapter for VXI-11 proxy server.

Translates SCPI-style ASCII commands to MODBUS function calls using
the mapping engine, communicates with MODBUS devices over TCP/IP.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from typing import Any, List, Optional

from .base import AdapterError, DeviceAdapter
from ..mapping_engine import (
    MappingError,
    ModbusAction,
    decode_registers,
    translate_command,
    FC_READ_COILS,
    FC_READ_DISCRETE_INPUTS,
    FC_READ_HOLDING_REGISTERS,
    FC_READ_INPUT_REGISTERS,
    FC_WRITE_SINGLE_COIL,
    FC_WRITE_SINGLE_REGISTER,
    FC_WRITE_MULTIPLE_REGISTERS,
)


class ModbusTcpAdapter(DeviceAdapter):
    """MODBUS-TCP device adapter with command mapping.
    
    Connects to MODBUS devices over TCP/IP and translates VXI-11 ASCII
    commands to MODBUS protocol using configurable mapping rules.
    """

    def __init__(self, name: str, **settings: Any) -> None:
        """Initialize MODBUS-TCP adapter.
        
        Args:
            name: Device name for this adapter instance
            **settings: Configuration settings:
                - host (str, required): IP address or hostname
                - port (int, default 502): TCP port
                - unit_id (int, default 1): MODBUS unit identifier
                - timeout (float, default 5.0): Socket timeout in seconds
                - mappings (List[Dict], optional): Mapping rule dicts
                - requires_lock (bool, default False): Exclusive locking
        """
        super().__init__(name)
        self.requires_lock = bool(settings.get("requires_lock", False))
        
        host = settings.get("host")
        if not host:
            raise AdapterError("MODBUS-TCP 'host' setting is required")
        
        self._host = str(host)
        self._port = int(settings.get("port", 502))
        self._unit_id = int(settings.get("unit_id", 1))
        self._timeout = float(settings.get("timeout", 5.0))
        self._mappings = settings.get("mappings", [])
        
        self._socket: Optional[socket.socket] = None
        self._transaction_id = 0
        self._read_buffer = ""  # Buffer for ASCII responses
    
    async def connect(self) -> None:
        """Validate connection parameters."""
        if not self._host:
            raise AdapterError("MODBUS-TCP host not specified")
        if not (1 <= self._port <= 65535):
            raise AdapterError(f"Invalid port: {self._port}")
        if not (0 <= self._unit_id <= 255):
            raise AdapterError(f"Invalid unit_id: {self._unit_id}")
    
    async def acquire(self) -> None:
        """Open TCP connection to MODBUS device."""
        try:
            sock = await asyncio.to_thread(
                socket.create_connection,
                (self._host, self._port),
                self._timeout,
            )
            sock.settimeout(self._timeout)
            self._socket = sock
        except (OSError, socket.error) as exc:
            raise AdapterError(f"Failed to connect to {self._host}:{self._port}: {exc}") from exc
        
        # Call base class acquire for locking
        await super().acquire()
    
    def release(self) -> None:
        """Release lock and close TCP connection."""
        # Close socket first
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            finally:
                self._socket = None
        
        # Then release base class lock
        super().release()
    
    async def disconnect(self) -> None:
        """Disconnect from MODBUS device."""
        self.release()
    
    def _next_transaction_id(self) -> int:
        """Generate next MODBUS transaction ID."""
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        return self._transaction_id
    
    def _build_mbap_header(self, pdu_length: int, transaction_id: int) -> bytes:
        """Build MODBUS Application Protocol (MBAP) header.
        
        Args:
            pdu_length: Length of PDU (Protocol Data Unit)
            transaction_id: Transaction identifier
        
        Returns:
            7-byte MBAP header
        """
        protocol_id = 0  # MODBUS protocol
        # MBAP length = unit_id (1 byte) + PDU
        length = 1 + pdu_length
        
        return struct.pack(
            ">HHHB",
            transaction_id,
            protocol_id,
            length,
            self._unit_id,
        )
    
    def _build_read_request(self, function_code: int, address: int, count: int) -> bytes:
        """Build PDU for read functions (0x01-0x04)."""
        return struct.pack(">BHH", function_code, address, count)
    
    def _build_write_single_request(self, function_code: int, address: int, value: int) -> bytes:
        """Build PDU for write single functions (0x05, 0x06)."""
        return struct.pack(">BHH", function_code, address, value)
    
    def _build_write_multiple_request(self, address: int, values: List[int]) -> bytes:
        """Build PDU for write multiple registers (0x10)."""
        count = len(values)
        byte_count = count * 2
        
        # Pack header: function_code, address, count, byte_count
        pdu = struct.pack(">BHHB", FC_WRITE_MULTIPLE_REGISTERS, address, count, byte_count)
        
        # Pack register values
        for val in values:
            pdu += struct.pack(">H", val)
        
        return pdu
    
    async def _send_request(self, pdu: bytes) -> bytes:
        """Send MODBUS request and receive response.
        
        Args:
            pdu: Protocol Data Unit
        
        Returns:
            Response PDU (without MBAP header)
        
        Raises:
            AdapterError: On communication or protocol errors
        """
        if not self._socket:
            raise AdapterError("Socket not connected")
        
        transaction_id = self._next_transaction_id()
        mbap = self._build_mbap_header(len(pdu), transaction_id)
        request = mbap + pdu
        
        try:
            # Send request
            await asyncio.to_thread(self._socket.sendall, request)
            
            # Receive MBAP header (7 bytes)
            header = await asyncio.to_thread(self._socket.recv, 7)
            if len(header) < 7:
                raise AdapterError("Incomplete MBAP header received")
            
            recv_tid, recv_pid, recv_len, recv_uid = struct.unpack(">HHHB", header)
            
            # Validate response
            if recv_tid != transaction_id:
                raise AdapterError(f"Transaction ID mismatch: sent {transaction_id}, received {recv_tid}")
            if recv_pid != 0:
                raise AdapterError(f"Invalid protocol ID: {recv_pid}")
            
            # Receive PDU
            pdu_length = recv_len - 1  # Subtract unit_id byte
            response_pdu = await asyncio.to_thread(self._socket.recv, pdu_length)
            
            if len(response_pdu) < pdu_length:
                raise AdapterError("Incomplete PDU received")
            
            # Check for exception response
            function_code = response_pdu[0]
            if function_code >= 0x80:
                exception_code = response_pdu[1] if len(response_pdu) > 1 else 0
                raise AdapterError(f"MODBUS exception: function={function_code:#x}, code={exception_code:#x}")
            
            return response_pdu
        
        except socket.timeout as exc:
            raise AdapterError("MODBUS request timeout") from exc
        except (OSError, struct.error) as exc:
            raise AdapterError(f"MODBUS communication error: {exc}") from exc
    
    async def _execute_action(self, action: ModbusAction) -> Any:
        """Execute a MODBUS action and return decoded result.
        
        Args:
            action: ModbusAction from mapping engine
        
        Returns:
            Decoded value (int, float, bool, etc.)
        """
        fc = action.function_code
        
        # Build PDU based on function code
        if fc in (FC_READ_COILS, FC_READ_DISCRETE_INPUTS, FC_READ_HOLDING_REGISTERS, FC_READ_INPUT_REGISTERS):
            pdu = self._build_read_request(fc, action.address, action.count)
        
        elif fc in (FC_WRITE_SINGLE_COIL, FC_WRITE_SINGLE_REGISTER):
            if not action.values:
                raise AdapterError("Write action missing values")
            pdu = self._build_write_single_request(fc, action.address, action.values[0])
        
        elif fc == FC_WRITE_MULTIPLE_REGISTERS:
            if not action.values:
                raise AdapterError("Write multiple action missing values")
            pdu = self._build_write_multiple_request(action.address, action.values)
        
        else:
            raise AdapterError(f"Unsupported function code: {fc:#x}")
        
        # Send request and get response
        response = await self._send_request(pdu)
        
        # Parse response based on function code
        if fc in (FC_READ_HOLDING_REGISTERS, FC_READ_INPUT_REGISTERS):
            # Response format: function_code(1) + byte_count(1) + registers(N*2)
            byte_count = response[1]
            register_count = byte_count // 2
            
            registers = []
            for i in range(register_count):
                offset = 2 + i * 2
                reg_val = struct.unpack(">H", response[offset:offset+2])[0]
                registers.append(reg_val)
            
            # Decode registers to Python value
            return decode_registers(registers, action.data_type)
        
        elif fc in (FC_WRITE_SINGLE_REGISTER, FC_WRITE_MULTIPLE_REGISTERS):
            # Write responses echo address and value/count; return success indicator
            return "OK"
        
        else:
            # Other function codes not yet implemented
            return "OK"
    
    async def write(self, data: bytes) -> int:
        """Write ASCII command to MODBUS device (via mapping engine).
        
        Translates command to MODBUS action, executes it, buffers ASCII response.
        
        Returns:
            Number of bytes accepted
        """
        try:
            command = data.decode("ascii").strip()
            
            # Translate command to MODBUS action
            action = translate_command(command, self._mappings)
            
            # Execute action
            result = await self._execute_action(action)
            
            # Buffer ASCII response for query commands (reads)
            if action.function_code in (FC_READ_HOLDING_REGISTERS, FC_READ_INPUT_REGISTERS):
                # Format result as ASCII string
                if isinstance(result, float):
                    response = f"{result:.6f}"
                else:
                    response = str(result)
                self._read_buffer = response
            else:
                # Write commands don't produce query responses
                self._read_buffer = ""
            
            return len(data)
        
        except MappingError as exc:
            raise AdapterError(f"Command mapping failed: {exc}") from exc
        except Exception as exc:
            raise AdapterError(f"MODBUS operation failed: {exc}") from exc
    
    async def read(self, request_size: int) -> bytes:
        """Read buffered ASCII response from last query command.
        
        Args:
            request_size: Maximum bytes to read (ignored, returns full buffer)
        
        Returns:
            Buffered ASCII response as bytes
        """
        response = self._read_buffer
        self._read_buffer = ""  # Clear buffer after read
        return response.encode("ascii")
