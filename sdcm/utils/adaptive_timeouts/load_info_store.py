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
# Copyright (c) 2023 ScyllaDB
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import cached_property
import re
from typing import Any, TYPE_CHECKING
from itertools import count
from dataclasses import dataclass

import yaml
from cachetools import cached, TTLCache
from argus.client.generic_result import StaticGenericResultTable, ColumnMetadata, ResultType, Status

if TYPE_CHECKING:
    from sdcm.cluster import BaseNode

from sdcm.remote import RemoteCmdRunner
from sdcm.test_config import TestConfig
from sdcm.utils.decorators import retrying
from sdcm.utils.metaclasses import Singleton
from sdcm.argus_results import submit_results_to_argus

LOGGER = logging.getLogger(__name__)


def convert_to_mb(value) -> int:
    pattern = re.compile(r'^(\d+(\.\d+)?) *([KMGT]?B)$')
    match = pattern.match(value)
    if match:
        number = float(match.group(1))
        suffix = match.group(3)
        match suffix:
            case 'KB':
                number /= 1024
            case 'GB':
                number *= 1024
            case 'TB':
                number *= 1024 * 1024
            case 'PB':
                number *= 1024 * 1024 * 1024
        return int(number)
    raise ValueError(f"Couldn't parse value {value} to MB")


class NodeLoadInfoService:
    """
    Service to get information about node load through running commands on node like getting metrics from localhost:9180/9100,
    nodetool status, uptime (load), vmstat (disk utilization). Command responses are cached for some time to avoid too much requests.
    """

    def __init__(self, remoter: RemoteCmdRunner, name: str, scylla_version: str, node_idx: str):
        self.remoter = remoter
        self._name = name
        self._scylla_version = scylla_version
        self.node_idx = node_idx

    @cached_property
    def _io_properties(self):
        return yaml.safe_load(self.remoter.run('cat /etc/scylla.d/io_properties.yaml', verbose=False).stdout)

    @cached(cache=TTLCache(maxsize=1024, ttl=300))
    def _cf_stats(self, keyspace):
        pass

    @cached(cache=TTLCache(maxsize=1024, ttl=300))
    def _get_nodetool_info(self):
        return self.remoter.run('nodetool info', verbose=False).stdout

    @cached(cache=TTLCache(maxsize=1024, ttl=60))
    def _get_node_load(self) -> tuple[float, float, float]:
        try:
            metrics = self._get_node_exporter_metrics()
            load_1 = float(metrics['node_load1'])
            load_5 = float(metrics['node_load5'])
            load_15 = float(metrics['node_load15'])
            return load_1, load_5, load_15
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Couldn't get node load from prometheus metrics. Error: %s", exc)
            # fallback to uptime
            load_1, load_5, load_15 = self.remoter.run('uptime').stdout.split("load average: ")[1].split(",")
            return float(load_1), float(load_5), float(load_15)

    def get_node_boot_time_seconds(self) -> float:
        metrics = self._get_node_exporter_metrics()
        mem_available = float(metrics['node_boot_time_seconds'])
        return mem_available

    @retrying(n=5, sleep_time=1, allowed_exceptions=(ValueError,))
    def _get_metrics(self, port):
        metrics = self.remoter.run(f'curl -s localhost:{port}/metrics', verbose=False).stdout
        metrics_dict = {}
        for line in metrics.splitlines():
            if line and not line.startswith('#'):
                try:
                    key, value = line.rsplit(' ', 1)
                    metrics_dict[key] = value
                except ValueError:
                    LOGGER.debug("Couldn't parse line: %s", line)
        return metrics_dict

    @cached(cache=TTLCache(maxsize=1024, ttl=60))
    def _get_scylla_metrics(self):
        return self._get_metrics(port=9180)

    @cached(cache=TTLCache(maxsize=1024, ttl=60))
    def _get_node_exporter_metrics(self):
        return self._get_metrics(port=9100)

    @property
    def node_data_size_mb(self) -> int:
        for line in self._get_nodetool_info().splitlines():
            if line.startswith('Load'):
                return convert_to_mb(line.split(':')[1].strip())
        raise ValueError("Couldn't find Load in nodetool info response")

    @property
    def cpu_load_5(self) -> float:
        return self._get_node_load()[1]

    @cached_property
    def shards_count(self) -> int:
        return len([key for key in self._get_scylla_metrics() if key.startswith('scylla_lsa_free_space')])

    @cached_property
    def scheduler_regex(self) -> re.compile:
        return re.compile(r".*group=\"(?P<group>.*)\",shard=\"(?P<shard>\d+)")

    def scylla_scheduler_shares(self) -> dict:
        """
        output example: {"sl:sl200": [200, 200], "sl:default": [1000, 1000]}
        """
        scheduler_group_shares = defaultdict(list)
        all_metrics = self._get_metrics(port=9180)
        for key, value in all_metrics.items():
            if key.startswith('scylla_scheduler_shares'):
                try:
                    match = self.scheduler_regex.match(key)
                    try:
                        scheduler_group_shares[match.groups()[0]].append(int(value.split(".")[0]))
                    except ValueError as details:
                        LOGGER.error("Failed to to convert value %s to integer. Error: %s", value, details)
                except AttributeError as error:
                    LOGGER.error("Failed to match metric: %s. Error: %s", key, error)

        return scheduler_group_shares

    @cached_property
    def read_bandwidth_mb(self) -> float:
        """based on io_properties.yaml in MB/s """
        return self._io_properties["disks"][0]["read_bandwidth"] / 1024 / 1024

    @cached_property
    def write_bandwidth_mb(self) -> float:
        """based on io_properties.yaml in MB/s"""
        return self._io_properties["disks"][0]["write_bandwidth"] / 1024 / 1024

    @cached_property
    def read_iops(self) -> float:
        return self._io_properties["disks"][0]["read_iops"]

    @cached_property
    def write_iops(self) -> float:
        return self._io_properties["disks"][0]["write_iops"]

    def as_dict(self):
        return {
            "node_name": self._name,
            "node_idx": self.node_idx,
            "cpu_load_5": self.cpu_load_5,
            "shards_count": self.shards_count,
            "read_bandwidth_mb": self.read_bandwidth_mb,
            "write_bandwidth_mb": self.write_bandwidth_mb,
            "read_iops": self.read_iops,
            "write_iops": self.write_iops,
            "node_data_size_mb": self.node_data_size_mb,
            "scylla_version": self._scylla_version,
        }


