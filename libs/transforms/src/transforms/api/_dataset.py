"""The exposed Function definitions and docstrings is Copyright © 2023 Palantir Technologies Inc. and/or affiliates (“Palantir”). All rights reserved.

https://www.palantir.com/docs/foundry/transforms-python/transforms-python-api/
https://www.palantir.com/docs/foundry/transforms-python/transforms-python-api-classes/

"""  # noqa: E501

from __future__ import annotations

import inspect
import logging
import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry_dev_tools.errors.dataset import BranchNotFoundError, DatasetHasNoSchemaError, DatasetHasNoTransactionsError
from foundry_dev_tools.utils.api_types import SQLReturnType
from foundry_dev_tools.utils.misc import is_dataset_a_view
from foundry_dev_tools.utils.repo import git_toplevel_dir
from transforms import TRANSFORMS_FOUNDRY_CONTEXT

if TYPE_CHECKING:
    import pyspark
    import pyspark.sql

    from foundry_dev_tools.utils import api_types

LOGGER = logging.getLogger(__name__)


def _as_list(list_or_single_item: list[Any] | Any | None) -> list[Any]:  # noqa: ANN401
    """Helper function turning single values or None into lists.

    Args:
        list_or_single_item (List[Any] | Any | None): item or list to return as a list

    Returns:
        list:
            either the single item as a list, or the list passed in list_or_single_item

    """
    if not list_or_single_item:
        return []

    return list_or_single_item if isinstance(list_or_single_item, list) else [list_or_single_item]


