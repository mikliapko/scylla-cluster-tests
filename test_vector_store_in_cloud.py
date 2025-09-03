import ast
import random
import time

from mgmt_cli_test import ManagerTestFunctionsMixIn
from sdcm.tester import ClusterTester
from sdcm.utils.common import S3Storage
from sdcm.utils.file import File

# Snapshot of dataset for Vector Store testing
AWS_LOCATIONS = ["us-east-1:s3:scylla-cloud-backup-vector-50k-1536dim"]
GCP_LOCATIONS = ["us-east1:gs:scylla-cloud-backup-vector-50k-1536dim"]
SNAPSHOT_TAG = "sm_20250903104929UTC"

# Vector Store test data files
BUCKET_NAME = "vector-store-in-cloud"
TEST_DATA_FILENAME = "test_data.txt"
GROUND_TRUTH_FILENAME = "ground_truth.txt"
DATA_DIR_PATH = "data_dir/vector_store/"
TEST_DATA_FILE_PATH = DATA_DIR_PATH + TEST_DATA_FILENAME
GROUND_TRUTH_FILE_PATH = DATA_DIR_PATH + GROUND_TRUTH_FILENAME


class VectorStoreInCloud(ClusterTester):
    @property
    def locations(self) -> list[str]:
        if self.params.get("cluster_backend") == "aws":
            return AWS_LOCATIONS
        elif self.params.get("cluster_backend") == "gcp":
            return GCP_LOCATIONS
        else:
            raise ValueError(f"Unsupported cluster backend {self.params.get('cluster_backend')}")

    @staticmethod
    def _download_vector_store_test_data_from_s3():
        s3_storage = S3Storage(bucket=BUCKET_NAME)

        base_url = f"https://{BUCKET_NAME}.s3.amazonaws.com"
        s3_storage.download_file(link=f"{base_url}/{TEST_DATA_FILENAME}", dst_dir=DATA_DIR_PATH)
        s3_storage.download_file(link=f"{base_url}/{GROUND_TRUTH_FILENAME}", dst_dir=DATA_DIR_PATH)

    def run_vector_store_verification_queries(self):
        self._download_vector_store_test_data_from_s3()

        test_data = File(TEST_DATA_FILE_PATH).readlines()
        ground_truth = File(GROUND_TRUTH_FILE_PATH).readlines()
        assert len(test_data) == len(ground_truth), \
            "Test data and ground truth files have different lengths, can't proceed with verification queries"

        cql_cmd_template = "SELECT id FROM vdb_bench.vdb_bench_collection ORDER BY vector ANN OF {vector} LIMIT 10;"
        for query_num in range(1, 11):
            self.log.debug("Running verification request %d", query_num)
            index = random.randint(0, len(test_data) - 1)
            vector_value = test_data[index]
            ground_truth_ids = ast.literal_eval(ground_truth[index])

            with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
                query = cql_cmd_template.format(vector=vector_value)
                rows = session.execute(query).all()

            for idx, row in enumerate(rows):
                assert row.id in ground_truth_ids[:10], \
                    f"Row ID {row.id} at position {idx} not found amongst the first 10 expected ids {ground_truth_ids}"

    def test_vector_store_in_cloud(self):
        manager_operations = ManagerTestFunctionsMixIn()

        self.log.info("Initialize Scylla Manager")
        mgr_cluster = self.db_cluster.get_cluster_manager()

        if self.params.get("use_cloud_manager"):
            self.log.info("Delete scheduled backup task to not interfere")
            auto_backup_task = mgr_cluster.backup_task_list[0]
            mgr_cluster.delete_task(auto_backup_task)

            self.log.info("Adjust restore cluster backup policy")
            if self.params.get("cluster_backend") == "aws":
                manager_operations.adjust_aws_restore_policy(locations=self.locations,
                                                             cluster_id=self.db_cluster.cloud_cluster_id)

            self.log.info("Grant admin permissions to scylla_manager user")
            self.db_cluster.nodes[0].run_cqlsh(cmd="grant scylla_admin to scylla_manager")

        self.log.info("Restoring the schema")
        manager_operations.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=SNAPSHOT_TAG,
                                                    timeout=120, restore_schema=True, location_list=self.locations)

        self.log.info("Restoring the data")
        manager_operations.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=SNAPSHOT_TAG,
                                                    timeout=600, restore_data=True, location_list=self.locations)

        self.log.info("Create Vector Index")
        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            query = ("CREATE CUSTOM INDEX vdb_bench_collection_vector_idx ON vdb_bench.vdb_bench_collection(vector) "
                     "USING 'vector_index'")
            session.execute(query)

        # TODO: rework when it'd be possible to check index build status via API
        self.log.info("Sleep 120 seconds for indexes to be built")
        time.sleep(120)

        self.log.info("Run Vector Store verification queries")
        self.run_vector_store_verification_queries()
