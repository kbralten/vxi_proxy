Architecting a Configurable VXI-11 Protocol Gateway for SCPI and MODBUS Instruments
===================================================================================

Executive Summary
-----------------

This report presents a comprehensive architectural design for a VXI-11 to MODBUS and SCPI protocol proxy. The primary objective is to create a single, unified VXI-11 network endpoint that functions as a sophisticated gateway, providing transparent access to a heterogeneous collection of backend instruments that natively communicate using either SCPI or MODBUS protocols. The architecture is founded on the principles of the Gateway and Adapter design patterns, which facilitate robust protocol translation, abstract the complexity of backend devices, and enable a highly flexible, configuration-driven operational model.

Key features of this design include a fully compliant VXI-11 server façade that correctly handles the ONC-RPC based protocol, a modular backend adapter layer for seamless integration with different physical and transport layers, and a powerful command-mapping engine. This engine is capable of translating high-level, string-based commands into specific MODBUS register operations. The design emphasizes a concurrent and reliable execution model, utilizing modern asynchronous I/O to ensure high performance and scalability. This document serves as a complete engineering blueprint, detailing the system's logical components, data flows, configuration schema, and a recommended implementation strategy.

Section 1: Architectural Blueprint: The Protocol Gateway Pattern
----------------------------------------------------------------

### 1.1 Core Problem Statement: Protocol and Abstraction Mismatch

The central challenge addressed by this design is the integration of disparate instrument control protocols under a single, cohesive interface. Modern test and measurement environments frequently employ a mix of devices. High-level instruments often use SCPI, an ASCII-based, hierarchical command language layered on top of IEEE 488.2, which is well-suited for complex measurement tasks. In contrast, industrial sensors, controllers, and simpler devices commonly use MODBUS, a protocol that provides direct, low-level access to binary registers and coils.

This creates a significant "semantic impedance mismatch." VXI-11, the target client-facing protocol, is session-oriented and designed to carry ASCII-based messages, closely mirroring the GPIB/SCPI paradigm. MODBUS, however, is transactional and register-oriented, dealing with binary data packets and function codes. A client application built for VXI-11 cannot directly communicate with a MODBUS device without an intermediary that can bridge this fundamental gap in protocol structure, data representation, and operational abstraction.

### 1.2 The Gateway Pattern as the Architectural Solution

The API Gateway design pattern provides the canonical architectural solution for this class of problem. By implementing the proxy as a gateway, the system establishes a single, well-defined entry point for all VXI-11 clients. This gateway encapsulates the complexity of the internal system, including the variety of backend protocols, connection details, and data formats, presenting a simplified and unified view to the client.

The gateway architecture effectively decouples the VXI-11 clients from the backend devices. Clients interact with a consistent VXI-11 server, unaware of whether the target instrument is a SCPI device on a TCP socket or a MODBUS RTU device on a shared RS-485 bus. The gateway assumes the full responsibility for protocol translation, request routing, and response aggregation.

This architectural choice is not merely a matter of convenience; it is a strategic decision that ensures future extensibility. The design is based on the Hexagonal Architecture (also known as Ports and Adapters), where a stable application core interacts with the outside world through well-defined interfaces ("ports"). The protocol-specific logic is encapsulated in interchangeable "adapters." This structure inherently supports the addition of new backend protocols---such as OPC-UA or MQTT---by simply developing a new adapter module, without requiring any modifications to the VXI-11 server façade or the core routing engine. The system is therefore not just a VXI-11-to-X translator, but a generic and future-proof VXI-11-to-anything translation framework.

### 1.3 System Components and Data Flow

The gateway is composed of three primary logical components:

-   **VXI-11 Server Façade:** The public-facing interface responsible for implementing the VXI-11 protocol. It listens for incoming connections, terminates the ONC-RPC sessions, and handles all VXI-11 Core Channel procedures.

-   **Core Engine:** The central processing unit of the gateway. It manages application state, including active links and device locks. It contains the routing logic to map VXI-11 instrument names to backend device adapters and orchestrates the command translation process.

-   **Device Adapter Layer:** A collection of modules, each implementing a common interface but tailored to a specific backend protocol (e.g., `SCPI-TCP Adapter`, `MODBUS-RTU Adapter`).

A typical request lifecycle demonstrates the interaction between these components:

1.  A VXI-11 client application initiates a connection and sends a `create_link` RPC for a logical instrument named "temp_sensor_1".

2.  The VXI-11 Server Façade receives and decodes the RPC request.

3.  The Core Engine receives the request and consults its configuration, mapping the name "temp_sensor_1" to a physical device definition (e.g., a MODBUS RTU device at slave address 3 on `/dev/ttyS0`).

4.  The Core Engine selects the appropriate `MODBUS-RTU Adapter` and instructs it to prepare for communication with the specified device.

5.  The gateway generates a unique link identifier (`lid`) and returns it to the client via the VXI-11 Façade.

6.  The client subsequently sends a `device_write` RPC using the `lid`, containing the ASCII command "MEAS:TEMP?".

7.  The Core Engine uses the `lid` to route the command to the previously associated `MODBUS-RTU Adapter`.

8.  The adapter invokes its internal mapping logic, which translates the string "MEAS:TEMP?" into a specific MODBUS request PDU (e.g., Function Code 03, Read Holding Registers, starting at address 100).

9.  The adapter executes the MODBUS transaction over the serial port, receiving a binary response.

10. The adapter parses the binary response, converts the register data into an ASCII string representation (e.g., "25.3"), and places it in an output buffer.

11. When the client issues a `device_read` RPC, the adapter provides this buffered string, which is then passed back through the VXI-11 Façade to the client.

Section 2: The VXI-11 Server Façade
-----------------------------------