class Input:
    """Specification of a transform dataset input.

    Some API requests may be sent when the Input class is constructed. However, the actual download
    is only initiated when dataframe() or get_local_path_to_dataset() is called.

    """

    def __init__(
        self,
        alias: api_types.DatasetRid | api_types.FoundryPath,
        branch: api_types.DatasetBranch | None = None,
        description: str | None = None,  # noqa: ARG002
        stop_propagating=None,  # noqa: ARG002,ANN001
        stop_requiring=None,  # noqa: ARG002,ANN001
        checks=None,  # noqa: ARG002,ANN001
    ):
        """Specification of a transform dataset input.

        Args:
            alias (str | None): Dataset rid or the absolute Compass path of the dataset.
                If not specified, parameter is unbound.
            branch (str | None): Branch name to resolve the input dataset to.
                If not specified, resolved at build-time.
            stop_propagating (Markings | None): not implemented in Foundry DevTools
            stop_requiring (OrgMarkings | None): not implemented in Foundry DevTools
            checks (List[Check], Check): not implemented in foundry-dev-tools
            description (str): not implemented in foundry-dev-tools

        """
        # extract caller filename to retrieve git information
        caller_filename = inspect.stack()[1].filename
        LOGGER.debug("Input instantiated from %s", caller_filename)
        self._spark_df = None
        if branch is None:
            branch = _get_branch(Path(caller_filename))

        if self._is_online:
            (
                self._is_spark_df_retrievable,
                self._dataset_identity,
                self.branch,
            ) = self._online(alias, branch)
        else:
            (
                self._is_spark_df_retrievable,
                self._dataset_identity,
                self.branch,
            ) = self._offline(alias, branch)

    @property
    def _is_online(self) -> bool:
        return not TRANSFORMS_FOUNDRY_CONTEXT.config.transforms_freeze_cache

    def _online(
        self,
        alias: api_types.DatasetRid | api_types.FoundryPath,
        branch: api_types.DatasetBranch,
    ) -> tuple[bool, api_types.DatasetIdentity, api_types.DatasetBranch]:
        try:
            dataset_identity = TRANSFORMS_FOUNDRY_CONTEXT.foundry_rest_client.get_dataset_identity(alias, branch)
        except BranchNotFoundError:
            LOGGER.debug(
                "Dataset %s not found on branch %s, falling back to dataset from master.",
                alias,
                branch,
            )
            branch = "master"
            dataset_identity = TRANSFORMS_FOUNDRY_CONTEXT.foundry_rest_client.get_dataset_identity(alias, branch)
        if dataset_identity["last_transaction_rid"] is None:
            raise DatasetHasNoTransactionsError(dataset=alias)
        if self._dataset_has_schema(dataset_identity, branch):
            return (
                True,
                dataset_identity,
                branch,
            )
        LOGGER.debug(
            "Dataset rid: %s, path: %s on branch %s has no schema, "
            "falling back to file download. "
            "Only filesystem() is supported with this dataset.",
            dataset_identity["dataset_rid"],
            dataset_identity["dataset_path"],
            branch,
        )
        return False, dataset_identity, branch

    def _offline(self, alias: str, branch: str) -> tuple[bool, api_types.DatasetIdentity, str]:
        dataset_identity = TRANSFORMS_FOUNDRY_CONTEXT.cached_foundry_client.cache.get_dataset_identity_not_branch_aware(
            alias,
        )
        if TRANSFORMS_FOUNDRY_CONTEXT.cached_foundry_client.cache.dataset_has_schema(dataset_identity):
            return True, dataset_identity, branch
        return False, dataset_identity, branch

    def _dataset_has_schema(
        self,
        dataset_identity: api_types.DatasetIdentity,
        branch: api_types.DatasetBranch,
    ) -> bool | None:
        try:
            TRANSFORMS_FOUNDRY_CONTEXT.foundry_rest_client.get_dataset_schema(
                dataset_identity["dataset_rid"],
                dataset_identity["last_transaction_rid"],
                branch,
            )
        except DatasetHasNoSchemaError:
            return False
        else:
            return True

    def _retrieve_spark_df(
        self,
        dataset_identity: api_types.DatasetIdentity,
        branch: api_types.DatasetBranch,
    ) -> pyspark.sql.DataFrame:
        if dataset_identity in list(TRANSFORMS_FOUNDRY_CONTEXT.cached_foundry_client.cache.keys()):
            return self._retrieve_from_cache(dataset_identity, branch)
        return self._retrieve_from_foundry_and_cache(dataset_identity, branch)

    def _retrieve_from_cache(
        self,
        dataset_identity: api_types.DatasetIdentity,
        branch: api_types.DatasetBranch,
    ) -> pyspark.sql.DataFrame:
        LOGGER.debug("Returning data for %s on branch %s from cache", dataset_identity, branch)
        return TRANSFORMS_FOUNDRY_CONTEXT.cached_foundry_client.cache[dataset_identity]

    def _read_spark_df_with_sql_query(
        self,
        dataset_path: api_types.FoundryPath,
        branch: api_types.DatasetBranch = "master",
    ) -> pyspark.sql.DataFrame:
        query = f"SELECT * FROM `{dataset_path}`"  # noqa: S608
        if TRANSFORMS_FOUNDRY_CONTEXT.config.transforms_sql_sample_select_random:
            query = query + " ORDER BY RAND()"
        query = query + f" LIMIT {TRANSFORMS_FOUNDRY_CONTEXT.config.transforms_sql_sample_row_limit}"
        LOGGER.debug("Executing Foundry/SparkSQL Query: %s \n on branch %s", query, branch)
        return TRANSFORMS_FOUNDRY_CONTEXT.foundry_rest_client.query_foundry_sql(
            query,
            branch=branch,
            return_type=SQLReturnType.SPARK,
        )

    def _retrieve_from_foundry_and_cache(
        self,
        dataset_identity: api_types.DatasetIdentity,
        branch: str,
    ) -> pyspark.sql.DataFrame:
        LOGGER.debug("Caching data for %s on branch %s", dataset_identity, branch)
        transaction = dataset_identity["last_transaction"]["transaction"]
        if is_dataset_a_view(transaction):
            foundry_stats = TRANSFORMS_FOUNDRY_CONTEXT.foundry_rest_client.foundry_stats(
                dataset_identity["dataset_rid"],
                dataset_identity["last_transaction"]["rid"],
            )
            size_in_bytes = int(foundry_stats["computedDatasetStats"]["sizeInBytes"])
        else:
            size_in_bytes = transaction["metadata"]["totalFileSize"]
        size_in_mega_bytes = size_in_bytes / 1024 / 1024
        size_in_mega_bytes_rounded = round(size_in_mega_bytes, ndigits=2)
        LOGGER.debug("Dataset has size of %s MegaBytes.", size_in_mega_bytes_rounded)
        if (TRANSFORMS_FOUNDRY_CONTEXT.config.transforms_force_full_dataset_download) or (
            size_in_mega_bytes < TRANSFORMS_FOUNDRY_CONTEXT.config.transforms_sql_dataset_size_threshold
        ):
            spark_df = TRANSFORMS_FOUNDRY_CONTEXT.cached_foundry_client.load_dataset(
                dataset_identity["dataset_rid"],
                branch,
            )
        else:
            dataset_name = dataset_identity["dataset_path"].split("/")[-1]
            warnings.warn(
                f"Retrieving subset ({TRANSFORMS_FOUNDRY_CONTEXT.config.transforms_sql_sample_row_limit} rows)"
                f" of dataset '{dataset_name}'"
                f" with rid '{dataset_identity['dataset_rid']}' "
                f"because dataset size {size_in_mega_bytes_rounded} megabytes >= "
                f"{TRANSFORMS_FOUNDRY_CONTEXT.config.transforms_sql_dataset_size_threshold} megabytes "
                f"(as defined in config['transforms_sql_dataset_size_threshold']).",
            )
            spark_df = self._read_spark_df_with_sql_query(dataset_identity["dataset_path"], branch)
            TRANSFORMS_FOUNDRY_CONTEXT.cached_foundry_client.cache[dataset_identity] = spark_df
        return spark_df

    def dataframe(self) -> pyspark.sql.DataFrame | None:
        """Get the cached :external+spark:py:class:`~pyspark.sql.DataFrame` of this Input.

        Only available if the input has a schema. The Spark DataFrame will get loaded the first
        time this method is invoked.

        Returns:
            :external+spark:py:class:`~pyspark.sql.DataFrame`: The cached DataFrame of this Input

        """
        if self._is_spark_df_retrievable and self._spark_df is None:
            if self._is_online:
                self._spark_df = self._retrieve_spark_df(self._dataset_identity, self.branch)
            else:
                self._spark_df = TRANSFORMS_FOUNDRY_CONTEXT.cached_foundry_client.cache[self._dataset_identity]

        return self._spark_df

    def get_dataset_identity(self) -> api_types.DatasetIdentity:
        """Returns identity of this Input.

        Returns:
            dict:
                with the keys dataset_path, dataset_rid, last_transaction_rid

        """
        return self._dataset_identity

    def get_local_path_to_dataset(self) -> str:
        """Returns path to the dataset's files on disk.

        Calling this method for the first time may trigger downloading the dataset files.

        Returns:
            str:
                path to the dataset's files on disk

        """
        return os.fspath(
            TRANSFORMS_FOUNDRY_CONTEXT.cached_foundry_client._fetch_dataset(  # noqa: SLF001
                self._dataset_identity,
                self.branch,
            )
            if self._is_online
            else TRANSFORMS_FOUNDRY_CONTEXT.cached_foundry_client.cache.get_path_to_local_dataset(
                self._dataset_identity,
            ),
        )


