VXI-11 Protocol Gateway for SCPI, MODBUS, and USBTMC Instruments
================================================================

1\. Overview
------------

This project provides a powerful and configurable VXI-11 protocol gateway that creates a single, unified network endpoint for a diverse collection of backend test and measurement instruments. It acts as a sophisticated proxy, translating the VXI-11 protocol into the native protocols of various devices, including SCPI (over TCP), MODBUS (RTU, ASCII, and TCP), and USBTMC.

The primary goal is to abstract the complexity of a heterogeneous instrument environment. Client applications can connect to a single, stable VXI-11 server and communicate with any configured device using a consistent interface, regardless of the underlying protocol or physical connection.

2\. Key Features
----------------

-   **Unified VXI-11 Interface**: Provides a fully compliant VXI-11 server that handles all necessary ONC-RPC procedures (`create_link`, `device_write`, `device_read`, `device_lock`, etc.).

-   **Multi-Protocol Backend Support**: Seamlessly integrates with various instrument types through a modular adapter architecture.

    -   **SCPI**: For TCP/IP-based instruments.

    -   **MODBUS**: Supports MODBUS TCP, RTU, and ASCII variants for industrial sensors and controllers.

    -   **USBTMC**: For modern instruments connected directly via USB.

-   **Powerful Command Mapping**: A core translation engine allows you to create a high-level, SCPI-like command set for MODBUS devices. Map human-readable commands (e.g., `"MEAS:TEMP?"`) to specific register operations.

-   **Configuration-Driven**: All gateway behavior is defined in a single, human-readable `config.yaml` file. No hard-coded logic means you can add or change instruments without modifying the application code.

-   **Web-Based GUI**: An intuitive graphical user interface, served by the gateway itself, allows for easy, error-proof configuration of devices and command mappings through any web browser.

-   **High Performance & Concurrency**: Built on an asynchronous I/O model to handle numerous simultaneous client connections and device communications efficiently without blocking.

-   **Robust Resource Management**: Correctly implements VXI-11 device locking to manage concurrent access and ensure exclusive control in multi-client environments.

3\. How It Works
----------------

The gateway is built on the **Gateway** and **Adapter** design patterns.

1.  **VXI-11 Server Façade**: A client connects to this public-facing interface, sending a request to link to a logical instrument name (e.g., `"oven_ctrl"`).

2.  **Core Engine**: The engine receives the request, looks up `"oven_ctrl"` in its configuration, and determines it's a MODBUS RTU device on `/dev/ttyUSB0`.

3.  **Device Adapter**: The engine selects the appropriate **MODBUS-RTU Adapter** to handle communication.

4.  **Command Translation**: The client sends a VXI-11 `device_write` command with the string `"MEAS:TEMP?"`. The Core Engine routes this to the adapter, which uses the configured mapping rules to translate it into a MODBUS "Read Input Registers" request.

5.  **Execution**: The adapter sends the binary MODBUS command over the serial port, receives the binary response, parses it, converts the data to an ASCII string (e.g., `"25.3"`), and buffers it.

6.  **Response**: When the client issues a `device_read` command, the gateway returns the buffered ASCII string.

This architecture completely decouples the client from the backend device, providing a seamless translation layer.

4\. Configuration (`config.yaml`)
---------------------------------

The entire system is controlled by the `config.yaml` file, which is divided into three main sections: `server`, `devices`, and `mappings`.

### 4.1 `server` Section

This section contains global settings for the gateway.

```
server:
  host: 0.0.0.0  # IP address to listen on. 0.0.0.0 for all interfaces.
  port: 1024     # TCP port for the VXI-11 Core service. Use 0 for a dynamic port.

```

### 4.2 `devices` Section

This is an inventory of all physical instruments the gateway can control. The key for each entry is the logical VXI-11 name you will use to connect.

**SCPI over TCP Example:**

```
devices:
  dmm_main:
    type: scpi-tcp
    host: 192.168.1.100
    port: 5025

```

**USBTMC Example:**

```
devices:
  scope_main:
    type: usbtmc
    vendor_id: 0x0957
    product_id: 0x1755
    serial_number: "MY12345678" # Optional, for disambiguation

```

**MODBUS RTU (Serial) Example:**

```
devices:
  oven_ctrl:
    type: modbus-rtu
    port: /dev/ttyUSB0 # Use COM3 on Windows
    baudrate: 9600
    parity: N
    stopbits: 1
    slave_id: 5

```

**MODBUS TCP Example:**

```
devices:
  power_meter:
    type: modbus-tcp
    host: 192.168.1.101
    port: 502
    slave_id: 1

```

### 4.3 `mappings` Section

This section defines the command translation logic for your MODBUS devices, creating a user-friendly API for them.

**Example for the `oven_ctrl` MODBUS device:**