### 2.1 The ONC-RPC Foundation

A correct implementation of the VXI-11 server must be built upon the Open Network Computing (ONC) Remote Procedure Call (RPC) standard, as VXI-11 is not a simple raw socket protocol. VXI-11 defines three distinct RPC programs, each identified by a unique number and operating on a separate "channel".

-   `DEVICE_CORE`: Program number `0x0607AF`, for primary instrument communication.

-   `DEVICE_ASYNC`: Program number `0x0607B0`, for handling asynchronous events like service requests.

-   `DEVICE_INTR`: Program number `0x0607B1`, for the interrupt channel.

This gateway design prioritizes the implementation of the `DEVICE_CORE` program, as it contains all the procedures necessary for command-and-response style instrument control.

### 2.2 Connection Establishment and Discovery

For compatibility with standard client libraries like NI-VISA, the gateway must correctly implement the VXI-11 discovery and connection handshake process. This process relies on the RPC Portmapper service, a standard component of ONC-RPC that typically listens on both TCP and UDP port 111.

The gateway must therefore implement listeners on port 111. When a client wishes to connect, it will first query the Portmapper to resolve the program number for the VXI-11 Core Channel (`0x0607AF`) into the specific TCP port number on which the gateway's main service is listening. This allows the core service to be bound to a dynamic port while still being discoverable at a well-known address.

### 2.3 Core Channel RPC Implementation

The server façade must provide robust implementations for the essential `DEVICE_CORE` RPC procedures.

-   **`create_link`**: This procedure establishes a logical communication pathway to an instrument. It receives parameters including a `clientId` and a `device` name string (e.g., "dmm_main"). The gateway's core logic uses this `device` name as a key to look up the corresponding physical device in its configuration file. If the device is found and available, the gateway instantiates the appropriate adapter, creates an internal state object to represent the link, generates a unique `lid` (link identifier), and returns it to the client in a `Create_LinkResp` structure. The `device` name string is the primary abstraction key. While the VXI-11.2 specification defines a rigid format like "gpib0,4" for GPIB gateways , this proxy leverages the flexibility of the string parameter. The configuration can map descriptive, human-readable names like "rack1_oven_sensor" or "production_line_dmm" to physical devices. This transforms the proxy from a simple translator into a virtual instrument rack, where a logical inventory is directly addressable via VXI-11.

-   **`device_write`**: This procedure sends a command to the instrument. It receives the `lid` created by `create_link`, along with `io_timeout`, `flags`, and the `data` payload, which is the ASCII command string. The gateway uses the `lid` to identify the target device and its associated adapter, then passes the command string to the adapter for translation and execution.

-   **`device_read`**: This procedure retrieves a response from the instrument. It receives the `lid`, a `request_size`, and other parameters. The gateway uses the `lid` to query the associated adapter for any pending response data in its output buffer. This data is then formatted into a `Device_ReadResp` structure and sent back to the client.

-   **`device_lock` / `device_unlock`**: These procedures are critical for managing concurrent access in a multi-client environment. Upon receiving a `device_lock` call, the gateway will interact with a central resource manager to acquire an exclusive lock on the underlying physical device associated with the `lid`. This prevents other links from interacting with the device until a `device_unlock` call is received. The implementation must respect the `lock_timeout` parameter.

-   **`destroy_link`**: This procedure terminates a logical link. It receives a `lid`, and the gateway proceeds to tear down the internal state associated with that link, implicitly releasing any locks it holds and freeing the associated adapter resources.

The following table summarizes the mapping of VXI-11 RPCs to the gateway's internal actions.

**Table 1: VXI-11 Core Channel RPCs in the Gateway Context**

| **RPC Name** | **Key Parameters** | **Gateway's Internal Action** | **Expected Response** |
| --- | --- | --- | --- |
| `create_link` | `device` (string) | Looks up `device` name in configuration. Instantiates and prepares the corresponding backend adapter. Creates an internal link state object. | Returns a unique `lid` (link ID) and device parameters like `maxRecvSize`. |
| `device_write` | `lid`, `data` (bytes) | Uses `lid` to find the active link and its adapter. Passes the `data` string to the adapter for translation and transmission to the physical device. | Returns an error code indicating if the data was accepted for transmission. |
| `device_read` | `lid`, `request_size` | Uses `lid` to find the active link. Retrieves formatted ASCII response data from the adapter's output buffer. | Returns the response data and a reason code (e.g., indicating end of message). |
| `device_lock` | `lid`, `lock_timeout` | Uses `lid` to identify the underlying physical device. Atomically acquires an exclusive lock on that device via the central resource manager. | Returns an error code (e.g., success, or "device locked" if timeout expires). |
| `device_unlock` | `lid` | Uses `lid` to identify the underlying physical device. Releases the exclusive lock via the resource manager. | Returns an error code indicating success. |
| `destroy_link` | `lid` | Releases any locks held by the link. Tears down the internal link state object and associated resources. | Returns an error code indicating success. |

Section 3: Backend Integration: The Adapter Layer
-------------------------------------------------

### 3.1 The Adapter Design Pattern

To connect the Core Engine's generic, protocol-agnostic interface to the specific APIs of the backend devices, the design employs the Adapter pattern. A common `DeviceAdapter` abstract base class or interface will be defined, establishing a contract that all concrete adapters must fulfill. This interface will include methods such as `connect()`, `disconnect()`, `write(command_string)`, `read()`, `lock()`, and `unlock()`. This approach ensures that the Core Engine can manage any device type through a consistent set of operations.

### 3.2 SCPI Device Adapter

The SCPI adapter is the most straightforward to implement.

-   **Communication:** It will manage connections to SCPI instruments, typically over a standard TCP/IP socket, although serial port communication should also be supported.

