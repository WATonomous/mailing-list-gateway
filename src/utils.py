import json
import logging
import os
from enum import Enum

import typer
import yaml

logger = logging.getLogger()


class OutputFormat(str, Enum):
    yaml = "yaml"
    json = "json"
    raw = "raw"


def cli_print_retval(ret: dict | list, output_format: OutputFormat):
    if output_format == OutputFormat.yaml:
        print(yaml.dump(ret, default_flow_style=False))
    elif output_format == OutputFormat.json:
        print(json.dumps(ret, indent=2))
    elif output_format == OutputFormat.raw:
        print(ret)
    else:
        raise ValueError(f"Unknown output format: {output_format}")


app = typer.Typer(result_callback=cli_print_retval)


@app.callback()
# This function is used to add global CLI options
def main(output_format: OutputFormat = OutputFormat.yaml):
    pass


def set_up_logging():
    log_level = os.environ.get("APP_LOG_LEVEL", "INFO")
    logger.setLevel(log_level)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def get_azure_table_client(table_name: str, create_table_if_not_exists: bool = False):
    """
    Get an Azure Table client for the given table name.
    Docs: https://learn.microsoft.com/en-us/python/api/azure-data-tables/azure.data.tables.tableclient?view=azure-python
    """
    from azure.data.tables import TableClient
    from azure.core.exceptions import ResourceExistsError

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
