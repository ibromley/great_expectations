import datetime
import logging
from string import Template
from urllib.parse import urlparse

from great_expectations.core.batch import Batch
from great_expectations.core.util import nested_update
from great_expectations.dataset.sqlalchemy_dataset import SqlAlchemyBatchReference
from great_expectations.datasource import Datasource
from great_expectations.datasource.types import BatchMarkers
from great_expectations.exceptions import DatasourceInitializationError
from great_expectations.types import ClassConfig
from great_expectations.types.configurations import classConfigSchema

logger = logging.getLogger(__name__)

try:
    import sqlalchemy
    from sqlalchemy import create_engine
except ImportError:
    sqlalchemy = None
    create_engine = None
    logger.debug("Unable to import sqlalchemy.")


class SqlAlchemyDatasource(Datasource):
    """
    A SqlAlchemyDatasource will provide data_assets converting batch_kwargs using the following rules:
      - if the batch_kwargs include a table key, the datasource will provide a dataset object connected
        to that table
      - if the batch_kwargs include a query key, the datasource will create a temporary table using that
        that query. The query can be parameterized according to the standard python Template engine, which
        uses $parameter, with additional kwargs passed to the get_batch method.
    """

    recognized_batch_parameters = {"query_parameters", "limit", "dataset_options"}

    @classmethod
    def build_configuration(
        cls, data_asset_type=None, batch_kwargs_generators=None, **kwargs
    ):
        """
        Build a full configuration object for a datasource, potentially including generators with defaults.

        Args:
            data_asset_type: A ClassConfig dictionary
            batch_kwargs_generators: Generator configuration dictionary
            **kwargs: Additional kwargs to be part of the datasource constructor's initialization

        Returns:
            A complete datasource configuration.

        """

        if data_asset_type is None:
            data_asset_type = {
                "class_name": "SqlAlchemyDataset",
                "module_name": "great_expectations.dataset",
            }
        else:
            data_asset_type = classConfigSchema.dump(ClassConfig(**data_asset_type))

        configuration = kwargs
        configuration["data_asset_type"] = data_asset_type
        if batch_kwargs_generators is not None:
            configuration["batch_kwargs_generators"] = batch_kwargs_generators

        return configuration

    def __init__(
        self,
        name="default",
        data_context=None,
        data_asset_type=None,
        credentials=None,
        batch_kwargs_generators=None,
        **kwargs
    ):
        if not sqlalchemy:
            raise DatasourceInitializationError(
                name, "ModuleNotFoundError: No module named 'sqlalchemy'"
            )

        configuration_with_defaults = SqlAlchemyDatasource.build_configuration(
            data_asset_type, batch_kwargs_generators, **kwargs
        )
        data_asset_type = configuration_with_defaults.pop("data_asset_type")
        batch_kwargs_generators = configuration_with_defaults.pop(
            "batch_kwargs_generators", None
        )
        super(SqlAlchemyDatasource, self).__init__(
            name,
            data_context=data_context,
            data_asset_type=data_asset_type,
            batch_kwargs_generators=batch_kwargs_generators,
            **configuration_with_defaults
        )

        if credentials is not None:
            self._datasource_config.update({"credentials": credentials})
        else:
            credentials = {}

        try:
            # if an engine was provided, use that
            if "engine" in kwargs:
                self.engine = kwargs.pop("engine")

            # if a connection string or url was provided, use that
            elif "connection_string" in kwargs:
                connection_string = kwargs.pop("connection_string")
                self.engine = create_engine(connection_string, **kwargs)
                self.engine.connect()
            elif "url" in credentials:
                url = credentials.pop("url")
                self.drivername = urlparse(url).scheme
                self.engine = create_engine(url, **kwargs)
                self.engine.connect()

            # Otherwise, connect using remaining kwargs
            else:
                options, drivername = self._get_sqlalchemy_connection_options(**kwargs)
                self.drivername = drivername
                self.engine = create_engine(options)
                self.engine.connect()

        except (
            sqlalchemy.exc.OperationalError,
            sqlalchemy.exc.DatabaseError,
        ) as sqlalchemy_error:
            raise DatasourceInitializationError(self._name, str(sqlalchemy_error))

        self._build_generators()

    def _get_sqlalchemy_connection_options(self, **kwargs):
        drivername = None
        if "credentials" in self._datasource_config:
            credentials = self._datasource_config["credentials"]
        else:
            credentials = {}

        # if a connection string or url was provided in the profile, use that
        if "connection_string" in credentials:
            options = credentials["connection_string"]
        elif "url" in credentials:
            options = credentials["url"]
        else:
            # Update credentials with anything passed during connection time
            drivername = credentials.pop("drivername")
            schema_name = credentials.pop("schema_name", None)
            if schema_name is not None:
                logger.warning(
                    "schema_name specified creating a URL with schema is not supported. Set a default "
                    "schema on the user connecting to your database."
                )
            options = sqlalchemy.engine.url.URL(drivername, **credentials)
        return options, drivername

    def get_batch(self, batch_kwargs, batch_parameters=None):
        # We need to build a batch_id to be used in the dataframe
        batch_markers = BatchMarkers(
            {
                "ge_load_time": datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y%m%dT%H%M%S.%fZ"
                )
            }
        )

        if "bigquery_temp_table" in batch_kwargs:
            query_support_table_name = batch_kwargs.get("bigquery_temp_table")
        elif "snowflake_transient_table" in batch_kwargs:
            # Snowflake uses a transient table, so we expect a table_name to be provided
            query_support_table_name = batch_kwargs.get("snowflake_transient_table")
        else:
            query_support_table_name = None

        if "query" in batch_kwargs:
            if "limit" in batch_kwargs or "offset" in batch_kwargs:
                logger.warning(
                    "Limit and offset parameters are ignored when using query-based batch_kwargs; consider "
                    "adding limit and offset directly to the generated query."
                )
            if "query_parameters" in batch_kwargs:
                query = Template(batch_kwargs["query"]).safe_substitute(
                    batch_kwargs["query_parameters"]
                )
            else:
                query = batch_kwargs["query"]
            batch_reference = SqlAlchemyBatchReference(
                engine=self.engine,
                query=query,
                table_name=query_support_table_name,
                schema=batch_kwargs.get("schema"),
            )
        elif "table" in batch_kwargs:
            table = batch_kwargs["table"]
            limit = batch_kwargs.get("limit")
            offset = batch_kwargs.get("offset")
            if limit is not None or offset is not None:
                logger.info(
                    "Generating query from table batch_kwargs based on limit and offset"
                )
                # In BigQuery the table name is already qualified with its schema name
                if self.engine.dialect.name.lower() == "bigquery":
                    schema = None
                else:
                    schema = batch_kwargs.get("schema")
                raw_query = (
                    sqlalchemy.select([sqlalchemy.text("*")])
                    .select_from(
                        sqlalchemy.schema.Table(
                            table, sqlalchemy.MetaData(), schema=schema
                        )
                    )
                    .offset(offset)
                    .limit(limit)
                )
                query = str(
                    raw_query.compile(
                        self.engine, compile_kwargs={"literal_binds": True}
                    )
                )
                batch_reference = SqlAlchemyBatchReference(
                    engine=self.engine,
                    query=query,
                    table_name=query_support_table_name,
                    schema=batch_kwargs.get("schema"),
                )
            else:
                batch_reference = SqlAlchemyBatchReference(
                    engine=self.engine,
                    table_name=table,
                    schema=batch_kwargs.get("schema"),
                )
        else:
            raise ValueError(
                "Invalid batch_kwargs: exactly one of 'table' or 'query' must be specified"
            )

        return Batch(
            datasource_name=self.name,
            batch_kwargs=batch_kwargs,
            data=batch_reference,
            batch_parameters=batch_parameters,
            batch_markers=batch_markers,
            data_context=self._data_context,
        )

    def process_batch_parameters(
        self, query_parameters=None, limit=None, dataset_options=None
    ):
        batch_kwargs = super(SqlAlchemyDatasource, self).process_batch_parameters(
            limit=limit, dataset_options=dataset_options,
        )
        nested_update(batch_kwargs, {"query_parameters": query_parameters})
        return batch_kwargs