-   **Command Handling:** For SCPI, the `write(command_string)` method is largely a pass-through function. It takes the ASCII command string received from the VXI-11 `device_write` call and sends it directly to the instrument's socket, appending a newline or other required termination character.

-   **Response Handling:** The `read()` method reads the ASCII-formatted response from the instrument until a termination character is received. This response is then buffered for the next VXI-11 `device_read` call.

-   **Error Management:** The adapter is responsible for catching TCP/IP connection errors and parsing standard SCPI error queue messages, translating them into a format the Core Engine can understand.
 
### 3.2.1 Adapter lifecycle and resource locking

Adapters that manage shared, physical resources (for example: serial ports, USBTMC devices, or other exclusive-access transports) must follow a small lifecycle contract so the gateway can safely arbitrate those resources across multiple VXI-11 links and clients.

#### Contract (recommended implementation):

- connect()/disconnect(): lightweight setup/teardown only. These should not open or hold the physical device. Use them to validate configuration and allocate in-memory structures.
- acquire(): open the physical resource and prepare the adapter for I/O. The server will call this immediately after the ResourceManager grants an exclusive lock for the device. acquire() may perform blocking I/O to open the device; if so, run that work in a thread (e.g., asyncio.to_thread) or ensure it is awaited in the server's async runtime. On failure, acquire() should raise so the server can undo the lock and report an error to the client.
- release(): close the physical resource and free any associated state. The server will call this when the ResourceManager releases the lock (device_unlock or implicit unlock on destroy_link). release() must be safe to call even if the resource is already closed.
- read()/write(): these operations assume the adapter currently holds the resource (acquire() has succeeded). If called without an open connection, they should return an error indicating no lock/connection.

#### Why this model?

- It lets create_link be a lightweight operation that does not touch hardware. Links can be established and enumerated even if the physical device is temporarily unavailable.
- It confines the window during which a device is open to the explicit lock period. This reduces contention and the risk of leaving ports open between uses.
- It matches the VXI-11 semantics: clients explicitly request exclusive access (device_lock), and that is the moment the gateway should open and hand over the device to the caller.

#### Server responsibilities and implementation notes:

- The Core Engine / VXI-11 server should call adapter.acquire() immediately after ResourceManager.lock succeeds for the device. If adapter.acquire() fails, the server should release the ResourceManager lock and return an appropriate VXI-11 error code to the client.
- The server should call adapter.release() when the ResourceManager releases the lock (device_unlock or when destroying a link that holds a lock). Because releasing hardware may involve blocking cleanup, run release() in the AsyncRuntime (or via asyncio.to_thread) so the RPC handler stays responsive.
- For adapters that use blocking libraries (for example, pyserial), perform I/O (open/read/write/close) on worker threads rather than blocking the event loop.

#### Testing and edge cases:

- Unit-test adapters using a fake or virtual device to verify acquire()/release() open/close semantics.
- Test behavior when acquire() raises (server must unlock and surface an error). Test that release() is idempotent.
- Consider timeouts: if acquire() takes too long, the server should enforce a sensible lock-acquisition timeout and report failure.

These guidelines should be followed by any adapter that controls exclusive-access hardware to avoid resource leaks and to make the gateway predictable under concurrent use.

### 3.3 MODBUS Device Adapters (RTU, ASCII, and TCP)

The MODBUS adapter is significantly more complex due to the fundamental protocol mismatch. It must handle multiple variants of the protocol.

-   **Protocol Variants:** The adapter must be capable of handling MODBUS TCP, MODBUS RTU, and MODBUS ASCII, as specified in the device's configuration. This requires it to conditionally use either a TCP socket or a serial port interface for communication.

-   **Framing, Encoding, and Error Checking:** The adapter must correctly construct the Application Data Unit (ADU) for each transaction, which differs significantly between variants :

    -   **MODBUS TCP:** Involves managing the 7-byte Modbus Application Protocol (MBAP) header. It relies on the underlying TCP/IP stack for error checking.

    -   **MODBUS RTU:** Transmits data in binary format, uses silent intervals on the serial line for message framing, and appends a 16-bit CRC (Cyclic Redundancy Check) for robust error detection.

    -   **MODBUS ASCII:** Encodes each byte of the message as two human-readable ASCII characters. Messages are explicitly framed with a leading colon (`:`) and a trailing carriage return/line feed (CR/LF). Error checking is performed using a simpler Longitudinal Redundancy Check (LRC). The adapter must implement both CRC and LRC calculation and validation logic.

-   **Command Translation:** The adapter's `write(command_string)` method is the site of the core translation logic. It will receive a high-level string like "MEAS:TEMP?" and must invoke the Command Mapping Engine (detailed in Section 4) to parse this string and translate it into a specific MODBUS Protocol Data Unit (PDU), such as a "Read Holding Registers" request.

-   **Data Type Handling:** A critical function of the adapter is managing the conversion between the ASCII string world of VXI-11/SCPI and the 16-bit register world of MODBUS. It must support multi-register data types, such as 32-bit floating-point numbers or signed integers, and correctly handle byte and word ordering (endianness), which can vary between devices.

-   **Response Translation:** The `read()` method will receive a response ADU. It must parse this response, validate it (including the checksum), extract the raw data from the registers, convert it to the appropriate data type (e.g., a floating-point number), and finally format it as a human-readable ASCII string to be returned to the VXI-11 client.

-   **Error Handling:** The adapter must recognize and handle MODBUS exception responses. When a MODBUS device returns an exception code (e.g., `0x01` for Illicit Function, `0x02` for Illicit Data Address), the adapter must catch this and translate it into a meaningful error that can be propagated back to the VXI-11 client.

