#!/usr/bin/env python

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

import random
import threading
import time
import uuid
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cassandra.cluster import Session

from cassandra.query import BatchStatement

from sdcm.cloud_api_client import ScyllaCloudAPIClient
from sdcm.rest.remote_curl_client import RemoteCurlClient
from sdcm.tester import ClusterTester
from sdcm.utils.decorators import retrying, Retry
from sdcm.utils.loader_utils import LoaderUtilsMixin


class ScaleOutFailedException(Exception):
    pass


class ScyllaCloudTestsMixin(ClusterTester, LoaderUtilsMixin):
    @cached_property
    def cloud_api_client(self):
        return ScyllaCloudAPIClient(
            api_url=self.params.cloud_env_credentials["base_url"],
            auth_token=self.params.cloud_env_credentials["api_token"],
        )

    @cached_property
    def account_id(self):
        return self.cloud_api_client.get_current_account_id()

    @cached_property
    def cluster_id(self):
        return self.cloud_api_client.get_cluster_id_by_name(
            account_id=self.account_id,
            cluster_name=self.db_cluster.name,
        )

    def get_email_data(self):
        self.log.info("Prepare data for email")
        email_data = self._get_common_email_data()
        return email_data


class ScyllaCloudVectorSearchXCloudTest(ScyllaCloudTestsMixin):
    KEYSPACE_NAME = "vector_search_ks"
    TABLE_NAME = "vector_search_table"
    INDEX_NAME = "vector_search_index"
    VECTOR_DIMENSION = 64
    VECTORS_PER_BATCH = 100

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.load_thread = None
        self.vector_thread = None
        self.vector_stop_event = None
        self.vector_validation_failures_count: int = 0

    @cached_property
    def vs_remote_curl_client(self) -> RemoteCurlClient:
        """Get RemoteCurlClient that runs curl on the first VS node against its local VS API."""
        vs_cluster = self.db_cluster.vector_store_cluster
        assert vs_cluster and vs_cluster.nodes, "Vector Store cluster has no nodes"
        vs_node = vs_cluster.nodes[0]
        port = self.params.get("vector_store_port")
        return RemoteCurlClient(host=f"localhost:{port}", endpoint="", node=vs_node)

    def load_cluster_to_scaling_threshold_in_background(self) -> threading.Thread:
        """Start loading cluster to scaling threshold in background."""

        def _run_load_in_background():
            stress_queue = []
            self.assemble_and_run_all_stress_cmd(
                stress_queue=stress_queue,
                stress_cmd=self.params.get("stress_cmd"),
                keyspace_num=1,
            )
            for queue in stress_queue:
                self.verify_stress_thread(queue)

            self.log.info("Write stress completed, starting background read stress")
            stress_read_cmd = self.params.get("stress_read_cmd")
            if stress_read_cmd:
                # Fire-and-forget: keep read load running in the background to stress the cluster.
                # We don't verify results here — any failures will surface as CassandraStressEvent
                self.assemble_and_run_all_stress_cmd(
                    stress_queue=[],
                    stress_cmd=stress_read_cmd,
                    keyspace_num=1,
                )

        load_thread = threading.Thread(target=_run_load_in_background)
        load_thread.start()
        self.log.info("Cluster loading to scaling threshold started in background thread")

        return load_thread

    def prepare_vs_keyspace(self) -> None:
        """Prepare vector search keyspace and table with index."""
        self.create_keyspace(keyspace_name=self.KEYSPACE_NAME, replication_factor=3)

        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            create_table_cql = f"""
                CREATE TABLE IF NOT EXISTS {self.KEYSPACE_NAME}.{self.TABLE_NAME} (
                    id uuid,
                    commenter text,
                    comment_vector vector<float, {self.VECTOR_DIMENSION}>,
                    created_at timestamp,
                    PRIMARY KEY (id, created_at)
                )
            """
            session.execute(create_table_cql)

            create_index_cql = f"""
                CREATE CUSTOM INDEX IF NOT EXISTS {self.INDEX_NAME}
                    ON {self.KEYSPACE_NAME}.{self.TABLE_NAME}(comment_vector)
                    USING 'vector_index'
                    WITH OPTIONS = {{'similarity_function': 'COSINE'}}
            """
            session.execute(create_index_cql)

        self.log.debug("Table %s.%s created successfully", self.KEYSPACE_NAME, self.TABLE_NAME)

    def wait_for_cluster_disk_utilization(
        self, target_utilization: int, check_interval: int = 30, wait_timeout: int = 3600
    ) -> None:
        """Wait until cluster disk utilization reaches the target percentage."""
        cluster_info = self.cloud_api_client.get_cluster_details(
            account_id=self.account_id,
            cluster_id=self.cluster_id,
            enriched=True,
        )
        # DB nodes are missing nodeType field, thus, can be filtered out this way
        cluster_nodes = [node for node in cluster_info["nodes"] if not node.get("nodeType")]
        total_storage = cluster_info["instance"]["totalStorage"] * len(cluster_nodes)

        self.log.debug("Waiting for cluster disk utilization to reach %d%%...", target_utilization)
        start_time = time.time()
        while time.time() - start_time < wait_timeout:
            clusters = self.cloud_api_client.get_clusters(account_id=self.account_id, metrics="STORAGE_USED")
            cluster = next(cluster for cluster in clusters if cluster["id"] == self.cluster_id)
            storage_used = cluster["metrics"]["STORAGE_USED"] / (1024**3)  # Convert bytes to GB

            current_disk_utilization = (storage_used / total_storage) * 100
            if current_disk_utilization >= target_utilization:
                self.log.info("Target disk utilization of %d%% reached", target_utilization)
                return

            self.log.debug("Current disk utilization: %.2f%%, waiting...", current_disk_utilization)
            time.sleep(check_interval)

        raise TimeoutError(f"Disk utilization did not reach {target_utilization}% within {wait_timeout}s")

    def log_index_status(self) -> None:
        """Log the current status of the vector index via the VS node API."""
        try:
            result = self.vs_remote_curl_client.run_remoter_curl(
                method="GET",
                path=f"api/v1/indexes/{self.KEYSPACE_NAME}/{self.INDEX_NAME}/status",
                params=None,
            )
            self.log.info(
                "Vector index status for %s.%s: %s", self.KEYSPACE_NAME, self.INDEX_NAME, result.stdout.strip()
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Failed to get vector index status: %s", exc)

    def validate_index_update(
        self, vector_id: uuid.UUID, test_vector: list[float], stop_event: threading.Event, session: "Session"
    ) -> bool | None:
        """Validate if a vector is found in the index with retry mechanism.

        This method performs validation attempts with delays between each attempt to allow
        for index updates to propagate. It will retry up to the specified number of times
        or until the vector is found in the ANN query results.

        Returns:
            True if the vector was found, False if not found after all retries,
            None if the stop event was received and the operation was aborted.
        """
        retries: int = 5
        delay: int = 45

        for attempt in range(1, retries + 1):
            self.log.debug("Waiting %d seconds for vector indexing (%d/%d attempt)...", delay, attempt, retries)
            if stop_event.wait(delay):
                self.log.info("Stop event received during indexing wait")
                return None

            self.log.debug("Checking the recently inserted vector made it into the index...")
            self.log_index_status()
            query_cql = f"""
                SELECT id, commenter FROM {self.KEYSPACE_NAME}.{self.TABLE_NAME}
                ORDER BY comment_vector ANN OF {test_vector} LIMIT 20
            """
            rows = list(session.execute(query_cql))

            found = any(str(row.id) == str(vector_id) for row in rows)
            if found:
                self.log.debug("Successfully found vector %s in ANN query results", vector_id)
                return True
            self.log.warning("Vector %s not found in ANN query results", vector_id)

        self.log.error("Vector %s not found after %d retries", vector_id, retries)
        return False

    def run_vector_search_operations_in_background(self, stop_event: threading.Event) -> threading.Thread:
        """Start vector insert and query operations in background."""

        def _run_vector_ops_in_background():
            operation_number = 0

            with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
                insert_prepared = session.prepare(f"""
                    INSERT INTO {self.KEYSPACE_NAME}.{self.TABLE_NAME} (id, commenter, comment_vector, created_at)
                    VALUES (?, ?, ?, toTimestamp(now()))
                """)

                while not stop_event.is_set():
                    operation_number += 1
                    self.log.info("Vector search operation #%d", operation_number)

                    batch = BatchStatement()
                    vectors = []
                    for i in range(self.VECTORS_PER_BATCH):
                        vector_id = uuid.uuid4()
                        test_vector = [round(random.uniform(0.0, 1.0), 2) for _ in range(self.VECTOR_DIMENSION)]
                        commenter = f"test_user_{operation_number}_{i}"
                        batch.add(insert_prepared, (vector_id, commenter, test_vector))
                        vectors.append((vector_id, test_vector))

                    sample_vector_id, sample_test_vector = random.choice(vectors)
                    try:
                        session.execute(batch)
                        self.log.info("Inserted batch of %d vectors", self.VECTORS_PER_BATCH)

                        self.log.info("Validating randomly selected vector %s", sample_vector_id)
                        validation_result = self.validate_index_update(
                            vector_id=sample_vector_id,
                            test_vector=sample_test_vector,
                            stop_event=stop_event,
                            session=session,
                        )
                        if validation_result is False:
                            self.log.error("Index update validation failed for vector %s", sample_vector_id)
                            self.vector_validation_failures_count += 1

                    except Exception as exc:  # noqa: BLE001
                        self.log.error("Error during vector operation #%d: %s", operation_number, exc)
                        self.vector_validation_failures_count += 1

            self.log.info("Vector search operations stopped after %d operations", operation_number)

        vector_thread = threading.Thread(target=_run_vector_ops_in_background)
        vector_thread.start()
        self.log.info("Vector search operations started in background thread")

        return vector_thread

    @retrying(n=30, sleep_time=60, allowed_exceptions=(Retry,))
    def wait_for_resize_request(self) -> int:
        requests = self.cloud_api_client.get_cluster_requests(account_id=self.account_id, cluster_id=self.cluster_id)
        resize_request = next((r for r in requests if r["requestType"] == "RESIZE_CLUSTER_V3"), None)
        if resize_request and resize_request["status"] == "IN_PROGRESS":
            return resize_request["id"]
        raise Retry("Resize request not found")

    @retrying(n=200, sleep_time=60, allowed_exceptions=(AssertionError,))
    def wait_for_cluster_scale_out(self, request_id: int) -> None:
        request = self.cloud_api_client.get_cluster_request_details(account_id=self.account_id, request_id=request_id)
        status = request["status"]
        if status in ("FAILED", "CANCELLED"):
            raise ScaleOutFailedException("Cluster scale out failed")
        assert status == "COMPLETED", f"Cluster resize is not completed yet. Current status: {status}"

    def stop_and_join_vector_thread(self, timeout: int = 300) -> None:
        """Stop the vector search background thread and wait for it to finish."""
        if not self.vector_thread or not self.vector_thread.is_alive():
            return
        self.log.info("Waiting for vector validation thread to complete...")
        if self.vector_stop_event:
            self.vector_stop_event.set()
        self.vector_thread.join(timeout=timeout)
        if self.vector_thread.is_alive():
            self.log.warning("Vector validation thread did not complete within timeout")

    def tearDown(self) -> None:
        """Clean up background threads and stop events."""
        if self.load_thread and self.load_thread.is_alive():
            self.log.info("Waiting for load thread to complete...")
            self.load_thread.join(timeout=60)
            if self.load_thread.is_alive():
                self.log.warning("C-S load thread did not complete within timeout")

        self.stop_and_join_vector_thread()

        super().tearDown()

    def test_vs_functions_while_xcloud_cluster_scaling(self) -> None:
        """Test vector search functionality during cluster scaling operations."""
        self.log.info("Prepare Vector Search test data")
        self.prepare_vs_keyspace()

        self.log.info("Start loading cluster to scaling threshold in background")
        self.load_thread = self.load_cluster_to_scaling_threshold_in_background()

        self.log.info("Wait for 60% of disk utilization to be reached before starting VS operation")
        self.wait_for_cluster_disk_utilization(target_utilization=60)

        self.log.info("Start running Vector Search operations in background while cluster is scaling")
        self.vector_stop_event = threading.Event()
        self.vector_thread = self.run_vector_search_operations_in_background(stop_event=self.vector_stop_event)

        self.log.info("Wait for cluster to scale out")
        request_id = self.wait_for_resize_request()
        self.wait_for_cluster_scale_out(request_id=request_id)

        self.log.info("Stop vector search operations and check results")
        self.stop_and_join_vector_thread()
        if self.vector_validation_failures_count:
            raise AssertionError(
                f"Vector search validation failed {self.vector_validation_failures_count} times during cluster scaling"
            )
