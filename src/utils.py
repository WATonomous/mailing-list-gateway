import os
import random
import string

from slugify import slugify
from watcloud_utils.logging import logger
from watcloud_utils.typer import app


def get_azure_table_client(table_name: str, create_table_if_not_exists: bool = False):
    """
    Get an Azure Table client for the given table name.
    Docs: https://learn.microsoft.com/en-us/python/api/azure-data-tables/azure.data.tables.tableclient?view=azure-python
    """
    from azure.core.exceptions import ResourceExistsError
    from azure.data.tables import TableClient

    conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]

    table_client = TableClient.from_connection_string(
        conn_str=conn_str, table_name=table_name
    )

    if create_table_if_not_exists:
        try:
            table_client.create_table()
            logger.info(f"Created table: {table_name}")
        except ResourceExistsError:
            logger.info(f"Table '{table_name}' already exists")

    return table_client


@app.command()
def dump_azure_table(table_name: str):
    table_client = get_azure_table_client(table_name)
    return list(table_client.list_entities())


@app.command()
def delete_azure_table(table_name: str):
    table_client = get_azure_table_client(table_name)
    table_client.delete_table()
    return f"Deleted table: {table_name}"


@app.command()
def random_str(length: int = 32, chars: str = string.ascii_lowercase):
    """
    Generate a random string of the given length.

    The default dictionary of characters to choose from is the lowercase alphabet.
    """
    return "".join(random.choices(chars, k=length))

@app.command()
def make_azure_table_key(strs: list[str]):
    r"""
    Generate an Azure Table key from the given strings.

    The generated key conforms to the following requirements:
    - (azure) up to 1024 characters
    - (azure) does not contain the characters '/', '\', '#', '?', or control characters
    - (custom) the beginning of each str is guaranteed to be included in the key
    - (custom) the generated key is deterministic for the given input

    Requirements derived from:
    - https://learn.microsoft.com/en-us/rest/api/storageservices/understanding-the-table-service-data-model
    """
    # Just a naive implementation for now
    max_len_per_str = 1024 // len(strs)

    key = "".join(slugify(s)[:max_len_per_str] for s in strs)

    return key




if __name__ == "__main__":
    app()
