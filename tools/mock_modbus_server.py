#!/usr/bin/env python3
"""Mock MODBUS-TCP server for integration testing.

Uses pymodbus to simulate a MODBUS device with predefined datastore.
"""

import argparse
import asyncio
import logging
import struct

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusSlaveContext,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer


# Predefined datastore layout:
# - Holding registers 40001-40002 (address 0-1): temperature float32_be (25.5°C)
# - Holding registers 40101-40102 (address 100-101): setpoint float32_be (20.0°C)
# - Input registers 30001-30002 (address 0-1): voltage uint32_be (12345)

def create_datastore() -> ModbusServerContext:
    """Create MODBUS datastore with predefined values."""
    
    # Encode temperature 25.5°C as float32_be
    temp_bytes = struct.pack(">f", 25.5)
    temp_regs = list(struct.unpack(">HH", temp_bytes))
    
    # Encode setpoint 20.0°C as float32_be
    setpoint_bytes = struct.pack(">f", 20.0)
    setpoint_regs = list(struct.unpack(">HH", setpoint_bytes))
    
    # Encode voltage 12345 as uint32_be
    voltage = 12345
    voltage_regs = [(voltage >> 16) & 0xFFFF, voltage & 0xFFFF]
    
    # Initialize holding registers (40001+)
    # Create block with 200 registers, initialize first few
    holding_values = [0] * 200
    holding_values[0:2] = temp_regs  # 40001-40002
    holding_values[100:102] = setpoint_regs  # 40101-40102
    
    # Initialize input registers (30001+)
    input_values = [0] * 100
    input_values[0:2] = voltage_regs  # 30001-30002
    
    # Create datastore blocks
    # Note: pymodbus uses 0-based addressing internally
    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 100),  # Discrete inputs
        co=ModbusSequentialDataBlock(0, [0] * 100),  # Coils
        hr=ModbusSequentialDataBlock(0, holding_values),  # Holding registers
        ir=ModbusSequentialDataBlock(0, input_values),  # Input registers
    )
    
    # Single-slave context (unit_id=1)
    context = ModbusServerContext(slaves={1: store}, single=False)
    
    return context


async def run_server(host: str = "127.0.0.1", port: int = 5020) -> None:
    """Run mock MODBUS-TCP server."""
    
    logging.info(f"Starting mock MODBUS-TCP server on {host}:{port}")
    logging.info("Datastore layout:")
    logging.info("  Holding 40001-40002 (address 0-1): temp=25.5°C (float32_be)")
    logging.info("  Holding 40101-40102 (address 100-101): setpoint=20.0°C (float32_be)")
    logging.info("  Input 30001-30002 (address 0-1): voltage=12345 (uint32_be)")
    
    context = create_datastore()
    
    await StartAsyncTcpServer(
        context=context,
        address=(host, port),
    )


def main() -> None:
    """Parse arguments and run server."""
    parser = argparse.ArgumentParser(description="Mock MODBUS-TCP server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=5020, help="TCP port")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    
    args = parser.parse_args()
    
    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    
    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        logging.info("Server stopped")


if __name__ == "__main__":
    main()
