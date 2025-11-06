from sdcm.tester import ClusterTester
from sdcm.utils import loader_utils
from sdcm.utils.common import S3Storage

BUCKET_NAME = "vector-store-in-cloud"
LATTE_DIR_PATH = "data_dir/latte"
DATASET_FILENAME = "dataset.txt"
GROUND_TRUTH_FILENAME = "ground_truth.txt"
TEST_DATA_FILENAME = "test_data.txt"


class VectorSearchSanity(ClusterTester, loader_utils.LoaderUtilsMixin):
    @staticmethod
    def download_vector_search_test_data_from_s3():
        s3_storage = S3Storage(bucket=BUCKET_NAME)

        base_url = f"https://{BUCKET_NAME}.s3.amazonaws.com"
        s3_storage.download_file(link=f"{base_url}/{TEST_DATA_FILENAME}", dst_dir=LATTE_DIR_PATH)
        s3_storage.download_file(link=f"{base_url}/{GROUND_TRUTH_FILENAME}", dst_dir=LATTE_DIR_PATH)
        s3_storage.download_file(link=f"{base_url}/{DATASET_FILENAME}", dst_dir=LATTE_DIR_PATH)

    def test_vector_search_sanity(self):
        self.log.info("Prepare Vector Search data")
        self.download_vector_search_test_data_from_s3()

        self.log.info("Populate cluster with data")
        self.run_prepare_write_cmd()

        self.log.info("Run ANN OF queries and validate recall")
        stress_queue = []
        stress_cmd = self.params.get('stress_cmd')
        self.assemble_and_run_all_stress_cmd(stress_queue, stress_cmd, keyspace_num=1)

        for stress in stress_queue:
            self.verify_stress_thread(stress)
