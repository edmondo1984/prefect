"""
Objects for specifying deployments and utilities for loading flows from deployments.
"""

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

import yaml
from pydantic import BaseModel, Field, parse_obj_as, validator

from prefect.blocks.core import Block
from prefect.client import OrionClient, get_client, inject_client
from prefect.context import PrefectObjectRegistry
from prefect.exceptions import ObjectNotFound
from prefect.filesystems import LocalFileSystem
from prefect.flows import Flow
from prefect.infrastructure import DockerContainer, KubernetesJob, Process
from prefect.logging.loggers import flow_run_logger
from prefect.orion import schemas
from prefect.utilities.asyncutils import run_sync_in_worker_thread, sync_compatible
from prefect.utilities.callables import ParameterSchema, parameter_schema
from prefect.utilities.dispatch import lookup_type
from prefect.utilities.filesystem import tmpchdir
from prefect.utilities.importtools import import_object


@inject_client
async def load_flow_from_flow_run(
    flow_run: schemas.core.FlowRun, client: OrionClient, ignore_storage: bool = False
) -> Flow:
    """
    Load a flow from the location/script provided in a deployment's storage document.

    If `ignore_storage=True` is provided, no pull from remote storage occurs.  This flag
    is largely for testing, and assumes the flow is already available locally.
    """
    deployment = await client.read_deployment(flow_run.deployment_id)

    if not ignore_storage:
        if deployment.storage_document_id:
            storage_document = await client.read_block_document(
                deployment.storage_document_id
            )
            storage_block = Block._from_block_document(storage_document)
        else:
            basepath = deployment.path or Path(deployment.manifest_path).parent
            storage_block = LocalFileSystem(basepath=basepath)

        sys.path.insert(0, ".")
        # TODO: append deployment.path
        await storage_block.get_directory(from_path=None, local_path=".")

    flow_run_logger(flow_run).debug(
        f"Loading flow for deployment {deployment.name!r}..."
    )

    import_path = Path(deployment.path) / deployment.entrypoint

    # for backwards compat
    if deployment.manifest_path:
        with open(deployment.manifest_path, "r") as f:
            import_path = json.load(f)["import_path"]
            import_path = (
                Path(deployment.manifest_path).parent / import_path
            ).absolute()
    flow = await run_sync_in_worker_thread(import_object, str(import_path))
    return flow


def load_deployments_from_yaml(
    path: str,
) -> PrefectObjectRegistry:
    """
    Load deployments from a yaml file.
    """
    with open(path, "r") as f:
        contents = f.read()

    # Parse into a yaml tree to retrieve separate documents
    nodes = yaml.compose_all(contents)

    with PrefectObjectRegistry(capture_failures=True) as registry:
        for node in nodes:
            with tmpchdir(path):
                deployment_dict = yaml.safe_load(yaml.serialize(node))
                # The return value is not necessary, just instantiating the Deployment
                # is enough to get it recorded on the registry
                parse_obj_as(Deployment, deployment_dict)

    return registry


