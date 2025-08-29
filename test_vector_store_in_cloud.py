import ast
import random
import time

from mgmt_cli_test import ManagerTestFunctionsMixIn
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


class VectorStoreInCloud(ManagerTestFunctionsMixIn):
    average_recall: float = 0.0

    def get_email_data(self):
        self.log.info("Prepare data for email")

        email_data = self._get_common_email_data()
        email_data.update({"average_recall": self.average_recall})

        return email_data

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

    def run_vector_store_verification(self):
        self._download_vector_store_test_data_from_s3()

        test_data = File(TEST_DATA_FILE_PATH).readlines()
        ground_truth = File(GROUND_TRUTH_FILE_PATH).readlines()
        assert len(test_data) == len(ground_truth), \
            "Test data and ground truth files have different lengths, can't proceed with verification queries"

        total_recall = 0
        total_queries = 100

        cql_cmd_template = "SELECT id FROM vdb_bench.vdb_bench_collection ORDER BY vector ANN OF {vector} LIMIT 10;"
        for query_num in range(1, total_queries + 1):
            self.log.debug("Running verification request #%d", query_num)
            index = random.randint(0, len(test_data) - 1)
            vector_value = test_data[index]

            with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
                query = cql_cmd_template.format(vector=vector_value)
                rows = session.execute(query).all()

            actual_ids = [row.id for row in rows]
            ground_truth_ids = ast.literal_eval(ground_truth[index])[:10]
            assert len(actual_ids) == len(ground_truth_ids), \
                f"Query #{query_num}: Mismatch in number of returned IDs ({len(actual_ids)}) " \
                f"and ground truth IDs ({len(ground_truth_ids)})"

            # Count how many elements from actual_ids are present in ground_truth_ids (order doesn't matter)
            elements_present = sum(1 for actual_id in actual_ids if actual_id in ground_truth_ids)

            query_recall = elements_present / len(actual_ids)
            total_recall += query_recall

            self.log.debug("Query #%d: %d/%d elements present, recall factor: %.2f",
                           query_num, elements_present, len(actual_ids), query_recall)

        average_recall = round(total_recall / total_queries, 3)
        self.average_recall = average_recall

        self.log.info("Average recall for %d verification queries: %f", total_queries, average_recall)
        assert average_recall > 0.85, f"Average recall {average_recall} is below the expected threshold of 0.85"

    def test_vector_store_in_cloud(self):
        self.log.info("Initialize Scylla Manager")
        mgr_cluster = self.db_cluster.get_cluster_manager()

        self.log.info("Delete scheduled backup task to not interfere")
        auto_backup_task = mgr_cluster.backup_task_list[0]
        mgr_cluster.delete_task(auto_backup_task)

        self.log.info("Adjust restore cluster backup policy")
        if self.params.get("cluster_backend") == "aws":
            self.adjust_aws_restore_policy(locations=self.locations, cluster_id=self.db_cluster.cloud_cluster_id)

        self.log.info("Grant admin permissions to scylla_manager user")
        self.db_cluster.nodes[0].run_cqlsh(cmd="grant scylla_admin to scylla_manager")

        time.sleep(30)

        self.log.info("Restoring the schema")
        self.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=SNAPSHOT_TAG,
                                      timeout=120, restore_schema=True, location_list=self.locations)

        self.log.info("Restoring the data")
        self.restore_backup_with_task(mgr_cluster=mgr_cluster, snapshot_tag=SNAPSHOT_TAG,
                                      timeout=600, restore_data=True, location_list=self.locations)

        self.log.info("Create Vector Index")
        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            query = ("CREATE CUSTOM INDEX vdb_bench_collection_vector_idx ON vdb_bench.vdb_bench_collection(vector) "
                     "USING 'vector_index'")
            session.execute(query)

        # TODO: rework when it'll be possible to check index readiness status
        self.log.info("Sleep 120 seconds for indexes to be built")
        time.sleep(120)

        self.log.info("Run Vector Store verification queries")
        self.run_vector_store_verification()
