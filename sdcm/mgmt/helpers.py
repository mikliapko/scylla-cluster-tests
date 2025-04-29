import gzip
import json
import os
import re
from pathlib import Path

from sdcm.utils.common import download_dir_from_cloud


CREATE_KS_REPLICATION_BLOCK_PATTERN = re.compile(r"replication\s*=\s*({.*?})")
REPLICATION_DICT_KEYS_PATTERN = re.compile(r"'([^']+)':")


def get_schema_create_statements_from_snapshot(bucket: str, mgr_cluster_id: str) -> tuple[list[str], list[str]]:
    """Parses schema information from a snapshot file and extracts CQL statements for keyspaces and tables,
    excluding entries related to system keyspaces, roles.

    Important note: only AWS is supported for now.

    Returns:
        tuple[list[str], list[str]]: A tuple containing two lists:
            - List of CQL statements for creating keyspaces.
            - List of CQL statements for creating tables.
    """
    s3_dir_with_schema_json = f"s3://{bucket}/backup/schema/cluster/{mgr_cluster_id}"
    temp_dir_with_schema_file = download_dir_from_cloud(url=s3_dir_with_schema_json)
    schema_file = Path(temp_dir_with_schema_file, os.listdir(temp_dir_with_schema_file)[0])

    with gzip.open(schema_file, 'rt', encoding='utf-8') as gz_file:
        schema = json.load(gz_file)

    keyspace_statements = []
    table_statements = []

    for entry in schema:
        if entry["type"] == "keyspace" and not entry["keyspace"].startswith("system"):
            keyspace_statements.append(entry["cql_stmt"])
        elif entry["type"] == "table" and not entry["keyspace"].startswith("system"):
            table_statements.append(entry["cql_stmt"])

    return keyspace_statements, table_statements


def get_dc_name_from_ks_statement(ks_statement: str) -> str:
    """Extracts the name of a datacenter from a CREATE KEYSPACE statement.

    For example, for the statement:

        "CREATE KEYSPACE \"5gb_stcs_quorum_64_16_2024_2_4\"
            WITH replication = {'class': 'org.apache.cassandra.locator.NetworkTopologyStrategy', 'us-east': '3'}
            AND durable_writes = true
            AND tablets = {'enabled': false};"

    the function will return "us-east".
    """
    # Firstly, the code extracts the whole replication block from the statement.
    # Then, it finds all keys in the replication dictionary and filters out the 'class' key.
    # It doesn't search for dc_name in the replication block directly because it might not be the second key.
    replication_block = CREATE_KS_REPLICATION_BLOCK_PATTERN.search(ks_statement).group(1)
    _keys = REPLICATION_DICT_KEYS_PATTERN.findall(replication_block)

    return next(iter(key for key in _keys if key != 'class'))