def _get_branch(caller_filename: Path) -> str:
    git_dir = git_toplevel_dir(caller_filename)
    if not git_dir:
        # fallback for VS Interactive Console
        # or Jupyter lab on Windows
        git_dir = Path.cwd()

    head_file = git_dir.joinpath(".git", "HEAD")
    if head_file.is_file():
        with head_file.open() as hf:
            ref = hf.read().strip()

        if ref.startswith("ref: refs/heads/"):
            return ref[16:]

        return "HEAD"  # immitate behaviour of `git rev-parse --abbrev-ref HEAD`

    warnings.warn("Could not detect git branch of project, falling back to 'master'.")
    return "master"


class Output:
    """Specification of a transform dataset output.

    Writing the Output back to Foundry is not implemented.

    """

    def __init__(
        self,
        alias: str | None = None,
        sever_permissions: bool | None = False,  # noqa: ARG002
        description: str | None = None,  # noqa: ARG002
        checks=None,  # noqa: ANN001,ARG002
    ):
        """Specification of a transform output.

        Args:
            alias (str | None): Dataset rid or the absolute Compass path of the dataset.
                If not specified, parameter is unbound.
            sever_permissions (bool | None): not implemented in foundry-dev-tools
            description (str | None): not implemented in foundry-dev-tools
            checks (List[Check], Check): not implemented in foundry-dev-tools
        """
        self.alias = alias


class UnmarkingDef:
    """Base class for unmarking datasets configuration."""

    def __init__(self, marking_ids: list[str] | str, on_branches: list[str] | str | None):
        """Default constructor.

        Args:
            marking_ids (List[str], str): List of marking identifiers or single marking identifier.
            on_branches (List[str], str): Branch on which to apply unmarking.
        """
        self.marking_ids = _as_list(marking_ids)
        self.branches = _as_list(on_branches)


class Markings(UnmarkingDef):
    """Specification of a marking that stops propagating from input.

    The actual marking removal is not implemented.
    """


class OrgMarkings(UnmarkingDef):
    """Specification of a marking that is no longer required on the output.

    The actual marking requirement check is not implemented.
    """