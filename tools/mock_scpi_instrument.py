"""Mock SCPI serial instrument simulator for testing.

This script simulates a simple SCPI-compliant instrument over a serial port.
It responds to standard IEEE 488.2 common commands and a few measurement queries.

Usage:
    python mock_scpi_instrument.py COM10 --baudrate 115200

The instrument responds to:
    *IDN?           - Returns identification string
    *RST            - Resets the instrument (clears errors)
    *CLS            - Clears status
    *ESR?           - Returns event status register (always 0)
    *STB?           - Returns status byte (always 0)
    *OPC?           - Returns "1" (operation complete)
    SYST:ERR?       - Returns error queue (or "0,No error")
    MEAS:VOLT?      - Returns a simulated voltage reading
    MEAS:CURR?      - Returns a simulated current reading
    MEAS:TEMP?      - Returns a simulated temperature reading

Example virtual serial port setup:
    Windows (com0com):
        1. Install com0com from https://com0com.sourceforge.net/
        2. Create a pair: COM10 <-> COM11
        3. Run this script on COM10
        4. Connect your VXI-11 proxy to COM11

    Linux (socat):
        1. Create virtual pair:
           socat -d -d pty,raw,echo=0 pty,raw,echo=0
        2. Note the PTY paths (e.g., /dev/pts/3 and /dev/pts/4)
        3. Run this script on one PTY
        4. Connect your VXI-11 proxy to the other PTY
"""

import argparse
import os
import random
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import serial  # type: ignore


class MockScpiInstrument:
    """Simulates a SCPI instrument with basic IEEE 488.2 commands."""

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 0.1):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial_port: "serial.Serial | None" = None
        self.running = False
        self.error_queue: list[tuple[int, str]] = []

        # Simulated instrument state
        self.manufacturer = "Mock Instruments Inc."
        self.model = "SCPI-SIM-1000"
        self.serial_number = "SIM123456"
        self.firmware_version = "1.0.0"

        # Measurement simulation state
        self.base_voltage = 5.0
        self.base_current = 0.5
        self.base_temp = 25.0

    def connect(self) -> None:
        """Open the serial port and prepare for communication."""
        try:
            import serial  # type: ignore
        except ImportError:
            print("ERROR: pyserial is required. Install it with: pip install pyserial")
            sys.exit(1)

        # Normalize Windows COM port names to include \\.\\ prefix
        if os.name == "nt" and not self.port.startswith("\\\\.\\"):
            self.port = f"\\\\.\\{self.port}"

        print(f"Opening serial port {self.port} at {self.baudrate} baud...")
        try:
            self.serial_port = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=1.0,
            )
            print(f"Serial port {self.port} opened successfully")
            print(f"  Settings: {self.baudrate} 8N1")
            print(f"  Timeout: {self.timeout}s")
        except Exception as exc:
            print(f"Failed to open serial port: {exc}")
            sys.exit(1)

    def disconnect(self) -> None:
        """Close the serial port."""
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
            print(f"\nSerial port {self.port} closed")

    def read_command(self) -> str | None:
        """Read a line from the serial port (blocking with timeout)."""
        if not self.serial_port:
            return None

        try:
            # Read until newline or timeout
            line = self.serial_port.readline()
            if line:
                # Decode and strip termination characters
                cmd = line.decode("utf-8", errors="replace").strip()
                return cmd
        except Exception as exc:
            print(f"Read error: {exc}")
        return None

    def write_response(self, response: str) -> None:
        """Write a response back to the serial port."""
        if not self.serial_port:
            return

        try:
            # Add newline termination
            data = (response + "\n").encode("utf-8")
            self.serial_port.write(data)
            self.serial_port.flush()
        except Exception as exc:
            print(f"Write error: {exc}")

    def handle_command(self, cmd: str) -> str | None:
        """Process a SCPI command and return the response (or None for no response)."""
        cmd = cmd.strip().upper()

        # IEEE 488.2 Common Commands
        if cmd == "*IDN?":
            return f"{self.manufacturer},{self.model},{self.serial_number},{self.firmware_version}"

        elif cmd == "*RST":
            self.error_queue.clear()
            return None  # No response for write commands

        elif cmd == "*CLS":
            self.error_queue.clear()
            return None

        elif cmd == "*ESR?":
            return "0"  # Event Status Register (no events)

        elif cmd == "*STB?":
            return "0"  # Status Byte (no status bits set)

        elif cmd == "*OPC?":
            return "1"  # Operation complete

        elif cmd == "*OPC":
            return None  # Set operation complete flag (no response)

        # System commands
        elif cmd == "SYST:ERR?" or cmd == "SYSTEM:ERROR?":
            if self.error_queue:
                code, msg = self.error_queue.pop(0)
                return f"{code},{msg}"
            else:
                return "0,No error"

        # Measurement commands (simulated with random variations)
        elif cmd == "MEAS:VOLT?" or cmd == "MEASURE:VOLTAGE?":
            voltage = self.base_voltage + random.uniform(-0.1, 0.1)
            return f"{voltage:.4f}"

        elif cmd == "MEAS:CURR?" or cmd == "MEASURE:CURRENT?":
            current = self.base_current + random.uniform(-0.01, 0.01)
            return f"{current:.5f}"

        elif cmd == "MEAS:TEMP?" or cmd == "MEASURE:TEMPERATURE?":
            temp = self.base_temp + random.uniform(-0.5, 0.5)
            return f"{temp:.2f}"

        # Unknown command
        else:
            # Add error to queue: -113 = Undefined header
            self.error_queue.append((-113, f"Undefined header: {cmd[:20]}"))
            return None

    def run(self) -> None:
        """Main loop: read commands and send responses."""
        self.running = True
        print(f"\n{'='*60}")
        print(f"Mock SCPI Instrument Running")
        print(f"{'='*60}")
        print(f"Manufacturer: {self.manufacturer}")
        print(f"Model:        {self.model}")
        print(f"Serial:       {self.serial_number}")
        print(f"Firmware:     {self.firmware_version}")
        print(f"{'='*60}")
        print("Waiting for commands... (Press Ctrl+C to stop)\n")

        try:
            while self.running:
                cmd = self.read_command()
                if cmd:
                    timestamp = time.strftime("%H:%M:%S")
                    print(f"[{timestamp}] RX: {cmd}")

                    # Add debug logging for received commands
                    print(f"[DEBUG] Received command: {cmd}")

                    response = self.handle_command(cmd)
                    if response is not None:
                        self.write_response(response)
                        print(f"[{timestamp}] TX: {response}")
                    else:
                        print(f"[{timestamp}] (no response)")

        except KeyboardInterrupt:
            print("\n\nReceived interrupt signal, shutting down...")
            self.running = False


def main():
    parser = argparse.ArgumentParser(
        description="Mock SCPI serial instrument simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "port",
        help="Serial port to use (e.g., COM10, /dev/ttyUSB0, /dev/pts/3)",
    )
    parser.add_argument(
        "--baudrate",
        "-b",
        type=int,
        default=115200,
        help="Baud rate (default: 115200)",
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=float,
        default=0.1,
        help="Read timeout in seconds (default: 0.1)",
    )

    args = parser.parse_args()

    instrument = MockScpiInstrument(
        port=args.port,
        baudrate=args.baudrate,
        timeout=args.timeout,
    )

    try:
        instrument.connect()
        instrument.run()
    finally:
        instrument.disconnect()


if __name__ == "__main__":
    main()