A subtle but critical responsibility of the serial MODBUS adapters (RTU and ASCII) is serial port arbitration. An RS-485 bus is a shared medium where only one device can transmit at a time. If the gateway is configured to communicate with multiple MODBUS devices on the same physical serial port, the adapter layer must ensure that all requests to that port are serialized. This prevents multiple concurrent VXI-11 requests from causing collisions on the bus. The architecture must therefore include a shared `SerialPortManager` or a similar mutex mechanism that adapters use to request exclusive access to a physical serial port before initiating a transaction.

### 3.4 USBTMC Device Adapter

The USB Test & Measurement Class (USBTMC) is a modern protocol built on top of USB, designed to replace GPIB for direct PC-to-instrument connections. It mimics the message-based communication style of IEEE-488, making it a natural fit for SCPI-style commands.

-   **Communication:** The adapter will communicate with a locally connected USB instrument. This requires the gateway to have access to the host machine's USB subsystem. The adapter will identify the target device using its Vendor ID (VID) and Product ID (PID), and optionally a serial number for disambiguation. Communication occurs over USB Bulk-IN and Bulk-OUT endpoints.

-   **Command Handling:** Much like the SCPI-TCP adapter, the `write(command_string)` method will be a pass-through. It will take the ASCII command string from the VXI-11 `device_write` call and send it to the instrument's Bulk-OUT endpoint.

-   **Device Discovery and Permissions:** The adapter must be able to enumerate connected USB devices to find the one matching the configuration parameters. On systems like Linux, this will require appropriate permissions, often configured via `udev` rules, to allow the gateway process to access the raw USB device.

-   **Implementation:** This adapter will be built on top of a standard USB library, such as `libusb`, and a corresponding Python wrapper like `PyUSB`.

**Table 2: Common MODBUS Function Codes for Mapping**

| **Function Code (Hex)** | **Name** | **Action Type** | **Target Data Type** |
| --- | --- | --- | --- |
| `0x01` | Read Coils | Read | Single Bit (Coil) |
| `0x02` | Read Discrete Inputs | Read | Single Bit (Discrete Input) |
| `0x03` | Read Holding Registers | Read | 16-bit Word (Holding Register) |
| `0x04` | Read Input Registers | Read | 16-bit Word (Input Register) |
| `0x05` | Write Single Coil | Write | Single Bit (Coil) |
| `0x06` | Write Single Register | Write | 16-bit Word (Holding Register) |
| `0x0F` | Write Multiple Coils | Write | Multiple Bits (Coil) |
| `0x10` | Write Multiple Registers | Write | Multiple Words (Holding Register) |

Section 4: The Core Translation and Mapping Engine
--------------------------------------------------

### 4.1 Design Goals

The design of the core mapping engine is guided by three principles:

-   **Flexibility:** The engine's behavior must be entirely dictated by an external configuration file. No hard-coded logic for specific devices or commands should exist in the source code.

-   **Extensibility:** Adding new command patterns, data types, or even new translation logic should be possible by modifying the configuration, not the application code.

-   **Performance:** The parsing and mapping logic must be efficient to minimize the latency introduced by the gateway.

### 4.2 Device Mapping

The first stage of translation is device mapping. At startup, the gateway loads the configuration file and builds an internal registry (e.g., a hash map or dictionary) that maps the logical VXI-11 `device` names to their physical device definitions. Each definition contains the device type (`scpi-tcp`, `modbus-rtu`, etc.) and all necessary connection parameters (IP address, serial port, slave ID, etc.), which are then used to instantiate the correct adapter.

### 4.3 MODBUS Command Mapping and Parsing

This engine is the intellectual core of the gateway for MODBUS devices. It defines a new, high-level, SCPI-like Application Programming Interface (API) for devices that natively lack one. A simple MODBUS device only understands low-level commands like "read register 123". This engine allows a user to define a human-readable command like "GET:PUMP:PRESSURE" and map it to that low-level action. This capability transforms the proxy from a mere protocol translator into an **instrument virtualization and API enhancement platform**. A collection of simple, cryptically-interfaced MODBUS devices can be presented to the test automation framework as a suite of fully-featured, SCPI-compliant virtual instruments.

-   **Rule-Based Approach:** The mapping is implemented using an ordered list of rules defined in the configuration for each MODBUS device. When a command arrives, the engine iterates through this list and executes the first rule that matches.

-   **Rule Structure:** Each rule is a data structure containing:

    -   `pattern`: A regular expression or wildcard string used to match the incoming ASCII command from the VXI-11 client. This pattern can include capture groups to extract parameters from the command string (e.g., `SOUR:VOLT (\d+\.\d+)`).

    -   `action`: The corresponding MODBUS function to execute, using a name from Table 2 (e.g., `write_holding_registers`).

    -   `params`: A dictionary of parameters that define the MODBUS transaction:

        -   `address`: The starting coil or register address for the operation.

        -   `count`: The number of coils or registers to read or write.

        -   `data_type`: The format of the data in the registers (e.g., `uint16`, `float32_be`, `string`), which dictates how the adapter should encode writes and decode reads.

        -   `value`: For write operations, this field provides the value to be written. It can be a static value or a template that uses captured groups from the `pattern` regex (e.g., "$1" to use the first captured group).

Section 5: Concurrency, Performance, and Reliability
----------------------------------------------------

### 5.1 Concurrency Model

The gateway must be designed to handle multiple concurrent operations efficiently. It will receive simultaneous VXI-11 connections from multiple clients, and each client may attempt to communicate with different backend devices at the same time. A traditional thread-per-connection model can be inefficient, consuming significant memory and CPU resources for context switching, especially as the number of clients and devices grows.

