"""Unit tests for sdcm.cloud.cluster_utils module."""

import time
from unittest.mock import MagicMock

import pytest

from sdcm.cloud.api_client import ScyllaCloudAPIClient
from sdcm.cloud.cluster_utils import CloudClusterUtils, ScaleOutFailedException

ACCOUNT_ID = 100
CLUSTER_ID = 200


@pytest.fixture
def api_client():
    """Cloud API client mock, so no request reaches Scylla Cloud."""
    return MagicMock(spec=ScyllaCloudAPIClient)


@pytest.fixture
def cluster_utils(api_client):
    """CloudClusterUtils bound to a test cluster and the mocked API client."""
    return CloudClusterUtils(api_client=api_client, account_id=ACCOUNT_ID, cluster_id=CLUSTER_ID)


@pytest.fixture
def no_sleep(monkeypatch):
    """Record sleep calls instead of sleeping, so polling and retries run instantly."""
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return sleeps


# --- node and storage info ---


def test_get_active_cluster_node_ids_skips_deleted_nodes(cluster_utils, api_client):
    """Nodes that are deleted or being deleted must not be reported as active, whatever the status case."""
    api_client.get_cluster_nodes.return_value = [
        {"id": 1, "status": "ACTIVE"},
        {"id": 2, "status": "deleted"},
        {"id": 3, "status": "PENDING_DELETE"},
        {"id": 4, "status": "CREATING"},
    ]

    assert cluster_utils.get_active_cluster_node_ids() == {1, 4}
    api_client.get_cluster_nodes.assert_called_once_with(account_id=ACCOUNT_ID, cluster_id=CLUSTER_ID)


def test_get_cluster_total_storage_counts_only_db_nodes(cluster_utils, api_client):
    """Total storage must count only DB nodes; non-DB nodes (e.g. VS, Manager) carry a nodeType field."""
    api_client.get_cluster_details.return_value = {
        "instance": {"totalStorage": 500},
        "nodes": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4, "nodeType": "VECTOR_SEARCH"}],
    }

    assert cluster_utils.get_cluster_total_storage() == 1500
    api_client.get_cluster_details.assert_called_once_with(account_id=ACCOUNT_ID, cluster_id=CLUSTER_ID, enriched=True)


def test_get_cluster_used_storage_returns_own_cluster_in_gb(cluster_utils, api_client):
    """Used storage must be taken from the bound cluster only and converted from bytes to GB."""
    api_client.get_clusters.return_value = [
        {"id": 999, "metrics": {"STORAGE_USED": 7 * 1024**3}},
        {"id": CLUSTER_ID, "metrics": {"STORAGE_USED": 3 * 1024**3}},
    ]

    assert cluster_utils.get_cluster_used_storage() == 3
    api_client.get_clusters.assert_called_once_with(account_id=ACCOUNT_ID, metrics="STORAGE_USED")


def test_wait_for_cluster_disk_utilization_polls_until_target_reached(cluster_utils, api_client, no_sleep):
    """The wait must keep polling used storage until utilization reaches the target percentage."""
    api_client.get_cluster_details.return_value = {"instance": {"totalStorage": 100}, "nodes": [{"id": 1}]}
    api_client.get_clusters.side_effect = [
        [{"id": CLUSTER_ID, "metrics": {"STORAGE_USED": used_gb * 1024**3}}] for used_gb in (10, 69, 71)
    ]

    check_interval = 5
    cluster_utils.wait_for_cluster_disk_utilization(target_utilization=70, check_interval=check_interval)

    assert api_client.get_clusters.call_count == 3
    assert no_sleep == [check_interval] * 2


# --- cluster resize ---


@pytest.mark.parametrize(
    "requests_sequence",
    [
        pytest.param([[{"id": 7, "requestType": "RESIZE_CLUSTER_V3", "status": "IN_PROGRESS"}]], id="found-at-once"),
        pytest.param(
            [
                [],
                [{"id": 6, "requestType": "CREATE_CLUSTER", "status": "IN_PROGRESS"}],
                [{"id": 7, "requestType": "RESIZE_CLUSTER_V3", "status": "IN_PROGRESS"}],
            ],
            id="found-after-retries",
        ),
    ],
)
def test_wait_for_resize_request_returns_in_progress_resize_id(cluster_utils, api_client, no_sleep, requests_sequence):
    """The wait must return the ID of the in-progress resize request, retrying until it appears."""
    api_client.get_cluster_requests.side_effect = requests_sequence

    assert cluster_utils.wait_for_resize_request() == 7
    assert api_client.get_cluster_requests.call_count == len(requests_sequence)


def test_wait_for_cluster_scale_out_retries_until_completed(cluster_utils, api_client, no_sleep):
    """The wait must keep polling the resize request until it is COMPLETED."""
    api_client.get_cluster_request_details.side_effect = [
        {"status": "IN_PROGRESS"},
        {"status": "IN_PROGRESS"},
        {"status": "COMPLETED"},
    ]

    cluster_utils.wait_for_cluster_scale_out(request_id=7)

    assert api_client.get_cluster_request_details.call_count == 3
    api_client.get_cluster_request_details.assert_called_with(account_id=ACCOUNT_ID, request_id=7)


@pytest.mark.parametrize("status", [pytest.param("FAILED", id="failed"), pytest.param("CANCELLED", id="cancelled")])
def test_wait_for_cluster_scale_out_failed_request_raises_immediately(cluster_utils, api_client, no_sleep, status):
    """A failed or cancelled resize must fail the wait at once instead of polling until the retry limit."""
    api_client.get_cluster_request_details.return_value = {"status": status}

    with pytest.raises(ScaleOutFailedException):
        cluster_utils.wait_for_cluster_scale_out(request_id=7)

    assert api_client.get_cluster_request_details.call_count == 1