class Deployment(BaseModel):
    """
    A Prefect Deployment definition, used for specifying and building deployments.

    Args:
        name: A name for the deployment (required).
        version: An optional version for the deployment; defaults to the flow's version
        description: An optional description of the deployment; defaults to the flow's description
        tags: An optional list of tags to associate with this deployment
        schedule: A schedule to run this deployment on, once registered
        flow_name: The name of the flow this deployment encapsulates
        parameters: A dictionary of parameter values to pass to runs created from this deployment
        infrastructure: An optional infrastructure block used to configure infrastructure for runs;
            if not provided, will default to running this deployment in Agent subprocesses
        infra_overrides: A dictionary of dot delimited infrastructure overrides that will be applied at
            runtime; for example `env.CONFIG_KEY=config_value` or `namespace='prefect'`
        storage: An optional remote storage block used to store and retrieve this workflow;
            if not provided, will default to referencing this flow by its local path
        path: The path to the working directory for the workflow, relative to remote storage or,
            if stored on a local filesystem, an absolute path
        entrypoint: The path to the entrypoint for the workflow, always relative to the `path`
        parameter_openapi_schema: The parameter schema of the flow, including defaults.

    Examples:

        Create a new deployment using configuration defaults for an imported flow:

        >>> from my_project.flows import my_flow
        >>> from prefect.deployments import Deployment
        >>>
        >>> deployment = Deployment(name="example", version="1", tags=["demo"])
        >>> deployment.build_from_flow(my_flow)
        >>> deployment.apply()

        Create a new deployment with custom storage and an infrastructure override:

        >>> from my_project.flows import my_flow
        >>> from prefect.deployments import Deployment
        >>> from prefect.filesystems import S3

        >>> storage = S3.load("dev-bucket") # load a pre-defined block
        >>> deployment = Deployment(
        ...     name="s3-example",
        ...     version="2",
        ...     tags=["aws"],
        ...     storage=storage,
        ...     infra_overrides=dict("env.PREFECT_LOGGING_LEVEL"="DEBUG"),
        >>> )
        >>> deployment.build_from_flow(my_flow)
        >>> deployment.apply()

    """

    class Config:
        validate_assignment = True

    @property
    def _editable_fields(self) -> List[str]:
        editable_fields = [
            "name",
            "description",
            "version",
            "work_queue_name",
            "tags",
            "parameters",
            "schedule",
            "infra_overrides",
        ]
        if self.infrastructure._block_document_id:
            return editable_fields
        else:
            return editable_fields + ["infrastructure"]

    @sync_compatible
    async def to_yaml(self, path: Path) -> None:
        yaml_dict = self._yaml_dict()
        schema = self.schema()

        with open(path, "w") as f:
            # write header
            f.write(
                f"###\n### A complete description of a Prefect Deployment for flow {self.flow_name!r}\n###\n"
            )

            # write editable fields
            for field in self._editable_fields:
                # write any comments
                if schema["properties"][field].get("yaml_comment"):
                    f.write(f"# {schema['properties'][field]['yaml_comment']}\n")
                # write the field
                yaml.dump({field: yaml_dict[field]}, f, sort_keys=False)

            # write non-editable fields
            f.write("\n###\n### DO NOT EDIT BELOW THIS LINE\n###\n")
            yaml.dump(
                {k: v for k, v in yaml_dict.items() if k not in self._editable_fields},
                f,
                sort_keys=False,
            )

    def _yaml_dict(self) -> dict:
        """
        Returns a YAML-compatible representation of this deployment as a dictionary.
        """
        # avoids issues with UUIDs showing up in YAML
        all_fields = json.loads(
            self.json(
                exclude={
                    "storage": {"_filesystem", "filesystem", "_remote_file_system"}
                }
            )
        )
        if all_fields["storage"]:
            all_fields["storage"][
                "_block_type_slug"
            ] = self.storage.get_block_type_slug()
        return all_fields

    # top level metadata
    name: str = Field(..., description="The name of the deployment.")
    description: str = Field(
        None, description="An optional description of the deployment."
    )
    version: str = Field(None, description="An optional version for the deployment.")
    tags: List[str] = Field(default_factory=list)
    schedule: schemas.schedules.SCHEDULE_TYPES = None
    flow_name: str = Field(None, description="The name of the flow.")
    work_queue_name: Optional[str] = Field(
        None,
        description="The work queue for the deployment.",
        yaml_comment="The work queue that will handle this deployment's runs",
    )

    # flow data
    parameters: Dict[str, Any] = Field(default_factory=dict)
    manifest_path: str = Field(
        None,
        description="The path to the flow's manifest file, relative to the chosen storage.",
    )
    infrastructure: Union[DockerContainer, KubernetesJob, Process] = Field(
        default_factory=Process
    )
    infra_overrides: Dict[str, Any] = Field(
        default_factory=dict,
        description="Overrides to apply to the base infrastructure block at runtime.",
    )
    storage: Optional[Block] = Field(
        None,
        help="The remote storage to use for this workflow.",
    )
    path: str = Field(
        None,
        description="The path to the working directory for the workflow, relative to remote storage or an absolute path.",
    )
    entrypoint: str = Field(
        None,
        description="The path to the entrypoint for the workflow, relative to the `path`.",
    )
    parameter_openapi_schema: ParameterSchema = Field(
        None, description="The parameter schema of the flow, including defaults."
    )

    @validator("storage", pre=True)
    def cast_storage_to_block_type(cls, value):
        if isinstance(value, dict):
            block = lookup_type(Block, value.pop("_block_type_slug"))
            return block(**value)
        return value

    @classmethod
    @sync_compatible
    async def load_from_yaml(cls, path: str):
        with open(str(path), "r") as f:
            data = yaml.safe_load(f)
            return cls(**data)

    @sync_compatible
    async def load(self) -> bool:
        """
        Queries the API for a deployment with this name for this flow, and if found, prepopulates
        settings.  Returns a boolean specifying whether a load was successful or not.
        """
        if not self.name or not self.flow_name:
            raise ValueError("Both a deployment name and flow name must be provided.")
        async with get_client() as client:
            try:
                deployment = await client.read_deployment_by_name(
                    f"{self.flow_name}/{self.name}"
                )
                if deployment.storage_document_id:
                    storage = Block._from_block_document(
                        await client.read_block_document(deployment.storage_document_id)
                    )

                excluded_fields = self.__fields_set__.union(
                    {"infrastructure", "storage"}
                )
                for field in set(self.__fields__.keys()) - excluded_fields:
                    new_value = getattr(deployment, field)
                    setattr(self, field, new_value)

                if "infrastructure" not in self.__fields_set__:
                    if deployment.infrastructure_document_id:
                        self.infrastructure = Block._from_block_document(
                            await client.read_block_document(
                                deployment.infrastructure_document_id
                            )
                        )
                if "storage" not in self.__fields_set__:
                    if deployment.storage_document_id:
                        self.storage = Block._from_block_document(
                            await client.read_block_document(
                                deployment.storage_document_id
                            )
                        )
            except ObjectNotFound:
                return False
        return True

    @sync_compatible
    async def update(self, ignore_none: bool = False, **kwargs):
        """
        Performs an in-place update with the provided settings.

        Args:
            ignore_none: if True, all `None` values are ignored when performing the update
        """
        unknown_keys = set(kwargs.keys()) - set(self.dict().keys())
        if unknown_keys:
            raise ValueError(
                f"Received unexpected attributes: {', '.join(unknown_keys)}"
            )
        for key, value in kwargs.items():
            if ignore_none and value is None:
                continue
            setattr(self, key, value)

    @sync_compatible
    async def upload_to_storage(self, storage_block: Block = None) -> Optional[int]:
        """
        Uploads the workflow this deployment represents using a provided storage block;
        if no block is provided, defaults to configuring self for local storage.
        """
        deployment_path = None
        file_count = None
        if storage_block:
            template = await Block.load(storage_block)
            self.storage = template.copy(
                exclude={"_block_document_id", "_block_document_name", "_is_anonymous"}
            )

            # upload current directory to storage location
            file_count = await self.storage.put_directory(ignore_file=".prefectignore")
        elif not self.storage:
            # default storage, no need to move anything around
            self.storage = None
            deployment_path = str(Path(".").absolute())
        else:
            file_count = await self.storage.put_directory(ignore_file=".prefectignore")

        # persists storage now in case it contains secret values
        if self.storage and not self.storage._block_document_id:
            await self.storage._save(is_anonymous=True)

        self.path = deployment_path
        return file_count

    @sync_compatible
    async def apply(self) -> UUID:
        """
        Registers this deployment with the API and returns the deployment's ID.
        """
        if not self.name or not self.flow_name:
            raise ValueError("Both a deployment name and flow name must be set.")
        async with get_client() as client:
            # prep IDs
            flow_id = await client.create_flow_from_name(self.flow_name)

            infrastructure_document_id = self.infrastructure._block_document_id
            if not infrastructure_document_id:
                # if not building off a block, will create an anonymous block
                self.infrastructure = self.infrastructure.copy()
                infrastructure_document_id = await self.infrastructure._save(
                    is_anonymous=True,
                )

            # we assume storage was already saved
            storage_document_id = getattr(self.storage, "_block_document_id", None)

            deployment_id = await client.create_deployment(
                flow_id=flow_id,
                name=self.name,
                work_queue_name=self.work_queue_name,
                version=self.version,
                schedule=self.schedule,
                parameters=self.parameters,
                description=self.description,
                tags=self.tags,
                manifest_path=self.manifest_path,  # allows for backwards YAML compat
                path=self.path,
                entrypoint=self.entrypoint,
                infra_overrides=self.infra_overrides,
                storage_document_id=storage_document_id,
                infrastructure_document_id=infrastructure_document_id,
                parameter_openapi_schema=self.parameter_openapi_schema.dict(),
            )

            return deployment_id

    @sync_compatible
    async def build_from_flow(self, f: Flow, output: str = None):
        """
        Configure this deployment for a given flow.

        Note that this method loads any settings that may already be configured for this deployment
        server-side (e.g., schedules, default parameter values, etc.).

        Optionally writes the full specification as a YAML file in the location specified by `output`.
        """
        ## first see if an entrypoint can be determined
        flow_file = getattr(f, "__globals__", {}).get("__file__")
        mod_name = getattr(f, "__module__", None)
        if not flow_file:
            if not mod_name:
                # todo, check if the file location was manually set already
                raise ValueError("Could not determine flow's file location.")
            module = importlib.import_module(mod_name)
            flow_file = getattr(module, "__file__", None)
            if not flow_file:
                raise ValueError("Could not determine flow's file location.")

        self.flow_name = f.name
        await self.load()

        # set a few attributes for this flow object
        self.entrypoint = f"{Path(flow_file).absolute()}:{f.fn.__name__}"
        self.parameter_openapi_schema = parameter_schema(f)
        if not self.version:
            self.version = f.version
        if not self.description:
            self.description = f.description

        # if no storage is set, assume local for now
        # TODO: revisit with Docker integration
        # note: this method call sets `self.path`
        await self.upload_to_storage()

        if output:
            await self.to_yaml(output)
