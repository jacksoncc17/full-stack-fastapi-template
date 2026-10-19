"""What a heartbeat reports, and what it must not touch.

The split this pins down is capabilities (declared once, at registration: "this node has
katago") versus status (reported on every beat: "processed 3 jobs"). Both used to be the
`capabilities` column, so a heartbeat silently replaced what an admin had declared.
"""

import uuid

from fastapi.testclient import TestClient

from app.core.config import settings


def _name() -> str:
    return f"status-test-{uuid.uuid4().hex[:8]}"


def _register(
    client: TestClient, headers: dict[str, str], name: str,
    capabilities: dict | None = None,
) -> dict:
    body: dict = {"name": name}
    if capabilities is not None:
        body["capabilities"] = capabilities
    r = client.post(f"{settings.API_V1_STR}/admin/workers/", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _beat(client: TestClient, key: str, body: dict | None) -> None:
    r = client.post(
        f"{settings.API_V1_STR}/worker/heartbeat",
        headers={"X-Worker-Key": key},
        json=body,
    )
    assert r.status_code == 200, r.text


def _shown(client: TestClient, headers: dict[str, str], name: str) -> dict:
    """The row as the admin page sees it: the detail list, capabilities and status."""
    r = client.get(f"{settings.API_V1_STR}/admin/workers/list", headers=headers)
    assert r.status_code == 200, r.text
    rows = {w["name"]: w for w in r.json()["data"]}
    assert name in rows, sorted(rows)
    return rows[name]


def test_a_heartbeat_reports_status_and_leaves_capabilities_alone(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    name = _name()
    worker = _register(
        client, superuser_token_headers, name, capabilities={"has_katago": True}
    )

    _beat(client, worker["api_key"], {"status": {"processed": 2}})

    row = _shown(client, superuser_token_headers, name)
    assert row["status"] == {"processed": 2}
    # the declaration, not the report -- this is the assertion the split exists for
    assert row["capabilities"] == {"has_katago": True}


def test_an_old_worker_posting_capabilities_is_read_as_status(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A worker built before the split still posts its report as `capabilities`."""
    name = _name()
    worker = _register(client, superuser_token_headers, name)

    _beat(client, worker["api_key"], {"capabilities": {"processed": 1}})

    row = _shown(client, superuser_token_headers, name)
    assert row["status"] == {"processed": 1}
    assert row["capabilities"] is None


def test_a_heartbeat_replaces_the_whole_report(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    name = _name()
    worker = _register(client, superuser_token_headers, name)

    _beat(client, worker["api_key"], {"status": {"processed": 1, "fake": False}})
    _beat(client, worker["api_key"], {"status": {"processed": 2}})

    row = _shown(client, superuser_token_headers, name)
    assert row["status"] == {"processed": 2}


def test_a_heartbeat_without_a_report_keeps_the_last_one(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A bare heartbeat is a liveness ping; it must not erase what the worker last said."""
    name = _name()
    worker = _register(client, superuser_token_headers, name)

    _beat(client, worker["api_key"], {"status": {"processed": 7}})
    _beat(client, worker["api_key"], None)

    row = _shown(client, superuser_token_headers, name)
    assert row["status"] == {"processed": 7}
    assert row["seconds_since_heartbeat"] is not None