An event-driven, asynchronous I/O model is the recommended approach. Using frameworks like Python's `asyncio` or Go's goroutines, the server can manage thousands of connections within a single thread or a small thread pool. I/O operations (like waiting for data on a socket or serial port) do not block the entire process. Instead, the event loop yields control, allowing the server to process other ready tasks. This model provides superior scalability and resource efficiency, and it prevents a single slow backend device from degrading the performance of the entire gateway.

### 5.2 Resource Management and Locking

As described in Section 2, the VXI-11 protocol includes explicit commands for locking and unlocking devices to ensure exclusive access. The gateway must implement this functionality robustly. A centralized, thread-safe `ResourceManager` module is required to manage the lock state of all physical backend devices.

The locking logic will proceed as follows:

1.  A `device_lock` RPC arrives for a specific `lid`.

2.  The Core Engine identifies the underlying physical device associated with that `lid`.

3.  The `ResourceManager` is called to acquire a lock on this physical device. It checks if the device is already locked by a *different* `lid`.

4.  If the device is available, the manager marks it as locked by the requesting `lid` and returns success.

5.  If the device is locked, the manager honors the `lock_timeout` parameter from the RPC call, waiting for the specified duration. If the lock is not released within this time, it returns a `Device locked by another link` error (VXI-11 error code 11).

6.  A `device_unlock` RPC for a given `lid` will cause the `ResourceManager` to release the lock.

7.  The destruction of a link (via `destroy_link` or client disconnect) must also trigger an implicit release of any locks held by that link to prevent orphaned locks.

### 5.3 Error Handling and Propagation

A comprehensive, multi-layered error handling strategy is essential for a reliable gateway.

-   **Backend Error Detection:** The device adapters are the first line of defense. They must be capable of detecting a wide range of errors, including physical layer issues (TCP disconnects, serial port failures), protocol-level errors (MODBUS CRC mismatch, invalid response length), and application-level errors from the device itself (MODBUS exception codes, SCPI error messages).

-   **Error Translation:** When an adapter catches a backend-specific error, it must translate it into a generic internal error code that the Core Engine can understand. For instance, a MODBUS "Illicit Data Address" exception and a SCPI "-222,Data out of range" error could both be mapped to an internal `INVALID_PARAMETER` error.

-   **VXI-11 Error Propagation:** The Core Engine receives these internal error codes and maps them to the standardized `Device_ErrorCode` enumeration defined in the VXI-11 specification. This ensures that the client application receives a consistent and standard error message (e.g., `Syntax error`, `Device not accessible`, `Parameter error`) regardless of the underlying cause or backend protocol. This abstraction of error states is a key feature of the gateway.

Section 6: Configuration Schema and Implementation
--------------------------------------------------

### 6.1 Choice of Format: YAML

YAML is the recommended format for the configuration file. Its syntax is more human-readable than JSON, it supports comments, and it can elegantly represent the hierarchical data structures required for defining devices and their complex command mappings. These features make it highly suitable for the target users, such as test and automation engineers, who will be responsible for maintaining the configuration.

### 6.2 Top-Level Structure

The `config.yaml` file will be organized into three top-level sections:

-   `server`: Contains global settings for the VXI-11 server itself, such as the listening IP address and port.

-   `devices`: A dictionary where each key is a logical VXI-11 instrument name, and the value is an object defining the physical device and its connection parameters.

-   `mappings`: A dictionary where each key is a logical instrument name, and the value is a list of command mapping rules for that device.

### 6.3 `devices` Section Schema

This section defines the inventory of physical instruments known to the gateway.

**Example for a SCPI instrument over TCP/IP:**

YAML

```
devices:
  dmm_main:
    type: scpi-tcp
    host: 192.168.1.100
    port: 5025

```

**Example for a USBTMC instrument:**

YAML

```
devices:
  scope_main:
    type: usbtmc
    vendor_id: 0x0957
    product_id: 0x1755
    serial_number: "MY12345678" # Optional, for disambiguation

```

**Example for a MODBUS RTU device on a serial port:**

YAML

```
devices:
  oven_ctrl:
    type: modbus-rtu
    port: /dev/ttyUSB0
    baudrate: 9600
    parity: N
    stopbits: 1
    slave_id: 5

```

**Example for a MODBUS ASCII device on a serial port:**

YAML

```
devices:
  temp_logger:
    type: modbus-ascii
    port: /dev/ttyS1
    baudrate: 19200
    parity: E
    stopbits: 1
    slave_id: 10

```

**Example for a MODBUS TCP device:**

YAML

```
devices:
  power_meter:
    type: modbus-tcp
    host: 192.168.1.101
    port: 502
    slave_id: 1

```

### 6.4 `mappings` Section Schema

This section defines the command translation logic for MODBUS devices.

**Example Mapping for the `oven_ctrl` MODBUS RTU device:**

YAML

```
mappings:
  oven_ctrl:
    # Rule to read the current temperature
    - pattern: "MEAS:TEMP?"
      action: read_input_registers
      params:
        address: 30001
        count: 2
        data_type: float32_be
    # Rule to set the temperature setpoint, capturing the float value
    - pattern: "SOUR:SETPT (\d+\.\d+)"
      action: write_holding_registers
      params:
        address: 40101
        data_type: float32_be
        value: "$1" # Use the first captured group from the regex
    # Rule to turn the output on
    - pattern: "OUTP:STAT ON"
      action: write_single_coil
      params:
        address: 1
        value: true
    # Rule to turn the output off
    - pattern: "OUTP:STAT OFF"
      action: write_single_coil
      params:
        address: 1
        value: false

```

This example illustrates the power of the mapping engine. It defines a simple, SCPI-like command set for a MODBUS device, including parameterized commands where the value is extracted directly from the incoming VXI-11 command string.

**Table 3: Comprehensive Configuration Parameters**

