import asyncio


def test_modbus_tcp_static_response_short_circuit() -> None:
    from vxi_proxy.adapters.modbus_tcp import ModbusTcpAdapter

    mappings = [
        {"pattern": r"^PING$", "response": "PONG"},
    ]

    adapter = ModbusTcpAdapter("test-modbus", host="127.0.0.1", port=502, mappings=mappings)

    written = asyncio.run(adapter.write(b"PING"))
    assert written == 4

    out = asyncio.run(adapter.read(1024))
    assert out == b"PONG"