class AdaptiveTimeoutStore(metaclass=Singleton):
    """Class for storing metrics and other info related to adaptive timeouts.

    Used for future reference/node operations time tracking and calculations optimization."""

    def store(self, metrics: dict[str, Any], operation: str, duration: int | float, timeout: int,
              timeout_occurred: bool) -> None:
        pass

    def get(self, operation: str | None, timeout_occurred: bool = False):
        pass


@dataclass
class ArgusAdaptiveTimeoutResult:
    """Dataclass to hold adaptive timeout results for Argus submission."""
    operation: str
    duration: int
    timeout: int
    timeout_occurred: bool
    end_time: str
    metrics: dict[str, Any]


class AdaptiveTimeoutResultsTable(StaticGenericResultTable):

    def __init__(self, operation):
        super().__init__(name=f"{operation} - Timeout Statistics")

    class Meta:
        description = "measurement of specific operation timeouts (ex. decommission, adding nodes etc.)"
        columns = [
            ColumnMetadata(name="duration", unit="HH:MM:SS", type=ResultType.DURATION, higher_is_better=False),
            ColumnMetadata(name="timeout", unit="HH:MM:SS", type=ResultType.DURATION),
            ColumnMetadata(name="end_time", unit="", type=ResultType.TEXT),
            ColumnMetadata(name="cpu_load_5", unit="%", type=ResultType.FLOAT, visible=False),
            ColumnMetadata(name="shards_count", unit="", type=ResultType.INTEGER, visible=False),
            ColumnMetadata(name="read_bandwidth_mb", unit="MB/s", type=ResultType.FLOAT, visible=False),
            ColumnMetadata(name="write_bandwidth_mb", unit="MB/s", type=ResultType.FLOAT, visible=False),
            ColumnMetadata(name="read_iops", unit="op/s", type=ResultType.INTEGER, visible=False),
            ColumnMetadata(name="write_iops", unit="op/s", type=ResultType.INTEGER, visible=False),
            ColumnMetadata(name="node_data_size_mb", unit="MB", type=ResultType.INTEGER, visible=False),
            ColumnMetadata(name="node_idx", unit="", type=ResultType.TEXT),
        ]


