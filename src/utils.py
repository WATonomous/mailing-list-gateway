import os

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
def random_str(length: int = 10):
    """
    Generate a random string of the given length.
    """
    import random
    import string

    return "".join(random.choices(string.ascii_letters, k=length))


if __name__ == "__main__":
    app()
