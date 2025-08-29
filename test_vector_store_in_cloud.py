import ast
import random
import time

from mgmt_cli_test import ManagerTestFunctionsMixIn
from sdcm.mgmt.helpers import get_schema_create_statements_from_file, substitute_dc_name_in_ks_statements_if_different
from sdcm.utils.common import S3Storage
from sdcm.utils.file import File


# Vector Store test data files
BUCKET_NAME = "vector-store-in-cloud"

TEST_DATA_FILENAME = "test_data.txt"
GROUND_TRUTH_FILENAME = "ground_truth.txt"
DATASET_FILENAME = "dataset.txt"
SCHEMA_FILENAME = "schema.json"

DATA_DIR_PATH = "data_dir/vector_store/"
TEST_DATA_FILE_PATH = DATA_DIR_PATH + TEST_DATA_FILENAME
GROUND_TRUTH_FILE_PATH = DATA_DIR_PATH + GROUND_TRUTH_FILENAME
DATASET_FILE_PATH = DATA_DIR_PATH + DATASET_FILENAME
SCHEMA_FILE_PATH = DATA_DIR_PATH + SCHEMA_FILENAME


class VectorStoreInCloud(ManagerTestFunctionsMixIn):
    average_recall: float = 0.0

    def get_email_data(self):
        self.log.info("Prepare data for email")

        email_data = self._get_common_email_data()
        email_data.update({"average_recall": self.average_recall})

        return email_data

    @staticmethod
    def download_vector_store_test_data_from_s3(include_dataset: bool = True):
        s3_storage = S3Storage(bucket=BUCKET_NAME)

        base_url = f"https://{BUCKET_NAME}.s3.amazonaws.com"
        s3_storage.download_file(link=f"{base_url}/{TEST_DATA_FILENAME}", dst_dir=DATA_DIR_PATH)
        s3_storage.download_file(link=f"{base_url}/{GROUND_TRUTH_FILENAME}", dst_dir=DATA_DIR_PATH)

        if include_dataset:
            s3_storage.download_file(link=f"{base_url}/{SCHEMA_FILENAME}", dst_dir=DATA_DIR_PATH)
            s3_storage.download_file(link=f"{base_url}/{DATASET_FILENAME}", dst_dir=DATA_DIR_PATH)

    def run_vector_store_verification(self):
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

    def test_vector_store_in_cloud(self, restore_from_backup: bool = False):
        self.log.info("Download Vector Store test data from S3")
        include_dataset = not restore_from_backup
        self.download_vector_store_test_data_from_s3(include_dataset=include_dataset)

        self.log.info("Create schema applying CQL statements")
        ks_statements, other_statements = get_schema_create_statements_from_file(SCHEMA_FILE_PATH)

        # DC name written in the schema file may differ from the one used in the cluster under test
        # if run in region or cloud provider different from ones where backup was taken
        dc_under_test_name = next(iter(self.db_cluster.get_nodetool_status()))
        ks_statements = substitute_dc_name_in_ks_statements_if_different(ks_statements=ks_statements,
                                                                         current_dc_name=dc_under_test_name)

        with self.db_cluster.cql_connection_patient(node=self.db_cluster.nodes[0], verbose=False) as session:
            for cql_stmt in ks_statements + other_statements:
                session.execute(cql_stmt)

            self.log.info("Insert sample data into the table")
            insert_cmd_template = "INSERT INTO vdb_bench.vdb_bench_collection (id, vector) VALUES ({id}, {vector});"
            with open(DATASET_FILE_PATH, encoding="utf-8") as dataset_file:
                for line in dataset_file:
                    id_str, vector_str = line.strip().split(",", maxsplit=1)
                    insert_cmd = insert_cmd_template.format(id=int(id_str), vector=vector_str)
                    session.execute(insert_cmd)

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
