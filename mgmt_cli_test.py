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
# pylint: disable=too-many-lines

# pylint: disable=too-many-lines
import random
from pathlib import Path
from functools import cached_property
import re
import time
from textwrap import dedent
from datetime import datetime
from dataclasses import dataclass

import boto3
import yaml

from invoke import exceptions

from sdcm import mgmt
from sdcm.mgmt import ScyllaManagerError, TaskStatus, HostStatus, HostSsl, HostRestStatus
from sdcm.mgmt.cli import ScyllaManagerTool
from sdcm.mgmt.common import reconfigure_scylla_manager, get_persistent_snapshots
from sdcm.provision.helpers.certificate import TLSAssets
from sdcm.remote import shell_script_cmd
from sdcm.tester import ClusterTester
from sdcm.cluster import TestConfig
from sdcm.nemesis import MgmtRepair
from sdcm.utils.adaptive_timeouts import adaptive_timeout, Operations
from sdcm.utils.common import reach_enospc_on_node, clean_enospc_on_node
from sdcm.utils.loader_utils import LoaderUtilsMixin
from sdcm.sct_events.system import InfoEvent
from sdcm.sct_events.group_common_events import ignore_no_space_errors, ignore_stream_mutation_fragments_errors
from sdcm.utils.gce_utils import get_gce_storage_client
from sdcm.utils.azure_utils import AzureService
from sdcm.utils.tablets.common import TabletsConfiguration
from sdcm.exceptions import FilesNotCorrupted


@dataclass
class ManagerTestMetrics:
    backup_time = "N/A"
    restore_time = "N/A"


@dataclass
class SnapshotData:
    """Describes the backup snapshot:

    - bucket: S3 bucket name
    - tag: snapshot tag, for example 'sm_20240816185129UTC'
    - exp_timeout: expected timeout for the restore operation
    - keyspaces: list of keyspaces presented in backup
    - cs_read_cmd_template: cassandra-stress read command template
    - prohibit_verification_read: if True, the verification read will be prohibited. Most likely, such a backup was
      created via c-s user profile.
    - number_of_rows: number of data rows written in the DB
    - node_ids: list of node ids where backup was created
    """
    bucket: str
    tag: str
    exp_timeout: int
    keyspaces: list[str]
    ks_tables_map: dict[str, list[str]]
    cs_read_cmd_template: str
    prohibit_verification_read: bool
    number_of_rows: int
    node_ids: list[str]


@dataclass
class RestoreOutsideManager:
    load_and_stream: bool = False
    primary_replica_only: bool = False


class BackupFunctionsMixIn(LoaderUtilsMixin):
    DESTINATION = Path('/tmp/backup')

    backup_azure_blob_service = None
    backup_azure_blob_sas = None
    test_config = TestConfig()

    @cached_property
    def locations(self) -> list[str]:
        backend = self.params.get("backup_bucket_backend")

        buckets = self.params.get("backup_bucket_location")
        if not isinstance(buckets, list):
            buckets = buckets.split()

        # FIXME: Make it works with multiple locations or file a bug for scylla-manager.
        return [f"{backend}:{location}" for location in buckets[:1]]

    def _run_cmd_with_retry(self, executor, cmd, retries=10):
        for _ in range(retries):
            try:
                executor(cmd)
                break
            except exceptions.UnexpectedExit as ex:
                self.log.debug("cmd %s failed with error %s, will retry", cmd, ex)

    def install_awscli_dependencies(self, node):
        if node.distro.is_ubuntu or node.distro.is_debian:
            cmd = dedent("""
            apt update
            apt install -y python3-pip
            pip install awscli==1.18.140
            """)
        elif node.distro.is_rhel_like:
            cmd = dedent("""
            yum install -y epel-release
            yum install -y python-pip
            yum remove -y epel-release
            pip install awscli==1.18.140
            """)
        else:
            raise RuntimeError("This distro is not supported")
        self._run_cmd_with_retry(executor=node.remoter.sudo, cmd=shell_script_cmd(cmd))

    def install_gsutil_dependencies(self, node):
        if node.distro.is_ubuntu or node.distro.is_debian:
            cmd = dedent("""
                echo 'deb https://packages.cloud.google.com/apt cloud-sdk main' | sudo tee -a /etc/apt/sources.list.d/google-cloud-sdk.list
                curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key add -
                sudo apt-get update
            """)
            self._run_cmd_with_retry(executor=node.remoter.run, cmd=shell_script_cmd(cmd))
            node.install_package("google-cloud-cli")
        else:
            raise NotImplementedError("At the moment, we only support debian installation")

    def install_azcopy_dependencies(self, node):
        self._run_cmd_with_retry(executor=node.remoter.sudo, cmd=shell_script_cmd("""\
            curl -L https://aka.ms/downloadazcopy-v10-linux | \
                tar xz -C /usr/bin --strip-components 1 --wildcards '*/azcopy'
        """))
        self.backup_azure_blob_service = \
            f"https://{self.test_config.backup_azure_blob_credentials['account']}.blob.core.windows.net/"
        self.backup_azure_blob_sas = self.test_config.backup_azure_blob_credentials["download_sas"]

    @staticmethod
    def download_from_s3(node, source, destination):
        node.remoter.sudo(f"aws s3 cp '{source}' '{destination}'")

    @staticmethod
    def download_from_gs(node, source, destination):
        node.remoter.sudo(f"gsutil cp '{source.replace('gcs://', 'gs://')}' '{destination}'")

    def download_from_azure(self, node, source, destination):
        # azure://<bucket>/<path> -> https://<account>.blob.core.windows.net/<bucket>/<path>?SAS
        source = f"{source.replace('azure://', self.backup_azure_blob_service)}{self.backup_azure_blob_sas}"
        node.remoter.sudo(f"azcopy copy '{source}' '{destination}'")

    def get_table_id(self, node, table_name, keyspace_name=None, remove_hyphen=True):
        """

        :param keyspace_name: not mandatory. Should be used when there's more than one table with the same
        name in different keyspaces
        :param remove_hyphen: In the table's directory, scylla removes the hyphens from the id. Setting
        the attribute to True will remove the hyphens.
        """
        query = f"SELECT id FROM system_schema.tables WHERE table_name='{table_name}'"
        if keyspace_name:
            query += f"and keyspace_name='{keyspace_name}'"
        with self.db_cluster.cql_connection_patient(node) as session:
            results = session.execute(query)
        base_id = str(results[0].id)
        if remove_hyphen:
            return base_id.replace('-', '')
        return base_id

    def restore_backup_outside_of_manager(self, mgr_cluster, snapshot_tag, ks_tables_list, load_and_stream=False,
                                          primary_replica_only=False, location=None, node_ids=None):  # pylint: disable=too-many-locals
        self.log.info("Do backup outside of Manager")
        backup_bucket_backend = self.params.get("backup_bucket_backend")
        if backup_bucket_backend == "s3":
            install_dependencies = self.install_awscli_dependencies
            download = self.download_from_s3
        elif backup_bucket_backend == "gcs":
            install_dependencies = self.install_gsutil_dependencies
            download = self.download_from_gs
        elif backup_bucket_backend == "azure":
            install_dependencies = self.install_azcopy_dependencies
            download = self.download_from_azure
        else:
            raise ValueError(f'{backup_bucket_backend=} is not supported')

        per_node_backup_file_paths = mgr_cluster.get_backup_files_dict(snapshot_tag, location)
        for index, (node, node_id) in enumerate(zip(self.db_cluster.nodes, node_ids), start=1):
            self.log.info(f"Handling the node #{index}, {node.host_id}")
            install_dependencies(node=node)
            node_data_path = Path("/var/lib/scylla/data")
            for keyspace, tables in ks_tables_list.items():
                keyspace_path = node_data_path / keyspace
                for table in tables:
                    self.log.info(f"Handling the table {keyspace}.{table}")
                    table_id = self.get_table_id(node=node, table_name=table, keyspace_name=keyspace)
                    table_upload_path = keyspace_path / f"{table}-{table_id}" / "upload"
                    self.log.info(f"Start download the backup files, node #{index}, table - {keyspace}.{table}")
                    for file_path in per_node_backup_file_paths[node_id][keyspace][table]:
                        download(node=node, source=file_path, destination=table_upload_path)
                    self.log.info(f"Finished download, node #{index}, table - {keyspace}.{table}")
                    node.remoter.sudo(f"chown scylla:scylla -Rf {table_upload_path}")

                    load_and_stream_flag = "--load-and-stream" if load_and_stream else ""
                    primary_replica_only_flag = "--primary-replica-only" if primary_replica_only else ""
                    node_refresh_cmd = f"refresh {keyspace} {table} {load_and_stream_flag} {primary_replica_only_flag}"
                    self.log.info(f"Run node refresh cmd, node #{index}, table - {keyspace}.{table}")
                    node.run_nodetool(node_refresh_cmd)
                    self.log.info(f"Finished node refresh, node #{index}, table - {keyspace}.{table}")

    def restore_backup_from_backup_task(self, mgr_cluster, backup_task, keyspace_and_table_list):
        snapshot_tag = backup_task.get_snapshot_tag()
        self.restore_backup_outside_of_manager(mgr_cluster=mgr_cluster, snapshot_tag=snapshot_tag,
                                               ks_tables_list=keyspace_and_table_list)

    # pylint: disable=too-many-arguments
    def verify_backup_success(self, mgr_cluster, backup_task, ks_names: list = None, tables_names: list = None,
                              truncate=True, restore_data_with_task=False, timeout=None):
        if ks_names is None:
            ks_names = ['keyspace1']
        if tables_names is None:
            tables_names = ['standard1']
        ks_tables_map = {keyspace: tables_names for keyspace in ks_names}
        if truncate:
            for ks, tables in ks_tables_map.items():
                for table_name in tables:
                    self.log.info(f'running truncate on {ks}.{table_name}')
                    self.db_cluster.nodes[0].run_cqlsh(f'TRUNCATE {ks}.{table_name}')
        if restore_data_with_task:
            self.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=backup_task.get_snapshot_tag(),
                                          timeout=timeout, restore_data=True)
        else:
            self.restore_backup_from_backup_task(mgr_cluster=mgr_cluster, backup_task=backup_task,
                                                 keyspace_and_table_list=ks_tables_map)

    def restore_backup_with_task(self, mgr_cluster, snapshot_tag, timeout, restore_schema=False, restore_data=False,
                                 location_list=None, batch_size=None, parallel=None):
        location_list = location_list if location_list else self.locations
        restore_task = mgr_cluster.create_restore_task(restore_schema=restore_schema, restore_data=restore_data,
                                                       location_list=location_list, snapshot_tag=snapshot_tag,
                                                       batch_size=batch_size, parallel=parallel)
        restore_task.wait_and_get_final_status(step=30, timeout=timeout)
        assert restore_task.status == TaskStatus.DONE, f"Restoration of {snapshot_tag} has failed!"
        InfoEvent(message=f'The restore task has ended successfully. '
                          f'Restore run time: {restore_task.duration}.').publish()
        if restore_schema:
            self.db_cluster.restart_scylla()  # After schema restoration, you should restart the nodes
        return restore_task

    def run_verification_read_stress(self, ks_names=None):
        stress_queue = []
        stress_cmd = self.params.get('stress_read_cmd')
        keyspace_num = self.params.get('keyspace_num')
        InfoEvent(message='Starting read stress for data verification').publish()
        stress_start_time = datetime.now()
        if ks_names:
            self.assemble_and_run_all_stress_cmd_by_ks_names(stress_queue, stress_cmd, ks_names)
        else:
            self.assemble_and_run_all_stress_cmd(stress_queue, stress_cmd, keyspace_num)
        for stress in stress_queue:
            self.verify_stress_thread(cs_thread_pool=stress)
        stress_run_time = datetime.now() - stress_start_time
        InfoEvent(message=f'The read stress run was completed. Total run time: {stress_run_time}').publish()

    def _generate_load(self, keyspace_name: str = None):
        self.log.info('Starting c-s write workload')
        stress_cmd = self.params.get('stress_cmd')
        stress_thread = self.run_stress_thread(stress_cmd=stress_cmd, keyspace_name=keyspace_name)
        self.log.info('Sleeping for 15s to let cassandra-stress run...')
        time.sleep(15)
        return stress_thread

    def generate_load_and_wait_for_results(self, keyspace_name: str = None):
        load_thread = self._generate_load(keyspace_name=keyspace_name)
        load_results = load_thread.get_results()
        self.log.info(f'load={load_results}')

    def get_keyspace_name(self, ks_prefix: str = 'keyspace', ks_number: int = 1) -> list:
        """Get keyspace name based on the following logic:
            - if keyspaces number > 1, numeric indexes are used in the ks name;
            - if keyspaces number == 1, ks name depends on whether compression is applied to keyspace. If applied,
            the compression postfix will be used in the name, otherwise numeric index (keyspace1)
        """
        if ks_number > 1:
            return ['{}{}'.format(ks_prefix, i) for i in range(1, ks_number + 1)]
        else:
            stress_cmd = self.params.get('stress_read_cmd')
            if 'compression' in stress_cmd:
                compression_postfix = re.search('compression=(.*)Compressor', stress_cmd).group(1)
                keyspace_name = '{}_{}'.format(ks_prefix, compression_postfix.lower())
            else:
                keyspace_name = '{}{}'.format(ks_prefix, ks_number)
            return [keyspace_name]

    def generate_background_read_load(self):
        self.log.info('Starting c-s read')
        stress_cmd = self.params.get('stress_read_cmd')
        number_of_nodes = self.params.get("n_db_nodes")
        number_of_loaders = self.params.get("n_loaders")

        throttle_per_node = 14666
        throttle_per_loader = int(throttle_per_node * number_of_nodes / number_of_loaders)
        stress_cmd = stress_cmd.replace("<THROTTLE_PLACE_HOLDER>", str(throttle_per_loader))
        stress_thread = self.run_stress_thread(stress_cmd=stress_cmd)
        self.log.info('Sleeping for 15s to let cassandra-stress run...')
        time.sleep(15)
        return stress_thread