```
mappings:
  oven_ctrl:
    # Rule to read the current temperature from two input registers as a float
    - pattern: "MEAS:TEMP?"
      action: read_input_registers
      params:
        address: 30001
        count: 2
        data_type: float32_be # Big-endian 32-bit float

    # Rule to set the temperature, capturing a float value from the command
    - pattern: "SOUR:SETPT (\d+\.\d+)"
      action: write_holding_registers
      params:
        address: 40101
        data_type: float32_be
        value: "$1" # Use the first captured group from the regex

    # Rule to turn the output on by writing to a single coil
    - pattern: "OUTP:STAT ON"
      action: write_single_coil
      params:
        address: 1
        value: true

```

5\. Usage
---------

1.  **Installation**:

    ```
    # Clone the repository
    git clone <repository_url>
    cd vxi11-gateway

    # Install dependencies
    pip install -r requirements.txt

    ```

2.  **Configuration**:

    -   Edit the `config.yaml` file to define your server settings, devices, and command mappings.

3.  **Run the Gateway**:

    ```
    python gateway.py

    ```

4.  **Connect with a Client**:

    -   Use any standard VXI-11 compatible client library (like NI-VISA, PyVISA, etc.) to connect. The instrument address will be `TCPIP::<gateway_ip>::<logical_instrument_name>::INSTR`.

    -   For example, to connect to the `dmm_main` configured above on a gateway running at `192.168.1.200`, the VISA resource string would be `TCPIP::192.168.1.200::dmm_main::INSTR`.

5.  **Testing Without Hardware**:

    -   Use the included mock SCPI instrument simulator to test the gateway without physical instruments. See `tools/README.md` for detailed setup instructions with virtual serial port pairs (com0com on Windows, socat on Linux).

6\. Configuration GUI
---------------------

For easier management, the gateway hosts a web-based GUI. Simply navigate to `http://<gateway_ip>:<http_port>` in your browser.

The GUI allows you to:

-   View, add, edit, and delete devices from the configuration.

-   Use dynamic forms that adapt to the selected device type, showing only relevant fields.

-   Create and manage complex MODBUS command mappings with validated inputs.

-   Save changes and reload the gateway service with a single click.

8\. Docker image
-----------------

This repository includes a small Dockerfile and an entrypoint script that start the configuration GUI and (if available) the VXI-11 facade inside a container. The container is useful for running the gateway in an isolated environment or on CI.

Build the image:

```powershell
docker build -t vxi-proxy:gui .
```

Run the container (GUI + facade, ports published):

```powershell
docker run --rm -p 8080:8080 -p 1024:1024 vxi-proxy:gui
```

Mount your local config (recommended for development):

```powershell
docker run --rm -p 8080:8080 -p 1024:1024 \
  -v ${PWD}/config.yaml:/app/config.yaml \
  vxi-proxy:gui
```

Environment variables supported by the entrypoint:

- `CONFIG_PATH` — path to the YAML config inside the container (default `/app/config.yaml`).
- `GUI_HOST` / `GUI_PORT` — where the GUI binds inside the container (defaults `0.0.0.0:8080`).
- `DISABLE_FACADE` — set to `1` to run the GUI only and skip starting the facade.
- `SERVER_HOST_OVERRIDE` — entrypoint will set `server.host` to this value in the config before starting the facade (default `0.0.0.0`).
- `DISABLE_SERVER_HOST_OVERRIDE` — set to `1` to disable the automatic override of `server.host`.
- `PORTMAPPER_ENABLED` — set to `1` to start a tiny user-space rpcbind/portmapper on port 111 that answers GETPORT for VXI‑11 programs and returns the configured `server.port`.

Notes:

- The entrypoint tries to import `Vxi11ServerFacade` from `vxi_proxy.server`. If your project exposes the facade under a different name/path, either update the entrypoint or run the container with `DISABLE_FACADE=1` and start the facade by other means.
- The GUI is served from the container and will be reachable at `http://<host>:8080/` when you publish port 8080.
- If your facade listens on a different port than the one configured in `config.yaml`, publish that port when running the container (for example `-p 1024:1024`).
- If you enable the portmapper, publish 111/tcp and 111/udp as well: `-p 111:111 -p 111:111/udp`.

Minimal portmapper (optional)
-----------------------------

When `PORTMAPPER_ENABLED=1` or `server.portmapper_enabled: true` in `config.yaml`, the container starts a very small portmapper that implements PMAPPROC_NULL and PMAPPROC_GETPORT for the VXI‑11 programs (DEVICE_CORE/ASYNC/INTR) and returns the configured `server.port` for TCP.

Notes:
- This is not a full rpcbind replacement; it only answers GETPORT for VXI‑11.
- It listens on both TCP and UDP 111.

7\. License
-----------

This project is licensed under the MIT License. See the `LICENSE` file for details.