| **Parameter Path** | **Data Type** | **Required** | **Description** | **Example Value** |
| --- | --- | --- | --- | --- |
| `server.host` | string | No | IP address for the VXI-11 server to bind to. | `0.0.0.0` |
| `server.port` | integer | No | TCP port for the VXI-11 Core service. If 0, a dynamic port is used. | `1024` |
| `devices.<name>.type` | string | Yes | Device protocol type. One of `scpi-tcp`, `usbtmc`, `modbus-tcp`, `modbus-rtu`, `modbus-ascii`. | `modbus-ascii` |
| `devices.<name>.host` | string | If type is TCP | IP address or hostname of the device. | `192.168.1.50` |
| `devices.<name>.port` | string/integer | If type is TCP or RTU/ASCII | TCP port number or serial port path (e.g., `COM3`, `/dev/ttyUSB0`). | `/dev/ttyUSB0` |
| `devices.<name>.vendor_id` | hex integer | If type is USBTMC | The USB Vendor ID of the device. | `0x0957` |
| `devices.<name>.product_id` | hex integer | If type is USBTMC | The USB Product ID of the device. | `0x1755` |
| `devices.<name>.serial_number` | string | No (for USBTMC) | The USB serial number, used to select between multiple identical devices. | `MY12345678` |
| `devices.<name>.slave_id` | integer | If type is MODBUS | The MODBUS slave/unit ID (1-247). | `10` |
| `devices.<name>.baudrate` | integer | If type is RTU/ASCII | Serial port baud rate. | `9600` |
| `mappings.<name>` | list | For MODBUS | A list of command mapping rule objects for the named device. | `[...]` |
| `mappings.<name>.pattern` | string | Yes | Regular expression to match against the incoming command. | `"MEAS:VOLT\?"` |
| `mappings.<name>.action` | string | Yes | The MODBUS function to execute (e.g., `read_holding_registers`). | `read_holding_registers` |
| `mappings.<name>.params.address` | integer | Yes | The starting MODBUS register or coil address. | `40001` |
| `mappings.<name>.params.count` | integer | For read actions | The number of registers or coils to read. | `2` |
| `mappings.<name>.params.data_type` | string | For register actions | Data format (e.g., `uint16`, `int16`, `float32_be`, `float32_le`). | `float32_be` |
| `mappings.<name>.params.value` | any | For write actions | The value to write. Can be static or use regex captures (`$1`, `$2`). | `"$1"` |

Section 7: Configuration Graphical User Interface (GUI)
-------------------------------------------------------

### 7.1 Rationale and Design Philosophy

While the YAML configuration file offers maximum power and flexibility, direct text editing can be intimidating and error-prone for users not deeply familiar with YAML syntax. A Graphical User Interface (GUI) addresses this by providing a more accessible and guided configuration experience. The primary goals of the GUI are to lower the barrier to entry, reduce configuration errors through structured inputs and real-time validation, and streamline the management of a large inventory of devices and complex command mappings.

The design philosophy is to create a user-friendly, form-based interface that directly manipulates the underlying `config.yaml` file. This ensures that the YAML file remains the single source of truth, allowing power users to continue editing it directly if they choose. The GUI will be web-based, served by the proxy application itself, making it accessible from any modern browser on the network without requiring any client-side software installation.

### 7.2 Core Components and Workflow

The GUI will be structured around the main sections of the `config.yaml` file, presenting a clear and organized workflow for users.

-   **Device Management Panel:** This view will correspond to the `devices` section of the configuration.

    -   It will display a table listing all currently configured devices, showing their logical name, type, and connection details.

    -   An "Add Device" button will launch a form. This form will feature a "Device Type" dropdown menu (`scpi-tcp`, `modbus-rtu`, `modbus-tcp`).

    -   The form will dynamically adapt, showing only the relevant parameter fields for the selected device type. For instance, choosing `modbus-rtu` will display fields for `port`, `baudrate`, and `slave_id`, while hiding the `host` field.

    -   Each existing device in the table will have "Edit" and "Delete" options.

-   **MODBUS Mapping Editor:** This panel provides a specialized interface for managing the `mappings` section, which is the most complex part of the configuration.

    -   A primary dropdown menu will allow the user to select which MODBUS device to configure.

    -   Once a device is selected, a table will display all of its defined command mapping rules.

    -   An "Add/Edit Rule" form will guide the user through creating a mapping with validated inputs:

        -   **SCPI Command Pattern:** A text field for the command string (e.g., "MEAS:TEMP?"), with helpful tooltips explaining how to define capture groups with regular expressions for parameterized commands.

        -   **MODBUS Action:** A dropdown menu populated with supported MODBUS functions (e.g., "Read Holding Registers," "Write Single Coil").

        -   **Action Parameters:** A dynamic section of the form that changes based on the selected MODBUS action. For a "Read" action, it will prompt for a starting `address` and `count`. For a "Write" action, it will prompt for an `address` and a `value`.

        -   **Data Type:** For actions involving registers, a dropdown will provide choices for data interpretation (e.g., `uint16`, `int32_be`, `float32_le`).

-   **Configuration Control:**

    -   A persistent "Save Changes" button will write the current state of the GUI's configuration back to the `config.yaml` file on the server.

    -   Before saving, the backend will perform a full validation check. If any errors are detected (e.g., invalid IP address format, missing required field), the GUI will highlight the problematic fields and display clear error messages to the user.

    -   A "Reload Service" button will instruct the running proxy to re-read its configuration file, applying the new settings without requiring a full restart of the application.

### 7.3 Technical Implementation

The GUI will be implemented as a modern single-page web application (SPA), decoupled from the core proxy logic.

-   **Frontend:** The application will be built using a standard JavaScript framework such as React or Vue. This allows for the creation of a dynamic and responsive user interface.

