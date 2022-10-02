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
# Copyright (c) 2022 ScyllaDB

import re
import time


class LoaderUtilsMixin:
    """This mixin can be added to any class that inherits the 'ClusterTester' one"""

    def _parse_stress_cmd(self, stress_cmd, params):
        # Due to an issue with scylla & cassandra-stress - we need to create the counter table manually
        if 'counter_' in stress_cmd:
            self._create_counter_table()

        if 'compression' in stress_cmd:
            if 'keyspace_name' not in params:
                compression_prefix = re.search('compression=(.*)Compressor', stress_cmd).group(1)
                keyspace_name = "keyspace_{}".format(compression_prefix.lower())
                params.update({'keyspace_name': keyspace_name})

        return params

    def _create_counter_table(self):
        """
        workaround for the issue https://github.com/scylladb/scylla-tools-java/issues/32
        remove when resolved
        """
        node = self.db_cluster.nodes[0]
        with self.db_cluster.cql_connection_patient(node) as session:
            session.execute("""
                CREATE KEYSPACE IF NOT EXISTS keyspace1
                WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '3'} AND durable_writes = true;
            """)
            session.execute("""
                CREATE TABLE IF NOT EXISTS keyspace1.counter1 (
                    key blob PRIMARY KEY,
                    "C0" counter,
                    "C1" counter,
                    "C2" counter,
                    "C3" counter,
                    "C4" counter
                ) WITH COMPACT STORAGE
                    AND bloom_filter_fp_chance = 0.01
                    AND caching = '{"keys":"ALL","rows_per_partition":"ALL"}'
                    AND comment = ''
                    AND compaction = {'class': 'SizeTieredCompactionStrategy'}
                    AND compression = {}
                    AND gc_grace_seconds = 864000
                    AND default_time_to_live = 0
                    AND max_index_interval = 2048
                    AND min_index_interval = 128
                    AND read_repair_chance = 0.0
                    AND dclocal_read_repair_chance = 0.1
                    AND memtable_flush_period_in_ms = 0
                    AND speculative_retry = '99.0PERCENTILE';
            """)

    def _run_all_stress_cmds(self, stress_queue, params):
        stress_cmds = params['stress_cmd']
        if not isinstance(stress_cmds, list):
            stress_cmds = [stress_cmds]
        # In some cases we want the same stress_cmd to run several times (can be used with round_robin or not).
        stress_multiplier = self.params.get('stress_multiplier')
        if stress_multiplier > 1:
            stress_cmds *= stress_multiplier

        for stress_cmd in stress_cmds:
            params.update({'stress_cmd': stress_cmd})
            self._parse_stress_cmd(stress_cmd, params)

            # Run all stress commands
            self.log.debug('stress cmd: {}'.format(stress_cmd))
            if stress_cmd.startswith('scylla-bench'):
                stress_queue.append(self.run_stress_thread(stress_cmd=stress_cmd,
                                                           stats_aggregate_cmds=False,
                                                           round_robin=self.params.get('round_robin')))
            elif stress_cmd.startswith('cassandra-harry'):
                stress_queue.append(self.run_stress_thread(**params))
            else:
                stress_queue.append(self.run_stress_thread(**params))

            time.sleep(10)

            # Remove "user profile" param for the next command
            if 'profile' in params:
                del params['profile']

            if 'keyspace_name' in params:
                del params['keyspace_name']

        return stress_queue

    @staticmethod
    def _get_keyspace_name(ks_number, keyspace_pref='keyspace'):
        return '{}{}'.format(keyspace_pref, ks_number)

    def _run_cql_commands(self, cmds, node=None):
        node = node if node else self.db_cluster.nodes[0]

        if not isinstance(cmds, list):
            cmds = [cmds]

        for cmd in cmds:
            # pylint: disable=no-member
            with self.db_cluster.cql_connection_patient(node) as session:
                session.execute(cmd)

    def _pre_create_keyspace(self):
        cmds = self.params.get('pre_create_keyspace')
        self._run_cql_commands(cmds)

    def run_post_prepare_cql_cmds(self):
        if post_prepare_cql_cmds := self.params.get('post_prepare_cql_cmds'):
            self.log.debug("Execute post prepare queries: %s", post_prepare_cql_cmds)
            self._run_cql_commands(post_prepare_cql_cmds)

    def run_prepare_write_cmd(self):
        # In some cases (like many keyspaces), we want to create the schema (all keyspaces & tables) before the load
        # starts - due to the heavy load, the schema propogation can take long time and c-s fails.
        prepare_write_cmd = self.params.get('prepare_write_cmd')
        keyspace_num = self.params.get('keyspace_num')
        write_queue = []
        verify_queue = []

        if not prepare_write_cmd:
            self.log.debug("No prepare write commands are configured to run. Continue with stress commands")
            return
        # When the load is too heavy for one loader when using MULTI-KEYSPACES, the load is spreaded evenly across
        # the loaders (round_robin).
        if keyspace_num > 1 and self.params.get('round_robin'):
            self.log.debug("Using round_robin for multiple Keyspaces...")
            for i in range(1, keyspace_num + 1):
                keyspace_name = self._get_keyspace_name(i)
                self._run_all_stress_cmds(write_queue, params={'stress_cmd': prepare_write_cmd,
                                                               'keyspace_name': keyspace_name,
                                                               'round_robin': True})
        # Not using round_robin and all keyspaces will run on all loaders
        else:
            self._run_all_stress_cmds(write_queue, params={'stress_cmd': prepare_write_cmd,
                                                           'keyspace_num': keyspace_num,
                                                           'round_robin': self.params.get('round_robin')})

        # In some cases we don't want the nemesis to run during the "prepare" stage in order to be 100% sure that
        # all keys were written succesfully
        if self.params.get('nemesis_during_prepare'):
            # Wait for some data (according to the param in the yaml) to be populated, for multi keyspace need to
            # pay attention to the fact it checks only on keyspace1
            self.db_cluster.wait_total_space_used_per_node(keyspace=None)
            self.db_cluster.start_nemesis()

        # Wait on the queue till all threads come back.
        # todo: we need to improve this part for some cases that threads are being killed and we don't catch it.
        for stress in write_queue:
            self.verify_stress_thread(cs_thread_pool=stress)

        # Run nodetool flush on all nodes to make sure nothing left in memory
        # I decided to comment this out for now, when we found the data corruption bug, we wanted to be on the safe
        # side, but I don't think we should continue with this approach.
        # If we decided to add this back in the future, we need to wrap it with try-except because it can run
        # in parallel to nemesis and it will fail on one of the nodes.
        # self._flush_all_nodes()

        # In case we would like to verify all keys were written successfully before we start other stress / nemesis
        prepare_verify_cmd = self.params.get('prepare_verify_cmd')
        if prepare_verify_cmd:
            self._run_all_stress_cmds(verify_queue, params={'stress_cmd': prepare_verify_cmd,
                                                            'keyspace_num': keyspace_num})

            for stress in verify_queue:
                self.verify_stress_thread(cs_thread_pool=stress)

        self.run_post_prepare_cql_cmds()

        prepare_wait_no_compactions_timeout = self.params.get('prepare_wait_no_compactions_timeout')
        if prepare_wait_no_compactions_timeout:
            for node in self.db_cluster.nodes:
                node.run_nodetool("compact")
            self.wait_no_compactions_running(n=prepare_wait_no_compactions_timeout)
        self.log.info('Prepare finished')