# pylint: disable=too-many-public-methods
class MgmtCliTest(BackupFunctionsMixIn, ClusterTester):
    """
    Test Scylla Manager operations on Scylla cluster.
    """
    CLUSTER_NAME = "mgr_cluster1"
    LOCALSTRATEGY_KEYSPACE_NAME = "localstrategy_keyspace"
    NETWORKSTRATEGY_KEYSPACE_NAME = "networkstrategy_keyspace"

    manager_test_metrics = ManagerTestMetrics()

    def _ensure_and_get_cluster(self, manager_tool, force_add: bool = False):
        """Get the cluster if it is already added, otherwise add it to manager.
        Use force_add=True if you want to re-add the cluster (delete and add again) even if it already added.
        """
        mgr_cluster = manager_tool.get_cluster(cluster_name=self.CLUSTER_NAME)
        if not mgr_cluster or force_add:
            if mgr_cluster:
                mgr_cluster.delete()
            mgr_cluster = manager_tool.add_cluster(name=self.CLUSTER_NAME, db_cluster=self.db_cluster,
                                                   auth_token=self.monitors.mgmt_auth_token)
        return mgr_cluster

    def test_mgmt_repair_nemesis(self):
        """
            Test steps:
            1) Run cassandra stress on cluster.
            2) Add cluster to Manager and run full repair via Nemesis
        """
        self.generate_load_and_wait_for_results()
        self.log.debug("test_mgmt_cli: initialize MgmtRepair nemesis")
        mgmt_nemesis = MgmtRepair(tester_obj=self, termination_event=self.db_cluster.nemesis_termination_event)
        mgmt_nemesis.disrupt()

    def test_mgmt_cluster_crud(self):
        """
        Test steps:
        1) add a cluster to manager.
        2) update the cluster attributes in manager: name/host
        3) delete the cluster from manager and re-add again.
        """
        self.log.info('starting test_mgmt_cluster_crud')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        # Test cluster attributes
        cluster_orig_name = mgr_cluster.name
        mgr_cluster.update(name="{}_renamed".format(cluster_orig_name))
        assert mgr_cluster.name == cluster_orig_name+"_renamed", "Cluster name wasn't changed after update command"
        mgr_cluster.delete()
        mgr_cluster = manager_tool.add_cluster(self.CLUSTER_NAME, db_cluster=self.db_cluster,
                                               auth_token=self.monitors.mgmt_auth_token)

        mgr_cluster.delete()  # remove cluster at the end of the test
        self.log.info('finishing test_mgmt_cluster_crud')

    def get_cluster_hosts_ip(self):
        return ScyllaManagerTool.get_cluster_hosts_ip(self.db_cluster)

    def get_cluster_hosts_with_ips(self):
        return ScyllaManagerTool.get_cluster_hosts_with_ips(self.db_cluster)

    def get_all_dcs_names(self):
        dcs_names = set()
        for node in self.db_cluster.nodes:
            data_center = self.db_cluster.get_nodetool_info(node)['Data Center']
            dcs_names.add(data_center)
        return dcs_names

    def _create_keyspace_and_basic_table(self, keyspace_name, table_name="example_table",
                                         replication_factor=1):
        self.log.info("creating keyspace {}".format(keyspace_name))
        keyspace_existence = self.create_keyspace(keyspace_name, replication_factor)
        assert keyspace_existence, "keyspace creation failed"
        # Keyspaces without tables won't appear in the repair, so the must have one
        self.log.info("creating the table {} in the keyspace {}".format(table_name, keyspace_name))
        self.create_table(table_name, keyspace_name=keyspace_name)

    def test_manager_sanity(self, prepared_ks: bool = False, ks_names: list = None):
        """
        Test steps:
        1) Run the repair test.
        2) Run test_mgmt_cluster test.
        3) test_mgmt_cluster_healthcheck
        4) test_client_encryption
        """
        if not prepared_ks:
            self.generate_load_and_wait_for_results()
        with self.subTest('Basic Backup Test'):
            self.test_basic_backup(ks_names=ks_names)
        with self.subTest('Restore Backup Test'):
            self.test_restore_backup_with_task(ks_names=ks_names)
        with self.subTest('Repair Multiple Keyspace Types'):
            self.test_repair_multiple_keyspace_types()
        with self.subTest('Mgmt Cluster CRUD'):
            self.test_mgmt_cluster_crud()
        with self.subTest('Mgmt cluster Health Check'):
            self.test_mgmt_cluster_healthcheck()
        # test_healthcheck_change_max_timeout requires a multi dc run
        if self.db_cluster.nodes[0].test_config.MULTI_REGION:
            with self.subTest('Basic test healthcheck change max timeout'):
                self.test_healthcheck_change_max_timeout()
        with self.subTest('Basic test suspend and resume'):
            self.test_suspend_and_resume()
        with self.subTest('Client Encryption'):
            # Since this test activates encryption, it has to be the last test in the sanity
            self.test_client_encryption()

    def test_manager_sanity_vnodes_tablets_cluster(self):
        """
        Test steps:
        1) Create tablets keyspace and propagate some data.
        2) Create vnodes keyspace and propagate some data.
        3) Run sanity test (test_manager_sanity).
        """
        self.log.info('starting test_manager_sanity_vnodes_tablets_cluster')

        ks_config = [("tablets_keyspace", True), ("vnodes_keyspace", False)]
        ks_names = [i[0] for i in ks_config]
        for ks_name, tablets_enabled in ks_config:
            tablets_config = TabletsConfiguration(enabled=tablets_enabled)
            self.create_keyspace(ks_name, replication_factor=3, tablets_config=tablets_config)
            self.generate_load_and_wait_for_results(keyspace_name=ks_name)

        self.test_manager_sanity(prepared_ks=True, ks_names=ks_names)

        self.log.info('finishing test_manager_sanity_vnodes_tablets_cluster')

    def test_repair_intensity_feature_on_multiple_node(self):
        self._repair_intensity_feature(fault_multiple_nodes=True)

    def test_repair_intensity_feature_on_single_node(self):
        self._repair_intensity_feature(fault_multiple_nodes=False)

    def test_repair_control(self):
        InfoEvent(message="Starting C-S write load").publish()
        self.run_prepare_write_cmd()
        InfoEvent(message="Flushing").publish()
        for node in self.db_cluster.nodes:
            node.run_nodetool("flush")
        InfoEvent(message="Waiting for compactions to end").publish()
        self.wait_no_compactions_running(n=90, sleep_time=30)
        InfoEvent(message="Starting C-S read load").publish()
        stress_read_thread = self.generate_background_read_load()
        time.sleep(600)  # So we will see the base load of the cluster
        InfoEvent(message="Sleep ended - Starting tests").publish()
        self._create_repair_and_alter_it_with_repair_control()
        load_results = stress_read_thread.get_results()
        self.log.info('load={}'.format(load_results))

    def _create_repair_and_alter_it_with_repair_control(self):
        keyspace_to_be_repaired = "keyspace2"
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = manager_tool.add_cluster(name=self.CLUSTER_NAME + '_repair_control',
                                               db_cluster=self.db_cluster,
                                               auth_token=self.monitors.mgmt_auth_token)
        # writing 292968720 rows, equal to the amount of data written in the prepare (around 100gb per node),
        # to create a large data fault and therefore a longer running repair
        self.create_missing_rows_in_cluster(create_missing_rows_in_multiple_nodes=True,
                                            keyspace_to_be_repaired=keyspace_to_be_repaired,
                                            total_num_of_rows=292968720)
        arg_list = [{"intensity": .0001},
                    {"intensity": 0},
                    {"parallel": 1},
                    {"intensity": 2, "parallel": 1}]

        InfoEvent(message="Repair started").publish()
        repair_task = mgr_cluster.create_repair_task(keyspace="keyspace2")
        next_percentage_block = 20
        repair_task.wait_for_percentage(next_percentage_block)
        for args in arg_list:
            next_percentage_block += 20
            InfoEvent(message=f"Changing repair args to: {args}").publish()
            mgr_cluster.control_repair(**args)
            repair_task.wait_for_percentage(next_percentage_block)
        repair_task.wait_and_get_final_status(step=30)
        InfoEvent(message="Repair ended").publish()

    def _repair_intensity_feature(self, fault_multiple_nodes):
        InfoEvent(message="Starting C-S write load").publish()
        self.run_prepare_write_cmd()
        InfoEvent(message="Flushing").publish()
        for node in self.db_cluster.nodes:
            node.run_nodetool("flush")
        InfoEvent(message="Waiting for compactions to end").publish()
        self.wait_no_compactions_running(n=30, sleep_time=30)
        InfoEvent(message="Starting C-S read load").publish()
        stress_read_thread = self.generate_background_read_load()
        time.sleep(600)  # So we will see the base load of the cluster
        InfoEvent(message="Sleep ended - Starting tests").publish()
        with self.subTest('test_intensity_and_parallel'):
            self.test_intensity_and_parallel(fault_multiple_nodes=fault_multiple_nodes)
        load_results = stress_read_thread.get_results()
        self.log.info('load={}'.format(load_results))

    def get_email_data(self):
        self.log.info("Prepare data for email")

        email_data = self._get_common_email_data()

        restore_parameters = self.params.get("mgmt_restore_params")
        if restore_parameters:
            restore_parameters = restore_parameters.dict()

        agent_backup_config = self.params.get("mgmt_agent_backup_config")
        if agent_backup_config:
            agent_backup_config = agent_backup_config.dict()

        email_data.update(
            {
                "manager_server_repo": self.params.get("scylla_mgmt_address"),
                "manager_agent_repo": (self.params.get("scylla_mgmt_agent_address") or
                                       self.params.get("scylla_mgmt_address")),
                "backup_time": str(self.manager_test_metrics.backup_time),
                "restore_time": str(self.manager_test_metrics.restore_time),
                "restore_parameters": restore_parameters,
                "agent_backup_config": agent_backup_config,
            }
        )
        return email_data

    def test_restore_multiple_backup_snapshots(self):  # pylint: disable=too-many-locals  # noqa: PLR0914
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        cluster_backend = self.params.get('cluster_backend')
        if cluster_backend != 'aws':
            self.log.error("Test supports only AWS ATM")
            return
        persistent_manager_snapshots_dict = get_persistent_snapshots()
        target_bucket = persistent_manager_snapshots_dict[cluster_backend]["bucket"]
        backup_bucket_backend = self.params.get("backup_bucket_backend")
        location_list = [f"{backup_bucket_backend}:{target_bucket}"]
        confirmation_stress_template = persistent_manager_snapshots_dict[cluster_backend]["confirmation_stress_template"]
        read_stress_list = []
        snapshot_sizes = persistent_manager_snapshots_dict[cluster_backend]["snapshots_sizes"]
        for size in snapshot_sizes:
            number_of_rows = persistent_manager_snapshots_dict[cluster_backend]["snapshots_sizes"][size]["number_of_rows"]
            expected_timeout = persistent_manager_snapshots_dict[cluster_backend]["snapshots_sizes"][size]["expected_timeout"]
            snapshot_dict = persistent_manager_snapshots_dict[cluster_backend]["snapshots_sizes"][size]["snapshots"]
            snapshot_tag = random.choice(list(snapshot_dict.keys()))
            keyspace_name = snapshot_dict[snapshot_tag]["keyspace_name"]

            self.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=snapshot_tag,
                                          timeout=180, restore_schema=True, location_list=location_list)
            self.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=snapshot_tag,
                                          timeout=expected_timeout, restore_data=True, location_list=location_list)
            stress_command = confirmation_stress_template.format(num_of_rows=number_of_rows,
                                                                 keyspace_name=keyspace_name,
                                                                 sequence_start=1,
                                                                 sequence_end=number_of_rows)
            read_stress_list.append(stress_command)
        for stress in read_stress_list:
            read_thread = self.run_stress_thread(stress_cmd=stress, round_robin=False)
            self.verify_stress_thread(cs_thread_pool=read_thread)

    def test_backup_feature(self):
        self.generate_load_and_wait_for_results()
        with self.subTest('Backup Multiple KS\' and Tables'):
            self.test_backup_multiple_ks_tables()
        with self.subTest('Backup to Location with path'):
            self.test_backup_location_with_path()
        with self.subTest('Test Backup Rate Limit'):
            self.test_backup_rate_limit()
        with self.subTest('Test Backup Purge Removes Orphans Files'):
            self.test_backup_purge_removes_orphan_files()
        with self.subTest('Test restore a backup with restore task'):
            self.test_restore_backup_with_task()
        with self.subTest('Test Backup end of space'):  # Preferably at the end
            self.test_enospc_during_backup()
        with self.subTest('Test Restore end of space'):
            self.test_enospc_before_restore()

    def create_ks_and_tables(self, num_ks, num_table):
        # FIXME: beforehand we better change to have RF=1 to avoid restoring content while restoring replica of data
        table_name = []
        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            for keyspace in range(num_ks):
                session.execute(f"CREATE KEYSPACE IF NOT EXISTS ks00{keyspace} "
                                "WITH replication={'class':'NetworkTopologyStrategy', 'replication_factor':1}")
                for table in range(num_table):
                    session.execute(f'CREATE COLUMNFAMILY IF NOT EXISTS ks00{keyspace}.table00{table} '
                                    '(key varchar, c varchar, v varchar, PRIMARY KEY(key, c))')
                    table_name.append(f'ks00{keyspace}.table00{table}')
                    # FIXME: improve the structure + data insertion
                    # can use this function to populate tables better?
                    # self.populate_data_parallel()
        return table_name

    def test_basic_backup(self, ks_names: list = None):
        self.log.info('starting test_basic_backup')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        backup_task = mgr_cluster.create_backup_task(location_list=self.locations)
        backup_task_status = backup_task.wait_and_get_final_status(timeout=1500)
        assert backup_task_status == TaskStatus.DONE, \
            f"Backup task ended in {backup_task_status} instead of {TaskStatus.DONE}"
        self.verify_backup_success(mgr_cluster=mgr_cluster, backup_task=backup_task, ks_names=ks_names)
        self.run_verification_read_stress(ks_names)
        mgr_cluster.delete()  # remove cluster at the end of the test
        self.log.info('finishing test_basic_backup')

    def test_restore_backup_with_task(self, ks_names: list = None):
        self.log.info('starting test_restore_backup_with_task')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        if not ks_names:
            ks_names = ['keyspace1']
        backup_task = mgr_cluster.create_backup_task(location_list=self.locations, keyspace_list=ks_names)
        backup_task_status = backup_task.wait_and_get_final_status(timeout=1500)
        assert backup_task_status == TaskStatus.DONE, \
            f"Backup task ended in {backup_task_status} instead of {TaskStatus.DONE}"
        soft_timeout = 36 * 60
        hard_timeout = 50 * 60
        with adaptive_timeout(Operations.MGMT_REPAIR, self.db_cluster.nodes[0], timeout=soft_timeout):
            self.verify_backup_success(mgr_cluster=mgr_cluster, backup_task=backup_task, ks_names=ks_names,
                                       restore_data_with_task=True, timeout=hard_timeout)
        self.run_verification_read_stress(ks_names)
        mgr_cluster.delete()  # remove cluster at the end of the test
        self.log.info('finishing test_restore_backup_with_task')

    def test_backup_multiple_ks_tables(self):
        self.log.info('starting test_backup_multiple_ks_tables')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        tables = self.create_ks_and_tables(10, 100)
        self.log.debug('tables list = {}'.format(tables))
        # TODO: insert data to those tables
        backup_task = mgr_cluster.create_backup_task(location_list=self.locations)
        backup_task_status = backup_task.wait_and_get_final_status(timeout=1500)
        assert backup_task_status == TaskStatus.DONE, \
            f"Backup task ended in {backup_task_status} instead of {TaskStatus.DONE}"
        self.verify_backup_success(mgr_cluster=mgr_cluster, backup_task=backup_task)
        self.log.info('finishing test_backup_multiple_ks_tables')

    def test_backup_location_with_path(self):
        self.log.info('starting test_backup_location_with_path')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        try:
            mgr_cluster.create_backup_task(location_list=[f'{location}/path_testing/' for location in self.locations])
        except ScyllaManagerError as error:
            self.log.info('Expected to fail - error: {}'.format(error))
        self.log.info('finishing test_backup_location_with_path')

    def test_backup_rate_limit(self):
        self.log.info('starting test_backup_rate_limit')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        rate_limit_list = [f'{dc}:{random.randint(15, 25)}' for dc in self.get_all_dcs_names()]
        self.log.info('rate limit will be {}'.format(rate_limit_list))
        backup_task = mgr_cluster.create_backup_task(location_list=self.locations, rate_limit_list=rate_limit_list)
        task_status = backup_task.wait_and_get_final_status(timeout=18000)
        assert task_status == TaskStatus.DONE, \
            f"Task {backup_task.id} did not end successfully:\n{backup_task.detailed_progress}"
        self.log.info('backup task finished with status {}'.format(task_status))
        # TODO: verify that the rate limit is as set in the cmd
        self.verify_backup_success(mgr_cluster=mgr_cluster, backup_task=backup_task)
        self.log.info('finishing test_backup_rate_limit')

    @staticmethod
    def _get_all_snapshot_files_s3(cluster_id, bucket_name, region_name):
        file_set = set()
        s3_client = boto3.client('s3', region_name=region_name)
        paginator = s3_client.get_paginator('list_objects')
        pages = paginator.paginate(Bucket=bucket_name, Prefix=f'backup/sst/cluster/{cluster_id}')
        for page in pages:
            # No Contents key means that no snapshot file of the cluster exist,
            # probably no backup ran before this function
            if "Contents" in page:
                content_list = page["Contents"]
                file_set.update([item["Key"] for item in content_list])
        return file_set

    @staticmethod
    def _get_all_snapshot_files_gce(cluster_id, bucket_name):
        file_set = set()
        storage_client, _ = get_gce_storage_client()
        blobs = storage_client.list_blobs(bucket_or_name=bucket_name, prefix=f'backup/sst/cluster/{cluster_id}')
        for listing_object in blobs:
            file_set.add(listing_object.name)
        # Unlike S3, if no files match the prefix, no error will occur
        return file_set

    @staticmethod
    def _get_all_snapshot_files_azure(cluster_id, bucket_name):
        file_set = set()
        azure_service = AzureService()
        container_client = azure_service.blob.get_container_client(container=bucket_name)
        dir_listing = container_client.list_blobs(name_starts_with=f'backup/sst/cluster/{cluster_id}')
        for listing_object in dir_listing:
            file_set.add(listing_object.name)
        return file_set

    def _get_all_snapshot_files(self, cluster_id):
        bucket_name = self.params.get('backup_bucket_location').split()[0]
        if self.params.get('backup_bucket_backend') == 's3':
            region_name = self.params.get("backup_bucket_region") or self.params.get("region_name").split()[0]
            return self._get_all_snapshot_files_s3(cluster_id=cluster_id, bucket_name=bucket_name,
                                                   region_name=region_name)
        elif self.params.get('backup_bucket_backend') == 'gcs':
            return self._get_all_snapshot_files_gce(cluster_id=cluster_id, bucket_name=bucket_name)
        elif self.params.get('backup_bucket_backend') == 'azure':
            return self._get_all_snapshot_files_azure(cluster_id=cluster_id, bucket_name=bucket_name)
        else:
            raise ValueError(f'"{self.params.get("backup_bucket_backend")}" not supported')

    def test_backup_purge_removes_orphan_files(self):
        """
        The test stops a backup task mid-upload, so that orphan files will remain in the destination bucket.
        Afterwards, the test reruns the backup task from scratch (with the --no-continue flag, so it's practically
        a new task) and after the task concludes (successfully) the test makes sure the manager has deleted the
        previously mentioned orphan files from the bucket.
        """
        self.log.info('starting test_backup_purge_removes_orphan_files')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        snapshot_file_list_pre_test = self._get_all_snapshot_files(cluster_id=mgr_cluster.id)

        backup_task = mgr_cluster.create_backup_task(location_list=self.locations, retention=1)
        backup_task.wait_for_uploading_stage(step=5)
        backup_task.stop()
        snapshot_file_list_post_task_stopping = self._get_all_snapshot_files(cluster_id=mgr_cluster.id)
        orphan_files_pre_rerun = snapshot_file_list_post_task_stopping.difference(snapshot_file_list_pre_test)
        assert orphan_files_pre_rerun, "SCT could not create orphan snapshots by stopping a backup task"

        # So that the files' names will be different form the previous ones,
        # and they won't simply replace the previous files in the bucket
        for node in self.db_cluster.nodes:
            node.run_nodetool("compact")

        backup_task.start(continue_task=False)
        backup_task.wait_and_get_final_status(step=10)
        snapshot_file_list_post_purge = self._get_all_snapshot_files(cluster_id=mgr_cluster.id)
        orphan_files_post_rerun = snapshot_file_list_post_purge.intersection(orphan_files_pre_rerun)
        assert not orphan_files_post_rerun, "orphan files were not deleted!"

        self.log.info('finishing test_backup_purge_removes_orphan_files')

    def test_client_encryption(self):
        self.log.info('starting test_client_encryption')
        manager_node = self.monitors.nodes[0]
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=manager_node)
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        dict_host_health = mgr_cluster.get_hosts_health()
        for host_health in dict_host_health.values():
            assert host_health.ssl == HostSsl.OFF, "Not all hosts ssl is 'OFF'"

        healthcheck_task = mgr_cluster.get_healthcheck_task()

        self.db_cluster.enable_client_encrypt()

        self.log.info("Create and send client TLS certificate/key to the manager node")
        manager_node.create_node_certificate(cert_file=manager_node.ssl_conf_dir / TLSAssets.CLIENT_CERT,
                                             cert_key=manager_node.ssl_conf_dir / TLSAssets.CLIENT_KEY)
        manager_node.remoter.run(f'mkdir -p {mgmt.cli.SSL_CONF_DIR}')
        manager_node.remoter.send_files(src=str(manager_node.ssl_conf_dir) + '/', dst=str(mgmt.cli.SSL_CONF_DIR))

        # SM caches scylla nodes configuration and the healthcheck svc is independent from the cache updates.
        # Cache is being updated periodically, every 1 minute following the manager config for SCT.
        # We need to wait until SM is aware about the configuration change.
        mgr_cluster.update(
            client_encrypt=True,
            force_non_ssl_session_port=mgr_cluster.sctool.is_minimum_3_2_6_or_snapshot
        )
        time.sleep(90)

        healthcheck_task.wait_for_status(list_status=[TaskStatus.DONE], step=5, timeout=240)
        sleep = 40
        self.log.debug('Sleep {} seconds, waiting for health-check task to run by schedule on first time'.format(sleep))
        time.sleep(sleep)
        self.log.debug("Health-check task history is: {}".format(healthcheck_task.history))
        dict_host_health = mgr_cluster.get_hosts_health()
        for host_health in dict_host_health.values():
            assert host_health.ssl == HostSsl.ON, "Not all hosts ssl is 'ON'"
            assert host_health.status == HostStatus.UP, "Not all hosts status is 'UP'"

        mgr_cluster.delete()  # remove cluster at the end of the test
        self.log.info('finishing test_client_encryption')

    def test_mgmt_cluster_healthcheck(self):
        self.log.info('starting test_mgmt_cluster_healthcheck')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        other_host, other_host_ip = [
            host_data for host_data in self.get_cluster_hosts_with_ips() if
            host_data[1] != self.get_cluster_hosts_ip()[0]][0]
        sleep = 40
        self.log.debug('Sleep {} seconds, waiting for health-check task to run by schedule on first time'.format(sleep))
        time.sleep(sleep)
        healthcheck_task = mgr_cluster.get_healthcheck_task()
        self.log.debug("Health-check task history is: {}".format(healthcheck_task.history))
        dict_host_health = mgr_cluster.get_hosts_health()
        for host_health in dict_host_health.values():
            assert host_health.status == HostStatus.UP, "Not all hosts status is 'UP'"
            assert host_health.rest_status == HostRestStatus.UP, "Not all hosts REST status is 'UP'"
        # Check for sctool status change after scylla-server down
        other_host.stop_scylla_server()
        self.log.debug("Health-check next run is: {}".format(healthcheck_task.next_run))
        self.log.debug('Sleep {} seconds, waiting for health-check task to run after node down'.format(sleep))
        time.sleep(sleep)
        dict_host_health = mgr_cluster.get_hosts_health()
        assert dict_host_health[other_host_ip].status == HostStatus.DOWN, "Host: {} status is not 'DOWN'".format(
            other_host_ip)
        assert dict_host_health[other_host_ip].rest_status == HostRestStatus.DOWN, "Host: {} REST status is not 'DOWN'".format(
            other_host_ip)
        other_host.start_scylla_server()

        mgr_cluster.delete()  # remove cluster at the end of the test
        self.log.info('finishing test_mgmt_cluster_healthcheck')

    def test_healthcheck_change_max_timeout(self):
        """
        New in manager 2.6
        'max_timeout' is new parameter in the scylla manager yaml. It decides the maximum
        amount of time the manager healthcheck function will wait for a ping response before
        it will announce the node as timed out.

        The test sets the timeout as such that the latency of the nodes that exist in the
        local region (us-east-1) will not be longer than the set timeout, and therefore
        will appear as UP in the healthcheck, while the latency of the node that is the
        distant region (us-west-2) will be longer than the set timeout, and therefore the
        healthcheck will report it as TIMEOUT.

        The test makes sure that the healthcheck reports those statuses correctly.
        """
        self.log.info('starting test_healthcheck_change_max_timeout')

        nodes_from_local_dc = self.db_cluster.nodes[:2]
        nodes_from_distant_dc = self.db_cluster.nodes[2:]
        manager_node = self.monitors.nodes[0]
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=manager_node)
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        try:
            reconfigure_scylla_manager(manager_node=manager_node, logger=self.log,
                                       values_to_update=[{"healthcheck": {"max_timeout": "20ms"}}])
            sleep = 40
            self.log.debug('Sleep %s seconds, waiting for health-check task to rerun', sleep)
            time.sleep(sleep)
            dict_host_health = mgr_cluster.get_hosts_health()
            for node in nodes_from_distant_dc:
                assert dict_host_health[node.ip_address].status == HostStatus.TIMEOUT, \
                    f'After setting "max_timeout" to a value shorter than the latency of the distant dc nodes, ' \
                    f'the healthcheck status of {node.ip_address} was not {HostStatus.TIMEOUT} as expected, but ' \
                    f'instead it was {dict_host_health[node.ip_address].status}'
            for node in nodes_from_local_dc:
                assert dict_host_health[node.ip_address].status == HostStatus.UP, \
                    f'After setting "max_timeout" to a value longer than the latency of the local dc nodes, ' \
                    f'the healthcheck status of {node.ip_address} is not {HostStatus.UP} as expected, but ' \
                    f'instead it was {dict_host_health[node.ip_address].status}'
        finally:
            reconfigure_scylla_manager(manager_node=manager_node, logger=self.log, values_to_remove=['healthcheck'])
            mgr_cluster.delete()  # remove cluster at the end of the test
        self.log.info('finishing test_healthcheck_change_max_timeout')

    def test_manager_upgrade(self):
        """
        Test steps:
        1) Run the repair test.
        2) Run manager upgrade to new version of yaml: 'scylla_mgmt_upgrade_to_repo'. (the 'from' version is: 'scylla_mgmt_address').
        """
        self.log.info('starting test_manager_upgrade')
        scylla_mgmt_upgrade_to_repo = self.params.get('scylla_mgmt_upgrade_to_repo')
        manager_node = self.monitors.nodes[0]
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=manager_node)
        selected_host = self.get_cluster_hosts_ip()[0]
        cluster_name = 'mgr_cluster1'
        mgr_cluster = manager_tool.get_cluster(cluster_name=cluster_name) or \
            manager_tool.add_cluster(name=cluster_name, host=selected_host,
                                     auth_token=self.monitors.mgmt_auth_token)
        self.log.info('Running some stress and repair before upgrade')
        self.test_mgmt_repair_nemesis()

        repair_task_list = mgr_cluster.repair_task_list

        manager_from_version = manager_tool.sctool.version
        manager_tool.upgrade(scylla_mgmt_upgrade_to_repo=scylla_mgmt_upgrade_to_repo)

        assert manager_from_version[0] != manager_tool.sctool.version[0], "Manager version not changed after upgrade."
        # verify all repair tasks exist
        for repair_task in repair_task_list:
            self.log.debug("{} status: {}".format(repair_task.id, repair_task.status))

        self.log.info('Running a new repair task after upgrade')
        repair_task = mgr_cluster.create_repair_task()
        self.log.debug("{} status: {}".format(repair_task.id, repair_task.status))
        self.log.info('finishing test_manager_upgrade')

    def test_manager_rollback_upgrade(self):
        """
        Test steps:
        1) Run Upgrade test: scylla_mgmt_address --> scylla_mgmt_upgrade_to_repo
        2) Run manager downgrade to pre-upgrade version as in yaml: 'scylla_mgmt_address'.
        """
        self.log.info('starting test_manager_rollback_upgrade')
        self.test_manager_upgrade()
        scylla_mgmt_address = self.params.get('scylla_mgmt_address')
        manager_node = self.monitors.nodes[0]
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=manager_node)
        manager_from_version = manager_tool.sctool.version
        manager_tool.rollback_upgrade(scylla_mgmt_address=scylla_mgmt_address)
        assert manager_from_version[0] != manager_tool.sctool.version[0], "Manager version not changed after rollback."
        self.log.info('finishing test_manager_rollback_upgrade')

    def test_repair_multiple_keyspace_types(self):  # pylint: disable=invalid-name
        self.log.info('starting test_repair_multiple_keyspace_types')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        self._create_keyspace_and_basic_table(self.NETWORKSTRATEGY_KEYSPACE_NAME, replication_factor=2)
        self._create_keyspace_and_basic_table(self.LOCALSTRATEGY_KEYSPACE_NAME, replication_factor=0)
        repair_task = mgr_cluster.create_repair_task()
        task_final_status = repair_task.wait_and_get_final_status(timeout=7200)
        assert task_final_status == TaskStatus.DONE, 'Task: {} final status is: {}.'.format(repair_task.id,
                                                                                            str(repair_task.status))
        self.log.info('Task: {} is done.'.format(repair_task.id))
        self.log.debug("sctool version is : {}".format(manager_tool.sctool.version))

        expected_keyspaces_to_be_repaired = ["system_distributed", self.NETWORKSTRATEGY_KEYSPACE_NAME]
        if not self.db_cluster.nodes[0].raft.is_consistent_topology_changes_enabled:
            expected_keyspaces_to_be_repaired.append("system_auth")
        self.log.debug("Keyspaces expected to be repaired: {}".format(expected_keyspaces_to_be_repaired))
        per_keyspace_progress = repair_task.per_keyspace_progress
        self.log.info("Looking in the repair output for all of the required keyspaces")
        for keyspace_name in expected_keyspaces_to_be_repaired:
            keyspace_repair_percentage = per_keyspace_progress.get(keyspace_name, None)
            assert keyspace_repair_percentage is not None, \
                "The keyspace {} was not included in the repair!".format(keyspace_name)

            assert keyspace_repair_percentage == 100, \
                "The repair of the keyspace {} stopped at {}%".format(
                    keyspace_name, keyspace_repair_percentage)

        localstrategy_keyspace_percentage = per_keyspace_progress.get(self.LOCALSTRATEGY_KEYSPACE_NAME, None)
        assert localstrategy_keyspace_percentage is None, \
            "The keyspace with the replication strategy of localstrategy was included in repair, when it shouldn't"
        self.log.info("the sctool repair command was completed successfully")

        mgr_cluster.delete()  # remove cluster at the end of the test
        self.log.info('finishing test_repair_multiple_keyspace_types')

    def test_enospc_during_backup(self):
        self.log.info('starting test_enospc_during_backup')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        # deleting previous snapshots so that the current backup will last longer
        previous_backup_tasks = mgr_cluster.backup_task_list
        for backup_task in previous_backup_tasks:
            backup_task.delete_backup_snapshot()

        target_node = self.db_cluster.nodes[1]

        with ignore_no_space_errors(node=target_node):
            try:
                backup_task = mgr_cluster.create_backup_task(location_list=self.locations)
                backup_task.wait_for_uploading_stage()
                backup_task.stop()

                reach_enospc_on_node(target_node=target_node)

                backup_task.start()

                backup_task.wait_and_get_final_status()
                assert backup_task.status == TaskStatus.DONE, "The backup failed to run on a node with no free space," \
                                                              " while it should have had the room for snapshots due " \
                                                              "to the previous run"

            finally:
                clean_enospc_on_node(target_node=target_node, sleep_time=30)
        self.log.info('finishing test_enospc_during_backup')

    def test_enospc_before_restore(self):
        self.log.info('starting test_enospc_before_restore')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)
        backup_task = mgr_cluster.create_backup_task(location_list=self.locations, keyspace_list=["keyspace1"])
        backup_task_status = backup_task.wait_and_get_final_status(timeout=1500)
        assert backup_task_status == TaskStatus.DONE, \
            f"Backup task ended in {backup_task_status} instead of {TaskStatus.DONE}"
        target_node = self.db_cluster.nodes[1]
        with ignore_no_space_errors(node=target_node), ignore_stream_mutation_fragments_errors():
            try:
                reach_enospc_on_node(target_node=target_node)

                snapshot_tag = backup_task.get_snapshot_tag()
                restore_task = mgr_cluster.create_restore_task(restore_data=True, location_list=self.locations,
                                                               snapshot_tag=snapshot_tag)
                final_status = restore_task.wait_and_get_final_status(step=30)

                assert final_status == TaskStatus.ERROR, \
                    f"The restore task is supposed to fail, since node {target_node} lacks the disk space to download" \
                    f"the snapshot files"
                full_progress_string = restore_task.progress_string(parse_table_res=False,
                                                                    is_verify_errorless_result=True).stdout
                assert "not enough disk space" in full_progress_string.lower(), \
                    f"The restore failed as expected when one of the nodes was out of disk space, but with an ill " \
                    f"fitting error message: {full_progress_string}"
            finally:
                clean_enospc_on_node(target_node=target_node, sleep_time=30)
        self.log.info('finishing test_enospc_before_restore')

    def _delete_keyspace_directory(self, db_node, keyspace_name):
        # Stop scylla service before deleting sstables to avoid partial deletion of files that are under compaction
        db_node.stop_scylla_server(verify_up=False, verify_down=True)

        try:
            directoy_path = f"/var/lib/scylla/data/{keyspace_name}"
            directory_size_result = db_node.remoter.sudo(f"du -h --max-depth=0 {directoy_path}")
            result = db_node.remoter.sudo(f'rm -rf {directoy_path}')
            if result.stderr:
                raise FilesNotCorrupted('Files were not corrupted. CorruptThenRepair nemesis can\'t be run. '
                                        'Error: {}'.format(result))
            if directory_size_result.stdout:
                directory_size = directory_size_result.stdout[:directory_size_result.stdout.find("\t")]
                self.log.debug("Removed the directory of keyspace {} from node {}\nThe size of the directory is {}".format(
                    keyspace_name, db_node, directory_size))

        finally:
            db_node.start_scylla_server(verify_up=True, verify_down=False)

    def _insert_data_while_excluding_each_node(self, total_num_of_rows, keyspace_name="keyspace2"):
        """
        The function split the number of rows to the number of nodes (minus 1) and in loop does the following:
        shuts down one node, insert one part of the rows and starts the node again.
        As a result, each node that was shut down will have missing rows and will require repair.
        """
        num_of_nodes = self.params.get("n_db_nodes")
        num_of_rows_per_insertion = int(total_num_of_rows / (num_of_nodes - 1))
        stress_command_template = "cassandra-stress write cl=QUORUM n={} -schema 'keyspace={}" \
                                  " replication(strategy=NetworkTopologyStrategy,replication_factor=3)'" \
                                  " -col 'size=FIXED(1024) n=FIXED(1)' -pop seq={}..{} -mode cql3" \
                                  " native -rate threads=200 -log interval=5"
        start_of_range = 1
        # We can't shut down node 1 since it's the default contact point of the stress command, and we have no way
        # of changing that. As such, we skip it.
        for node in self.db_cluster.nodes[1:]:
            self.log.info("inserting {} rows to every node except {}".format(num_of_rows_per_insertion, node.name))
            end_of_range = start_of_range + num_of_rows_per_insertion - 1
            node.stop_scylla_server(verify_up=False, verify_down=True)
            stress_thread = self.run_stress_thread(stress_cmd=stress_command_template.format(num_of_rows_per_insertion,
                                                                                             keyspace_name,
                                                                                             start_of_range,
                                                                                             end_of_range))
            time.sleep(15)
            self.log.info('load={}'.format(stress_thread.get_results()))
            node.start_scylla_server(verify_up=True, verify_down=False)
            start_of_range = end_of_range + 1
        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            session.execute(f"ALTER TABLE {keyspace_name}.standard1 WITH read_repair_chance = 0.0")

        for node in self.db_cluster.nodes:
            node.run_nodetool("flush")

    def create_missing_rows_in_cluster(self, create_missing_rows_in_multiple_nodes, total_num_of_rows,
                                       keyspace_to_be_repaired=None):
        if create_missing_rows_in_multiple_nodes:
            self._insert_data_while_excluding_each_node(total_num_of_rows=total_num_of_rows,
                                                        keyspace_name=keyspace_to_be_repaired)
            self.wait_no_compactions_running(n=40, sleep_time=10)
        else:
            target_node = self.db_cluster.nodes[2]
            self._delete_keyspace_directory(db_node=target_node, keyspace_name="keyspace1")

    def test_intensity_and_parallel(self, fault_multiple_nodes):
        keyspace_to_be_repaired = "keyspace2"
        InfoEvent(message='starting test_intensity_and_parallel').publish()
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = manager_tool.add_cluster(
            name=self.CLUSTER_NAME + '_intensity_and_parallel',
            db_cluster=self.db_cluster,
            auth_token=self.monitors.mgmt_auth_token,
        )

        InfoEvent(message="Starting faulty load (to be repaired)").publish()
        self.create_missing_rows_in_cluster(create_missing_rows_in_multiple_nodes=fault_multiple_nodes,
                                            keyspace_to_be_repaired=keyspace_to_be_repaired,
                                            total_num_of_rows=29296872)

        InfoEvent(message="Starting a repair with no intensity").publish()
        base_repair_task = mgr_cluster.create_repair_task(keyspace="keyspace*")
        base_repair_task.wait_and_get_final_status(step=30)
        assert base_repair_task.status == TaskStatus.DONE, "The base repair task did not end in the expected time"
        InfoEvent(message=f"The base repair, with no intensity argument, took {base_repair_task.duration}").publish()

        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            session.execute(f"DROP KEYSPACE IF EXISTS {keyspace_to_be_repaired}")

        arg_list = [{"intensity": .5},
                    {"intensity": .25},
                    {"intensity": .0001},
                    {"intensity": 2},
                    {"intensity": 4},
                    {"parallel": 1},
                    {"parallel": 2},
                    {"intensity": 2, "parallel": 1},
                    {"intensity": 100},
                    {"intensity": 0}]

        for arg_dict in arg_list:
            InfoEvent(message="Starting faulty load (to be repaired)").publish()
            self.create_missing_rows_in_cluster(create_missing_rows_in_multiple_nodes=fault_multiple_nodes,
                                                keyspace_to_be_repaired=keyspace_to_be_repaired,
                                                total_num_of_rows=29296872)

            InfoEvent(message=f"Starting a repair with {arg_dict}").publish()
            repair_task = mgr_cluster.create_repair_task(**arg_dict, keyspace="keyspace*")
            repair_task.wait_and_get_final_status(step=30)
            InfoEvent(message=f"repair with {arg_dict} took {repair_task.duration}").publish()

            with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
                session.execute(f"DROP KEYSPACE IF EXISTS {keyspace_to_be_repaired}")
        InfoEvent(message='finishing test_intensity_and_parallel').publish()

    def test_suspend_and_resume(self):
        with self.subTest('Suspend and resume backup task'):
            self._suspend_and_resume_task_template(task_type="backup")
        with self.subTest('Suspend and resume repair task'):
            self._suspend_and_resume_task_template(task_type="repair")
        with self.subTest('Suspend and resume without starting task'):
            self.test_suspend_and_resume_without_starting_tasks()
        with self.subTest('Suspend with on resume start tasks flag after duration has passed'):
            self._template_suspend_with_on_resume_start_tasks_flag(wait_for_duration=True)
        with self.subTest('Suspend with on resume start tasks flag before duration has passed'):
            self._template_suspend_with_on_resume_start_tasks_flag(wait_for_duration=False)

    def _suspend_and_resume_task_template(self, task_type):
        # task types: backup/repair
        self.log.info('starting test_suspend_and_resume_{}'.format(task_type))
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        # re-add the cluster to make the backup task run from scratch, otherwise it may be very fast and
        # the test is not able to catch the required statuses
        mgr_cluster = self._ensure_and_get_cluster(manager_tool, force_add=True)
        if task_type == "backup":
            suspendable_task = mgr_cluster.create_backup_task(location_list=self.locations)
        elif task_type == "repair":
            suspendable_task = mgr_cluster.create_repair_task()
        else:
            raise ValueError(f"Not familiar with task type: {task_type}")
        assert suspendable_task.wait_for_status(list_status=[TaskStatus.RUNNING], timeout=300, step=5), \
            f"task {suspendable_task.id} failed to reach status {TaskStatus.RUNNING}"
        with mgr_cluster.suspend_manager_then_resume(start_tasks=True):
            assert suspendable_task.wait_for_status(list_status=[TaskStatus.STOPPED], timeout=300, step=10), \
                f"task {suspendable_task.id} failed to reach status {TaskStatus.STOPPED}"
        assert suspendable_task.wait_for_status(list_status=[TaskStatus.DONE], timeout=1200, step=10), \
            f"task {suspendable_task.id} failed to reach status {TaskStatus.DONE}"
        self.log.info('finishing test_suspend_and_resume_{}'.format(task_type))

    def _template_suspend_with_on_resume_start_tasks_flag(self, wait_for_duration):
        suspension_duration = 75
        test_name_filler = "after_duration_passed" if wait_for_duration else "before_duration_passed"
        self.log.info('starting test_suspend_with_on_resume_start_tasks_flag_{}'.format(test_name_filler))
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        # re-add the cluster to make the backup task run from scratch, otherwise it may run very fast and
        # the test won't be able to catch the required statuses
        mgr_cluster = self._ensure_and_get_cluster(manager_tool, force_add=True)
        task_type = random.choice(["backup", "repair"])
        if task_type == "backup":
            suspendable_task = mgr_cluster.create_backup_task(location_list=self.locations)
        else:
            suspendable_task = mgr_cluster.create_repair_task()
        assert suspendable_task.wait_for_status(list_status=[TaskStatus.RUNNING], timeout=300, step=5), \
            f"task {suspendable_task.id} failed to reach status {TaskStatus.RUNNING}"
        with mgr_cluster.suspend_manager_then_resume(start_tasks=False, start_tasks_in_advance=True,
                                                     duration=f"{suspension_duration}s"):
            assert suspendable_task.wait_for_status(list_status=[TaskStatus.STOPPED], timeout=60, step=2), \
                f"task {suspendable_task.id} failed to reach status {TaskStatus.STOPPED}"
            if wait_for_duration:  # Whether waiting for the duration time to pass or not
                time.sleep(suspension_duration+5)
        if wait_for_duration:
            assert suspendable_task.wait_for_status(list_status=[TaskStatus.DONE], timeout=1200, step=10), \
                f"After the cluster was resumed (while resuming AFTER the suspend duration has passed)," \
                f" task {suspendable_task.id} failed to reach status " \
                f"{TaskStatus.DONE}, but instead stayed in {suspendable_task.status}"
        else:
            assert suspendable_task.status == TaskStatus.STOPPED, \
                "After the cluster was resumed (while resuming BEFORE the suspend duration "\
                f"has passed), task {suspendable_task.id} failed to stay in status STOPPED"
            time.sleep(suspension_duration + 5)
            assert suspendable_task.status == TaskStatus.STOPPED, \
                "After the cluster was resumed (while resuming BEFORE the suspend duration "\
                f"has passed), task {suspendable_task.id} failed to stay in status STOPPED after suspension time ended"
        self.log.info('finishing test_suspend_with_on_resume_start_tasks_flag_{}'.format(test_name_filler))

    def test_suspend_and_resume_without_starting_tasks(self):
        self.log.info('starting test_suspend_and_resume_without_starting_tasks')
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        # re-add the cluster to make the backup task run from scratch, otherwise it may be very fast and
        # the test is not able to catch the required statuses
        mgr_cluster = self._ensure_and_get_cluster(manager_tool, force_add=True)
        suspendable_task = mgr_cluster.create_backup_task(location_list=self.locations)
        assert suspendable_task.wait_for_status(list_status=[TaskStatus.RUNNING], timeout=300, step=5), \
            f"task {suspendable_task.id} failed to reach status {TaskStatus.RUNNING}"
        with mgr_cluster.suspend_manager_then_resume(start_tasks=False):
            mgr_cluster.suspend()
            assert suspendable_task.wait_for_status(list_status=[TaskStatus.STOPPED], timeout=300, step=10), \
                f"task {suspendable_task.id} failed to reach status {TaskStatus.STOPPED}"
            mgr_cluster.resume(start_tasks=False)
            self.log.info("Waiting a little time to make sure the task isn't started")
            time.sleep(60)
            current_task_status = suspendable_task.status
            assert current_task_status == TaskStatus.STOPPED, \
                f'Task {current_task_status} did not remain in "{TaskStatus.STOPPED}" status, but instead ' \
                f'reached "{current_task_status}" status'
        self.log.info('finishing test_suspend_and_resume_without_starting_tasks')

    def test_manager_installed_and_functional(self):
        """Verify that the Manager is installed and functional.

        The test is intended for execution on non-main OS distributions (for example, debian10)
        where the main goal is to execute installation test.
        The rest of the checks are executed on Ubuntu distribution in test_manager_sanity.
        """
        self.log.info('starting test_manager_installed_and_functional')

        manager_node = self.monitors.nodes[0]
        scylla_node = self.db_cluster.nodes[0]
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=manager_node)

        # Check scylla-manager and scylla-manager-agent are up and running (/ping endpoint)
        manager_node.is_manager_server_up()
        scylla_node.is_manager_agent_up()

        # Check the sctool version method is callable
        self.log.info("Got Manager's version: %s", manager_tool.sctool.version)

        # Add cluster and verify hosts health
        mgr_cluster = manager_tool.add_cluster(name=self.CLUSTER_NAME, db_cluster=self.db_cluster,
                                               auth_token=self.monitors.mgmt_auth_token)
        dict_host_health = mgr_cluster.get_hosts_health()
        for host_health in dict_host_health.values():
            assert host_health.status == HostStatus.UP, "Host status is not 'UP'"
            assert host_health.rest_status == HostRestStatus.UP, "Host REST status is not 'UP'"

        self.log.info('finishing test_manager_installed_and_functional')

    # ----- RESTORE -----

    @staticmethod
    def get_snapshot_data(snapshot_name: str):
        snapshots_config = "defaults/manager_restore_benchmark_snapshots.yaml"
        with open(snapshots_config, encoding="utf-8") as snapshots_yaml:
            all_snapshots_dict = yaml.safe_load(snapshots_yaml)

        try:
            snapshot_dict = all_snapshots_dict["sizes"][snapshot_name]
        except KeyError:
            raise ValueError(f"Snapshot data for size '{snapshot_name}'GB was not found in the {snapshots_config} file")

        ks_tables_map = {}
        for ks, ts in snapshot_dict["schema"].items():
            t_names = [list(t.keys())[0] for t in ts]
            ks_tables_map[ks] = t_names

        snapshot_data = SnapshotData(
            bucket=all_snapshots_dict["bucket"],
            tag=snapshot_dict["tag"],
            exp_timeout=snapshot_dict["exp_timeout"],
            keyspaces=list(snapshot_dict["schema"].keys()),
            ks_tables_map=ks_tables_map,
            cs_read_cmd_template=all_snapshots_dict["cs_read_cmd_template"],
            prohibit_verification_read=snapshot_dict["prohibit_verification_read"],
            number_of_rows=snapshot_dict["number_of_rows"],
            node_ids=snapshot_dict.get("node_ids"),
        )
        return snapshot_data

    def prepare_stress_read_cmd(self, command_template, keyspace_name, number_of_rows):
        stress_queue = []
        number_of_loaders = self.params.get("n_loaders")
        rows_per_loader = int(number_of_rows / number_of_loaders)
        for loader_index in range(number_of_loaders):
            stress_command = command_template.format(num_of_rows=rows_per_loader,
                                                     keyspace_name=keyspace_name,
                                                     sequence_start=rows_per_loader * loader_index + 1,
                                                     sequence_end=rows_per_loader * (loader_index + 1),)
            read_thread = self.run_stress_thread(stress_cmd=stress_command, round_robin=True,
                                                 stop_test_on_failure=False)
            stress_queue.append(read_thread)
        return stress_queue

    def get_restore_custom_parameters(self):
        if restore_params := self.params.get('mgmt_restore_params'):
            batch_size = restore_params.batch_size
            parallel = restore_params.parallel
        else:
            batch_size, parallel = None, None
        return batch_size, parallel

    def test_backup_and_restore_only_data(self):
        """The test is extensively used for restore benchmarking purposes and consists of the following steps:
        1. Populate the cluster with data (currently operates with datasets of 500GB, 1TB, 2TB, 5TB);
        2. Run the backup task and wait for its completion;
        3. Truncate the tables in the cluster;
        4. Run the restore task (custom batch_size and parallel params can be set in the pipeline)
           and wait for its completion;
        5. Run the verification read stress to ensure the data is restored correctly.
        """
        self.run_prepare_write_cmd()
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)

        backup_task = mgr_cluster.create_backup_task(location_list=self.locations, rate_limit_list=["0"])
        backup_task_status = backup_task.wait_and_get_final_status(timeout=200000)
        assert backup_task_status == TaskStatus.DONE, \
            f"Backup task ended in {backup_task_status} instead of {TaskStatus.DONE}"
        InfoEvent(message=f'The backup task has ended successfully. Backup run time: {backup_task.duration}').publish()
        self.manager_test_metrics.backup_time = backup_task.duration

        ks_number = self.params.get('keyspace_num') or 1
        ks_names = self.get_keyspace_name(ks_number=ks_number)
        for ks_name in ks_names:
            self.db_cluster.nodes[0].run_cqlsh(f'TRUNCATE {ks_name}.standard1')

        batch_size, parallel = self.get_restore_custom_parameters()
        task = self.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=backup_task.get_snapshot_tag(),
                                             timeout=110000, restore_data=True, batch_size=batch_size,
                                             parallel=parallel)
        self.manager_test_metrics.restore_time = task.duration

        self.run_verification_read_stress()

    def test_restore_from_precreated_backup(self, snapshot_name: str,
                                            restore_outside_manager: RestoreOutsideManager | None = None):
        """The test restores the schema and data from a pre-created backup and runs the verification read stress.
        1. Define the backup to restore from
        2. Run restore schema to empty cluster
        3. Run restore data
        4. Run verification read stress

        Args:
            snapshot_name: The name of the snapshot to restore from.
                           All snapshots are defined in the 'defaults/manager_restore_benchmark_snapshots.yaml'
            restore_outside_manager: restore outside of Manager configuration, set None to restore via Manager
        """
        manager_tool = mgmt.get_scylla_manager_tool(manager_node=self.monitors.nodes[0])
        mgr_cluster = self._ensure_and_get_cluster(manager_tool)

        snapshot_data = self.get_snapshot_data(snapshot_name)

        self.log.info("Restoring the schema")
        location = [f"s3:{snapshot_data.bucket}"]
        self.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=snapshot_data.tag, timeout=600,
                                      restore_schema=True, location_list=location)
        for ks_name in snapshot_data.keyspaces:
            self.set_ks_strategy_to_network_and_rf_according_to_cluster(keyspace=ks_name, repair_after_alter=False)

        if restore_outside_manager:
            self.log.info("Restoring the data outside the Manager")
            self.restore_backup_outside_of_manager(
                mgr_cluster=mgr_cluster,
                snapshot_tag=snapshot_data.tag,
                ks_tables_list=snapshot_data.ks_tables_map,
                load_and_stream=restore_outside_manager.load_and_stream,
                primary_replica_only=restore_outside_manager.primary_replica_only,
                location=location[0],
                node_ids=snapshot_data.node_ids,
            )
        else:
            self.log.info("Restoring the data with Manager task")
            batch_size, parallel = self.get_restore_custom_parameters()
            task = self.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=snapshot_data.tag,
                                                 timeout=snapshot_data.exp_timeout, restore_data=True,
                                                 location_list=location, batch_size=batch_size, parallel=parallel)
            self.manager_test_metrics.restore_time = task.duration

        if not (self.params.get('mgmt_ignore_post_restore_stress_read') or snapshot_data.prohibit_verification_read):
            self.log.info("Running verification read stress")
            stress_queue = self.prepare_stress_read_cmd(command_template=snapshot_data.cs_read_cmd_template,
                                                        keyspace_name=snapshot_data.keyspaces[0],
                                                        number_of_rows=snapshot_data.number_of_rows)
            for stress in stress_queue:
                assert self.verify_stress_thread(cs_thread_pool=stress), "Data verification stress command"
        else:
            self.log.info(f"Skipping verification read stress because of the test or snapshot configuration")

    def test_restore_benchmark(self):
        """Benchmark restore operation.

        The test suggests two flows - populate the cluster with data, create the backup, and then restore it or
        restore from a pre-created backup.
        """
        if reuse_snapshot_name := self.params.get('mgmt_reuse_backup_snapshot'):
            self.log.info("Executing test_restore_from_precreated_backup...")
            self.test_restore_from_precreated_backup(reuse_snapshot_name)
        else:
            self.log.info("Executing test_backup_and_restore_only_data...")
            self.test_backup_and_restore_only_data()

    def test_restore_data_outside_of_manager(self):
        """The test restores the schema and data from a pre-created backup.
        The distinctive feature is that data restore is performed outside the Manager via nodetool refresh.
        The test covers two nodetool refresh running options: with and w/o --load-and-stream and --primary-replica-only.
        The required mode can be chosen by setting `mgmt_nodetool_refresh_with_las_and_replica_flags`.

        The motivation of having such a test is to check L&S efficiency when doing the restore of the full cluster in
        comparison with the same test but with restore executed via Manager.
        """
        snapshot_name = self.params.get('mgmt_reuse_backup_snapshot')
        assert snapshot_name, ("The test requires a pre-created snapshot to restore from. "
                               "Please provide the 'mgmt_reuse_backup_snapshot' parameter.")

        with_las_and_replica_flags = self.params.get('mgmt_nodetool_refresh_with_las_and_replica_flags')
        if with_las_and_replica_flags:
            self.log.info('Restore with nodetool refresh + --load-and-stream and --primary-replica-only')
            restore_outside_manager = RestoreOutsideManager(load_and_stream=True, primary_replica_only=True)
        else:
            self.log.info('Restore with nodetool refresh')
            restore_outside_manager = RestoreOutsideManager()

        self.test_restore_from_precreated_backup(snapshot_name, restore_outside_manager=restore_outside_manager)