-   **Backend API:** The proxy server will expose a minimal REST API for the frontend to consume.

    -   `GET /api/config`: The backend reads the `config.yaml` file, converts it to a JSON object, and sends it to the frontend to populate the forms.

    -   `POST /api/config`: The frontend submits the entire configuration as a JSON object. The backend validates the data, converts it back into the proper YAML format, and overwrites the `config.yaml` file.

This architecture ensures a clean separation of concerns, allowing the user interface to evolve independently of the core protocol translation engine.

Section 8: Implementation Strategy and Recommendations
------------------------------------------------------

### 8.1 Technology Stack Recommendation

-   Primary Recommendation: Python with asyncio

    The Python ecosystem is exceptionally well-suited for this project. Mature, open-source libraries exist for the key protocols, which can dramatically accelerate development. Specifically, python-vxi11-server provides a solid foundation for the VXI-11 façade, handling the complexities of ONC-RPC.49 Numerous robust MODBUS libraries are also available. Python's native asyncio framework is the ideal choice for implementing the recommended event-driven, asynchronous concurrency model, allowing for efficient handling of many concurrent network operations.

-   Alternative: Go (Golang)

    Go is another strong candidate, particularly if maximizing raw performance and minimizing memory footprint are the absolute highest priorities. Go's native concurrency model, based on goroutines and channels, is a perfect fit for this type of network server.50 Several high-quality MODBUS client libraries exist for Go.52 The primary drawback is the lack of a pre-built VXI-11 server library. The ONC-RPC and VXI-11 protocol layers would need to be implemented from scratch, which represents a significant and complex engineering effort compared to leveraging existing Python libraries.

### 8.2 Phased Development and Testing Roadmap

This roadmap outlines a detailed, milestone-based approach to developing the protocol gateway. It prioritizes core architectural components and incorporates continuous testing with purpose-built mock devices.

-   **Milestone 1: Core Architecture and VXI-11 Foundation**

    -   **Objective:** Establish a stable VXI-11 server skeleton with robust resource management.

    -   **Sub-tasks:**

        -   Implement the VXI-11 Server Façade using a library like `python-vxi11-server` to handle ONC-RPC communication.

        -   Implement the core RPCs: `create_link` and `destroy_link`.

        -   Develop the centralized `ResourceManager` module to handle the `device_lock` and `device_unlock` RPCs, ensuring thread-safe, exclusive access to physical device representations.

        -   Define the abstract `DeviceAdapter` base class that establishes the common interface for all future device adapters.

        -   Create a simple "loopback" adapter that echoes commands, allowing for end-to-end testing of the VXI-11 channel and locking mechanism.

        -   Develop a suite of automated tests using a VXI-11 client library (e.g., `pyvisa` or `python-vxi11`) to validate link creation, destruction, and concurrent lock/unlock scenarios against the loopback adapter.

-   **Milestone 2: VXI-11 Interactive Terminal (REPL)**

    -   **Objective:** Create an interactive command-line tool for direct communication with the VXI-11 server, facilitating rapid testing and debugging.

    -   **Sub-tasks:**

        -   Choose a suitable VXI-11 client library (e.g., `python-vxi11`) for the terminal application.

        -   Implement the core REPL (Read-Eval-Print Loop) to read user input, send it to the server, and print the response.

        -   Incorporate connection management commands within the REPL (e.g., `connect localhost my_device`) to handle `create_link` and `destroy_link` RPCs.

        -   Map standard input to `device_write` and `device_read` calls to allow for interactive command-and-response sessions.

        -   Provide clear, formatted output for both successful responses and any VXI-11 errors returned by the server.

        -   Test the REPL against the "loopback" adapter from Milestone 1 to confirm basic functionality and its utility as a debugging tool.

-   **Milestone 3: SCPI Serial Adapter and Integration**

    -   **Objective:** Support pass-through communication for SCPI commands over a standard RS-232 serial port.

    -   **Sub-tasks:**

        -   Develop the `SCPI-Serial` adapter, inheriting from the `DeviceAdapter` base class, using a library like `pyserial`.

        -   Implement `device_write` and `device_read` to handle serial communication, including configuration of port settings (baud rate, parity, etc.) and message termination characters.

        -   **Develop a mock SCPI serial instrument.** This can be implemented using a virtual serial port pair (e.g., using `socat` on Linux or `com0com` on Windows), with a Python script on the other end that listens for SCPI commands and provides valid responses.

        -   Write integration tests that connect to the gateway via VXI-11, target a configured mock SCPI serial device, and verify that commands are passed through and responses are returned correctly.

-   **Milestone 4: SCPI-TCP Adapter and Integration**

    -   **Objective:** Implement pass-through communication with a standard TCP-based SCPI instrument.

    -   **Sub-tasks:**

        -   Develop the `SCPI-TCP` adapter, inheriting from the `DeviceAdapter` base class.

        -   Implement the `device_write` and `device_read` methods to handle raw socket communication and message termination.

        -   **Develop a mock SCPI-TCP instrument server.** This can be a simple Python `asyncio` socket server that listens on a port and responds to basic SCPI commands like `*IDN?` and `*RST`.

        -   Write integration tests that connect to the gateway via VXI-11, target a configured mock SCPI device, and verify that commands are passed through and responses are returned correctly.

        -   Notes / defaults for `scpi-tcp` adapter:

          - `requires_lock`: defaults to `false` (TCP devices allow concurrent clients by default; can be overridden per-device in YAML).
          - `read_termination`: preserved in returned payload (same behavior as `scpi-serial`).
          - `reconnect_on_error`: defaults to `false` (adapter will raise AdapterError on socket errors; reconnect is explicit).
          - Example YAML entry:

            ```yaml
            devices:
              dmm_tcp:
              type: scpi-tcp
              host: 127.0.0.1
              port: 5555
              io_timeout: 1.0
              write_termination: "\n"
              read_termination: "\n"
            ```

          - Mock server script: `tools/mock_scpi_tcp_server.py` (asyncio); run for manual testing:

            ```powershell
            # start mock on default port 5555
            .\.venv\Scripts\python.exe -u tools/mock_scpi_tcp_server.py --host 127.0.0.1 --port 5555
            ```

