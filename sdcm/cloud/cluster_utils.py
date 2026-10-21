# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2026 ScyllaDB

import logging
import time

from sdcm.cloud.api_client import ScyllaCloudAPIClient
from sdcm.utils.decorators import retrying, Retry

LOGGER = logging.getLogger(__name__)


class ScaleOutFailedException(Exception):
    pass


class CloudClusterUtils:
    """Helpers around Scylla Cloud API calls bound to a single cluster."""

    def __init__(self, api_client: ScyllaCloudAPIClient, account_id: int, cluster_id: int):
        self.api_client = api_client
        self.account_id = account_id
        self.cluster_id = cluster_id

    def get_active_cluster_node_ids(self) -> set[int]:
        """Return IDs of active DB nodes."""
        nodes = self.api_client.get_cluster_nodes(account_id=self.account_id, cluster_id=self.cluster_id)
        return {node["id"] for node in nodes if node["status"].upper() not in ("DELETED", "PENDING_DELETE")}

    def get_cluster_total_storage(self) -> float:
        """Return total storage (GB) of the cluster DB nodes as reported by Cloud API."""
        cluster_info = self.api_client.get_cluster_details(
            account_id=self.account_id,
            cluster_id=self.cluster_id,
            enriched=True,
        )
        # DB nodes are missing nodeType field, thus, can be filtered out this way
        cluster_nodes = [node for node in cluster_info["nodes"] if not node.get("nodeType")]
        return cluster_info["instance"]["totalStorage"] * len(cluster_nodes)

    def get_cluster_used_storage(self) -> float:
        """Return used storage (GB) of the cluster as reported by Cloud API."""
        clusters = self.api_client.get_clusters(account_id=self.account_id, metrics="STORAGE_USED")
        cluster = next(cluster for cluster in clusters if cluster["id"] == self.cluster_id)
        return cluster["metrics"]["STORAGE_USED"] / (1024**3)  # Convert bytes to GB

    def wait_for_cluster_disk_utilization(
        self, target_utilization: int, check_interval: int = 30, wait_timeout: int = 3600
    ) -> None:
        """Wait until cluster disk utilization reaches the target percentage."""
        total_storage = self.get_cluster_total_storage()

        LOGGER.debug("Waiting for cluster disk utilization to reach %d%%...", target_utilization)
        start_time = time.time()
        while time.time() - start_time < wait_timeout:
            current_disk_utilization = (self.get_cluster_used_storage() / total_storage) * 100
            if current_disk_utilization >= target_utilization:
                LOGGER.info("Target disk utilization of %d%% reached", target_utilization)
                return

            LOGGER.debug("Current disk utilization: %.2f%%, waiting...", current_disk_utilization)
            time.sleep(check_interval)

        raise TimeoutError(f"Disk utilization did not reach {target_utilization}% within {wait_timeout}s")

    @retrying(n=20, sleep_time=60, allowed_exceptions=(Retry,))
    def wait_for_resize_request(self) -> int:
        requests = self.api_client.get_cluster_requests(account_id=self.account_id, cluster_id=self.cluster_id)
        resize_request = next((r for r in requests if r["requestType"] == "RESIZE_CLUSTER_V3"), None)
        if resize_request and resize_request["status"] == "IN_PROGRESS":
            return resize_request["id"]
        raise Retry("Resize request not found")

    @retrying(n=180, sleep_time=60, allowed_exceptions=(AssertionError,))
    def wait_for_cluster_scale_out(self, request_id: int) -> None:
        request = self.api_client.get_cluster_request_details(account_id=self.account_id, request_id=request_id)
        status = request["status"]
        if status in ("FAILED", "CANCELLED"):
            raise ScaleOutFailedException("Cluster scale out failed")
        assert status == "COMPLETED", f"Cluster resize is not completed yet. Current status: {status}"