class ArgusAdaptiveTimeoutStore(AdaptiveTimeoutStore):
    """
    Report adaptive timeout results to Argus.
    """

    def __init__(self):
        self.test_config = TestConfig()
        self.cycle_counters = defaultdict(count)

    def send_adaptive_timeout_results_to_argus(self, result: ArgusAdaptiveTimeoutResult):
        """
        Send adaptive timeout results to Argus.
        """
        argus_client = self.test_config.argus_client()
        if not argus_client:
            LOGGER.warning("Will not submit to argus - no client initialized")
            return

        cycle = next(self.cycle_counters[result.operation]) + 1
        table = AdaptiveTimeoutResultsTable(operation=result.operation)
        table.add_result(column="duration", row=f"#{cycle}", value=result.duration,
                         status=Status.PASS if not result.timeout_occurred else Status.ERROR)
        table.add_result(column="timeout", row=f"#{cycle}", value=result.timeout, status=Status.UNSET)
        table.add_result(column="end_time", row=f"#{cycle}", value=result.end_time, status=Status.UNSET)
        table.add_result(column="cpu_load_5", row=f"#{cycle}",
                         value=result.metrics.get('cpu_load_5'), status=Status.UNSET)
        table.add_result(column="shards_count", row=f"#{cycle}",
                         value=result.metrics.get('shards_count'), status=Status.UNSET)
        table.add_result(column="read_bandwidth_mb", row=f"#{cycle}", value=result.metrics.get(
            'read_bandwidth_mb'), status=Status.UNSET)
        table.add_result(column="write_bandwidth_mb", row=f"#{cycle}", value=result.metrics.get(
            'write_bandwidth_mb'), status=Status.UNSET)
        table.add_result(column="read_iops", row=f"#{cycle}",
                         value=result.metrics.get('read_iops'), status=Status.UNSET)
        table.add_result(column="write_iops", row=f"#{cycle}",
                         value=result.metrics.get('write_iops'), status=Status.UNSET)
        table.add_result(column="node_data_size_mb", row=f"#{cycle}", value=result.metrics.get(
            'node_data_size_mb'), status=Status.UNSET)
        table.add_result(column="node_idx", row=f"#{cycle}",
                         value=result.metrics.get('node_idx'), status=Status.UNSET)

        logging.debug("Submitting adaptive timeout results to Argus: %s", table.as_dict())
        submit_results_to_argus(argus_client, table)

    def store(self, metrics: dict[str, Any], operation: str, duration: float, timeout: float,
              timeout_occurred: bool):
        result = ArgusAdaptiveTimeoutResult(
            operation=operation,
            duration=int(duration),
            timeout=int(timeout),
            timeout_occurred=timeout_occurred,
            end_time="N/A",
            metrics=metrics.copy()
        )
        try:
            result.end_time = datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%H:%M:%S")
        except ValueError:
            pass

        self.send_adaptive_timeout_results_to_argus(result)

    def get(self, operation: str | None, timeout_occurred: bool | None = None):

        # TODO: we don't have yet API to get measurements from Argus
        # TODO: also adaptive_timeout decorator isn't reading measurements yet...
        pass


class NodeLoadInfoServices(metaclass=Singleton):
    """Cache for NodeLoadInfoService instances."""

    def __init__(self):
        self._services: dict[str, NodeLoadInfoService] = {}

    def get(self, node: BaseNode) -> NodeLoadInfoService:  # noqa: F821
        if node not in self._services:
            self._services[node.name] = NodeLoadInfoService(node.remoter, node.name, node.scylla_version_detailed,
                                                            node_idx=str(getattr(node, "node_index", "")))
        if self._services[node.name].remoter != node.remoter:
            self._services[node.name] = NodeLoadInfoService(node.remoter, node.name, node.scylla_version_detailed,
                                                            node_idx=str(getattr(node, "node_index", "")))
        return self._services[node.name]