-   **Milestone 5: USBTMC Adapter and Integration**

    -   **Objective:** Enable control of locally connected USBTMC instruments.

    -   **Sub-tasks:**

        -   Develop the `USBTMC` adapter using a library like `python-usbtmc` which is built on `PyUSB`.

        -   Implement device discovery logic to identify instruments by Vendor ID, Product ID, and optional serial number.

        -   Implement `device_write` and `device_read` to communicate over the instrument's USB Bulk-IN and Bulk-OUT endpoints.

        -   **Create a testing plan for the USBTMC adapter.** This involves either using a known physical USBTMC device (e.g., a modern oscilloscope) or developing a mock USBTMC device using a microcontroller like a Raspberry Pi Pico, which can be programmed to emulate a USBTMC device.

        -   Write integration tests to verify that the gateway can successfully proxy commands to a USBTMC device.

        -   Notes / defaults for `usbtmc` adapter:

          - `requires_lock`: defaults to `true` (USBTMC devices require exclusive access).
          - `read_termination`: preserved in returned payload (same behavior as `scpi-serial`).
          - Device discovery: uses `python-usbtmc.Instrument(idVendor, idProduct, iSerial)` to open by VID/PID/serial.
          - Example YAML entry:

            ```yaml
            devices:
              my_scope:
                type: usbtmc
                vid: 0x0957
                pid: 0x1755
                serial: "MY12345678"  # optional
                timeout: 1.0
                write_termination: "\n"
                read_termination: "\n"
            ```

          - System requirements:
            - Linux: libusb-1.0 installed; udev rules for device access (e.g., `/etc/udev/rules.d/99-usbtmc.rules`):
              ```
              # Example udev rule for Agilent/Keysight devices
              SUBSYSTEM=="usb", ATTR{idVendor}=="0957", MODE="0664", GROUP="plugdev"
              ```
            - Windows: WinUSB or libusbK driver (install via Zadig: https://zadig.akeo.ie/).
            - macOS: libusb via Homebrew (`brew install libusb`).

          - Integration test (gated by environment variables):

            ```powershell
            # Set environment variables for physical device
            $env:HAVE_USBTMC_DEVICE="1"
            $env:DEVICE_VID="0x0957"
            $env:DEVICE_PID="0x1755"
            $env:DEVICE_SERIAL="MY12345678"  # optional

            # Run integration test
            .\.venv\Scripts\python.exe -u -m unittest tests.integration.test_usbtmc_integration -v
            ```

-   **Milestone 6: MODBUS Core Logic and TCP Adapter**

    -   **Objective:** Implement the core translation engine and support for MODBUS TCP devices.

    -   **Sub-tasks:**

        -   Design and implement the rule-based Command Mapping Engine, including regular expression parsing and parameter extraction.

        -   Develop the `MODBUS-TCP` adapter, handling the MBAP header and PDU construction.

        -   **Develop a mock MODBUS TCP server.** Use a library such as `pymodbus` to create a virtual device with a configurable data store of coils and registers that the gateway can read from and write to.

        -   Write extensive unit tests for the mapping engine to validate various command patterns, data types, and value extractions.

        -   Write integration tests: configure a mock MODBUS TCP device with a set of command mappings, connect via VXI-11, send mapped commands, and verify that the correct MODBUS transactions occur and that the responses are translated back correctly.

-   **Milestone 7: MODBUS Serial Adapters (RTU & ASCII)**

    -   **Objective:** Add support for serial-based MODBUS devices and handle shared bus access.

    -   **Sub-tasks:**

        -   Develop the `MODBUS-RTU` adapter, including logic for 16-bit CRC calculation.

        -   Develop the `MODBUS-ASCII` adapter, including logic for LRC checksum calculation.

        -   Implement the `SerialPortManager` to provide a mutex-like mechanism for serializing access to a physical serial port, preventing bus collisions.

        -   **Extend the mock MODBUS server** to support RTU and ASCII protocols over a virtual serial port (e.g., using `socat` on Linux or a similar tool on other platforms).

        -   Write integration tests for both RTU and ASCII adapters, specifically including scenarios where multiple VXI-11 clients attempt to access different slave devices on the same shared serial port to validate the arbitration logic.

-   **Milestone 8: Hardening, Deployment, and Documentation**

    -   **Objective:** Prepare the application for production use.

    -   **Sub-tasks:**

        -   Integrate a structured logging framework to provide detailed operational visibility for debugging.

        -   Add a simple HTTP endpoint for health checks and basic monitoring metrics.

        -   Write comprehensive user documentation detailing the `config.yaml` schema, device setup, and command mapping rules.

### 8.3 Operational Considerations

-   **Logging:** The gateway should implement structured logging (e.g., in JSON format). Logs should capture every significant event, including VXI-11 client connections, link creation/destruction, each VXI-11 command received, the resulting backend transaction (e.g., the exact MODBUS PDU sent), the response received, and any errors encountered at any stage. This level of detail is invaluable for debugging configuration issues and diagnosing communication problems with backend devices.

-   **Monitoring:** To ensure operational visibility, the gateway should expose key performance indicators via an HTTP endpoint, suitable for scraping by a monitoring system like Prometheus. Metrics should include the number of active VXI-11 clients and links, the latency of backend device responses (per device), and counters for successful transactions and errors.