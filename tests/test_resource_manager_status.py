import asyncio

from vxi_proxy.resource_manager import ResourceManager


def test_resource_manager_status_is_dict() -> None:
    mgr = ResourceManager()
    # status() is an async coroutine that returns a mapping
    status = asyncio.run(mgr.status())
    assert isinstance(status, dict)
    # Initially the manager should have no owners recorded
    assert status == {}
