# Mock SCPI Instrument Simulator

A Python-based serial port simulator that emulates a SCPI-compliant instrument for testing the VXI-11 proxy's SCPI-Serial adapter without physical hardware.

## Features

- Responds to standard IEEE 488.2 common commands (`*IDN?`, `*RST`, `*CLS`, etc.)
- Simulates measurement queries (`MEAS:VOLT?`, `MEAS:CURR?`, `MEAS:TEMP?`)
- Returns realistic readings with random variations
- Implements basic error queue handling
- Configurable serial port settings (baud rate, timeout)

## Requirements

```bash
pip install pyserial
```

## Usage

### Basic Usage

```bash
python tools/mock_scpi_instrument.py COM10
```

### With Custom Settings

```bash
python tools/mock_scpi_instrument.py COM10 --baudrate 9600 --timeout 0.5
```

### Command Line Options

- `port` - Serial port name (required)
  - Windows: `COM10`, `COM11`, etc.
  - Linux: `/dev/ttyUSB0`, `/dev/pts/3`, etc.
- `--baudrate`, `-b` - Baud rate (default: 115200)
- `--timeout`, `-t` - Read timeout in seconds (default: 0.1)

## Supported Commands

### IEEE 488.2 Common Commands

| Command | Response | Description |
|---------|----------|-------------|
| `*IDN?` | `Mock Instruments Inc.,SCPI-SIM-1000,SIM123456,1.0.0` | Identification query |
| `*RST` | (none) | Reset instrument |
| `*CLS` | (none) | Clear status |
| `*ESR?` | `0` | Event Status Register query |
| `*STB?` | `0` | Status Byte query |
| `*OPC?` | `1` | Operation Complete query |
| `*OPC` | (none) | Operation Complete command |

### System Commands

| Command | Response | Description |
|---------|----------|-------------|
| `SYST:ERR?` | `0,No error` or `<code>,<message>` | Error queue query |

### Measurement Commands

| Command | Response | Description |
|---------|----------|-------------|
| `MEAS:VOLT?` | `5.0234` | Voltage measurement (simulated ~5V ±0.1V) |
| `MEAS:CURR?` | `0.50123` | Current measurement (simulated ~0.5A ±0.01A) |
| `MEAS:TEMP?` | `25.34` | Temperature measurement (simulated ~25°C ±0.5°C) |

## Virtual Serial Port Setup

To test the SCPI-Serial adapter without physical hardware, you need to create a virtual serial port pair. One end connects to the mock instrument, the other to the VXI-11 proxy.

### Windows (com0com)

1. **Download and install com0com**
   - Get it from: https://com0com.sourceforge.net/
   - Or use the Null-modem emulator (com0com) fork: https://github.com/Raggles/com0com

2. **Create a virtual port pair**
   - Run the Setup Command Prompt from the Start menu
   - Create a pair (e.g., COM10 ↔ COM11):
     ```
     install PortName=COM10 PortName=COM11
     ```

3. **Run the mock instrument on one port**
   ```powershell
   python tools/mock_scpi_instrument.py COM10 --baudrate 115200
   ```

4. **Configure VXI-11 proxy to use the other port**
   - Edit `config.yaml`:
     ```yaml
     devices:
       mock_dmm:
         type: scpi-serial
         port: COM11
         baudrate: 115200
         timeout: 1.0
         write_termination: "\n"
         read_termination: "\n"
     ```

5. **Start the proxy and connect**
   ```powershell
   python scripts/start_server_with_terminal.py
   ```

### Linux (socat)

1. **Install socat** (if not already installed)
   ```bash
   # Debian/Ubuntu
   sudo apt-get install socat
   
   # Fedora/RHEL
   sudo dnf install socat
   
   # Arch Linux
   sudo pacman -S socat
   ```

2. **Create a virtual serial port pair**
   ```bash
   socat -d -d pty,raw,echo=0 pty,raw,echo=0
   ```
   
   This will output something like:
   ```
   2024/10/22 10:30:00 socat[12345] N PTY is /dev/pts/3
   2024/10/22 10:30:00 socat[12345] N PTY is /dev/pts/4
   ```

3. **Run the mock instrument on one PTY**
   ```bash
   python tools/mock_scpi_instrument.py /dev/pts/3 --baudrate 115200
   ```

4. **Configure VXI-11 proxy to use the other PTY**
   - Edit `config.yaml`:
     ```yaml
     devices:
       mock_dmm:
         type: scpi-serial
         port: /dev/pts/4
         baudrate: 115200
         timeout: 1.0
         write_termination: "\n"
         read_termination: "\n"
     ```

5. **Start the proxy and connect**
   ```bash
   python scripts/start_server_with_terminal.py
   ```

### Alternative: Using Physical Loopback

If you have a USB-to-Serial adapter with TX and RX pins accessible, you can create a physical loopback by connecting TX to RX. However, this only works for echo-based testing and won't provide SCPI command responses.

## Testing with the VXI-11 Terminal

Once both the mock instrument and VXI-11 proxy are running:

```
> connect localhost mock_dmm
Connected to mock_dmm via link 1

> lock
Lock acquired.

> *IDN?
Mock Instruments Inc.,SCPI-SIM-1000,SIM123456,1.0.0

> MEAS:VOLT?
5.0234

> MEAS:CURR?
0.50123

> *RST

> unlock
Lock released.

> quit
```

## Example Output

```
$ python tools/mock_scpi_instrument.py COM10 --baudrate 115200

Opening serial port COM10 at 115200 baud...
✓ Serial port COM10 opened successfully
  Settings: 115200 8N1
  Timeout: 0.1s

============================================================
Mock SCPI Instrument Running
============================================================
Manufacturer: Mock Instruments Inc.
Model:        SCPI-SIM-1000
Serial:       SIM123456
Firmware:     1.0.0
============================================================
Waiting for commands... (Press Ctrl+C to stop)

[10:30:15] RX: *IDN?
[10:30:15] TX: Mock Instruments Inc.,SCPI-SIM-1000,SIM123456,1.0.0
[10:30:18] RX: MEAS:VOLT?
[10:30:18] TX: 5.0234
[10:30:20] RX: *RST
[10:30:20] (no response)
```

## Integration Testing

Use this mock instrument to:

1. **Validate the SCPI-Serial adapter** - Ensure commands are correctly transmitted and responses received
2. **Test the VXI-11 server** - Verify the entire stack (VXI-11 → adapter → serial) works end-to-end
3. **Develop without hardware** - Build and test the proxy before connecting to real instruments
4. **Automated testing** - Spawn the mock instrument in test scripts for CI/CD pipelines

## Troubleshooting

### Port Access Issues (Linux)

If you get a permission error:
```bash
# Add your user to the dialout group
sudo usermod -a -G dialout $USER

# Log out and log back in for changes to take effect
```

### Port Already in Use

Make sure no other program is using the serial port:
```powershell
# Windows - list COM ports
mode
```

```bash
# Linux - list serial ports and processes
lsof /dev/ttyUSB0
```

### No Response from Mock Instrument

- Verify both sides use the same baud rate and line termination settings
- Check that the virtual port pair is correctly created
- Ensure the mock instrument is running before starting the proxy
- Try increasing the timeout values on both ends

## Extending the Mock Instrument

To add custom commands:

1. Edit `tools/mock_scpi_instrument.py`
2. Add new command handling in the `handle_command()` method:

```python
elif cmd == "CONF:VOLT?":
    return "10.0,0.001"  # Range and resolution

elif cmd.startswith("CONF:VOLT "):
    # Parse parameters and configure
    return None  # No response for write commands
```

3. Restart the mock instrument
