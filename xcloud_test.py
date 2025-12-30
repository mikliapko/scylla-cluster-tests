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
# Copyright (c) 2016 ScyllaDB

from sdcm.mgmt import HostStatus
from sdcm.utils.decorators import retrying, Retry
from longevity_test import LongevityTest


class XcloudScaleOutTest(LongevityTest):

    # The expected max time for scale out successful completion is 40 minutes
    @retrying(n=20, sleep_time=60*5, allowed_exceptions=(Retry, AssertionError))
    def verify_cluster_scaled_out(self, initial_node_ids: list):
        self.log.info("Get list of nodes after cluster is populated with data")
        updated_nodes_ids = self.db_cluster.get_cluster_nodes()

        self.log.info(f"Verify the number of nodes after scale out is 3 and all the old nodes were replaced")
        assert set(initial_node_ids).isdisjoint(set(updated_nodes_ids)), "Nodes were not replaced after scale out"
        assert len(updated_nodes_ids) == 3, "Number of nodes after scaling is not 3"

    def test_xcloud_scale_out(self):
        """
        Test Xcloud cluster automatic scale out triggering upon reaching specified threshold
        """
        initial_node_ids = self.db_cluster.get_cluster_nodes()
        stress_queue = []
        stress_cmd = self.params.get('stress_cmd')
        keyspace_num = 1

        self.assemble_and_run_all_stress_cmd(stress_queue, stress_cmd, keyspace_num)
        for stress in stress_queue:
            self.verify_stress_thread(stress)

        self.verify_cluster_scaled_out(initial_node_ids)