import ast
import random
import time
from functools import cached_property
from threading import Thread
from typing import Optional

from sdcm.mgmt.helpers import get_schema_create_statements_from_file, substitute_dc_name_in_ks_statements_if_different
from sdcm.tester import ClusterTester
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

ANN_SEARCH_CMD_TEMPLATE = "SELECT id FROM vdb_bench.vdb_bench_collection ORDER BY vector ANN OF {vector} LIMIT 10;"


class VectorStoreInCloudBase(ClusterTester):
    average_recall: float = 0.0

    def get_email_data(self):
        self.log.info("Prepare data for email")

        email_data = self._get_common_email_data()
        email_data.update({"average_recall": self.average_recall})

        return email_data

    @staticmethod
    def download_vector_store_test_data_from_s3():
        s3_storage = S3Storage(bucket=BUCKET_NAME)

        base_url = f"https://{BUCKET_NAME}.s3.amazonaws.com"
        s3_storage.download_file(link=f"{base_url}/{TEST_DATA_FILENAME}", dst_dir=DATA_DIR_PATH)
        s3_storage.download_file(link=f"{base_url}/{GROUND_TRUTH_FILENAME}", dst_dir=DATA_DIR_PATH)

        s3_storage.download_file(link=f"{base_url}/{SCHEMA_FILENAME}", dst_dir=DATA_DIR_PATH)
        s3_storage.download_file(link=f"{base_url}/{DATASET_FILENAME}", dst_dir=DATA_DIR_PATH)

    @cached_property
    def test_data(self):
        return File(TEST_DATA_FILE_PATH).readlines()

    @cached_property
    def ground_truth_data(self):
        return File(GROUND_TRUTH_FILE_PATH).readlines()

    def prepare_vector_store_index(self):
        self.log.info("Download Vector Store test data from S3")
        self.download_vector_store_test_data_from_s3()

        self.log.info("Create schema applying CQL statements")
        ks_statements, other_statements = get_schema_create_statements_from_file(schema_file_path=SCHEMA_FILE_PATH)

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

    def test_vector_store_recall(self, queries_num: Optional[int] = None, duration: Optional[int] = None):

        total_recall = 0

        def _run_ann_search():
            nonlocal total_recall
            index = random.randint(0, len(self.test_data) - 1)
            vector_value = self.test_data[index]

            with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
                query = ANN_SEARCH_CMD_TEMPLATE.format(vector=vector_value)
                self.log.debug("Running verification request #%d", query_num)
                rows = session.execute(query).all()

            actual_ids = [row.id for row in rows]
            ground_truth_ids = ast.literal_eval(self.ground_truth_data[index])[:10]
            assert len(actual_ids) == len(ground_truth_ids), \
                f"Mismatch in number of returned IDs ({len(actual_ids)}) and ground truth IDs ({len(ground_truth_ids)})"

            # Count how many elements from actual_ids are present in ground_truth_ids (order doesn't matter)
            elements_present = sum(1 for actual_id in actual_ids if actual_id in ground_truth_ids)

            query_recall = elements_present / len(actual_ids)
            total_recall += query_recall

            self.log.debug("Query #%d: %d/%d elements present, recall factor: %.2f",
                           query_num, elements_present, len(actual_ids), query_recall)

        assert (queries_num is not None) ^ (duration is not None), \
            "Either queries_num or duration must be provided, but not both"

        if queries_num is not None:
            for query_num in range(1, queries_num + 1):
                _run_ann_search()

            overall_queries_num = queries_num
        else:
            query_num = 0
            end_time = time.time() + duration
            while time.time() < end_time:
                query_num += 1
                _run_ann_search()

            overall_queries_num = query_num

        average_recall = round(total_recall / overall_queries_num, 3)
        self.average_recall = average_recall

        self.log.info("Average recall for %d verification queries: %f", overall_queries_num, average_recall)
        assert average_recall > 0.85, f"Average recall {average_recall} is below the expected threshold of 0.85"


class VectorStoreInCloudSanity(VectorStoreInCloudBase):
    def test_vector_store_sanity(self):
        self.log.info("Prepare Vector Store data")
        self.prepare_vector_store_index()

        self.log.info("Run Vector Store verification queries")
        self.test_vector_store_recall(queries_num=100)


class VectorStoreInCloudReplaceNode(VectorStoreInCloudBase):
    def test_replace_node_no_interruptions_expected(self,
                                                    instance_type_id: Optional[int] = None,
                                                    version: Optional[str] = None):
        self.log.debug("Start Vector Store verification queries in background thread")
        recall_test_thread = Thread(target=self.test_vector_store_recall, kwargs={"duration": 300})
        recall_test_thread.start()

        server_id = random.choice(self.db_cluster.define_vector_store_node_ids())
        self.log.debug("Replace Vector Store node %s - new instance type ID %s, new version %s",
                       server_id, instance_type_id, version)
        self.db_cluster.replace_vector_store_node(
            server_id=server_id,
            instance_type_id=instance_type_id,
            service_version=version,
        )

        recall_test_thread.join(timeout=300)

    def test_replace_node_broken_instance(self):
        vector_value = random.choice(self.test_data)
        query = ANN_SEARCH_CMD_TEMPLATE.format(vector=vector_value)

        # It will be run in background while node replacement is in progress
        with self.db_cluster.cql_connection_patient(self.db_cluster.nodes[0]) as session:
            session.execute(query)
            time.sleep(1)

    def test_vector_store_replace_node_in_cloud(self):
        self.log.info("Prepare Vector Store data")
        self.prepare_vector_store_index()

        self.log.info("Run Vector Store verification BEFORE replacing a node")
        self.test_vector_store_recall(queries_num=10)

        self.log.info("Run VS node replacement, no changes to instance type or version")
        self.test_replace_node_no_interruptions_expected()

        self.log.info("Run node replacement changing instance type")
        instance_type_id = 62 if self.params.get("cluster_backend") == "aws" else 41
        self.test_replace_node_no_interruptions_expected(instance_type_id=instance_type_id)

        self.log.info("Run Vector Store verification AFTER replacing a node")
        self.test_vector_store_recall(queries_num=10)
