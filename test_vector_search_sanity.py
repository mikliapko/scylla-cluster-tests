import time

from sdcm.tester import ClusterTester
from sdcm.utils.common import S3Storage

BUCKET_NAME = "vector-store-in-cloud"
LATTE_DIR_PATH = "data_dir/latte"
DATASET_FILENAME = "dataset.txt"
GROUND_TRUTH_FILENAME = "ground_truth.txt"
TEST_DATA_FILENAME = "test_data.txt"


class VectorSearchBase(ClusterTester):
    @staticmethod
    def download_vector_search_test_data_from_s3():
        s3_storage = S3Storage(bucket=BUCKET_NAME)

        base_url = f"https://{BUCKET_NAME}.s3.amazonaws.com"
        s3_storage.download_file(link=f"{base_url}/{TEST_DATA_FILENAME}", dst_dir=LATTE_DIR_PATH)
        s3_storage.download_file(link=f"{base_url}/{GROUND_TRUTH_FILENAME}", dst_dir=LATTE_DIR_PATH)
        s3_storage.download_file(link=f"{base_url}/{DATASET_FILENAME}", dst_dir=LATTE_DIR_PATH)


class VectorSearchSanity(VectorSearchBase):
    def test_vector_search_sanity(self):
        self.log.info("Prepare Vector Search data")
        self.download_vector_search_test_data_from_s3()

        self.log.info("Create schema and index")
        command = f"latte schema {LATTE_DIR_PATH}/vector_search.rn"
        self.run_stress_thread(command)

        self.log.info("Populate cluster with data")
        command = f"latte run {LATTE_DIR_PATH}/vector_search.rn -f insert -d 50000"
        self.run_stress_thread(command)

        self.log.info("Wait for index to build")
        time.sleep(120)

        self.log.info("Run ANN OF queries and validate recall")
        command = f"latte run {LATTE_DIR_PATH}/vector_search.rn -f validate_average_recall -d 60s"
        self.run_stress_thread(command)
