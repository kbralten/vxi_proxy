import asyncio


def test_generic_regex_static_response_short_circuit() -> None:
    from vxi_proxy.adapters.generic_regex import GenericRegexAdapter

    mappings = [
        {"pattern": r"^PING$", "response": "PONG"},
    ]

    adapter = GenericRegexAdapter(
        "test-generic",
        transport="tcp",
        host="127.0.0.1",
        port=502,
        mappings=mappings,
    )

    # write should return number of bytes accepted and not attempt network I/O
    written = asyncio.run(adapter.write(b"PING"))
    assert written == 4

    # read should return the static response
    out = asyncio.run(adapter.read(1024))
    assert out == b"PONG"
