import asyncio
from functools import partial
import io
from abc import abstractmethod
from pathlib import Path
from tempfile import gettempdir
from typing import Any, Dict, Optional
from uuid import uuid4

from prefect.blocks.core import BlockAPI, register_blockapi
from prefect.orion.schemas.data import DataDocument
from prefect.settings import Settings

from google.cloud import storage as gcs
from google.oauth2 import service_account

from prefect.utilities.asyncio import run_sync_in_worker_thread


class OrionStorageAPI(BlockAPI):
    """
    A block API that is used to persist bytes. Can be be used by Orion to persist data.
    """

    @abstractmethod
    async def write(self, data: bytes):
        """
        Persists bytes and returns a JSON-serializable Python object used to
        retrieve the persisted data.
        """

    @abstractmethod
    async def read(self, obj: Any):
        """
        Accepts a JSON-serializable Python object to retrieve persisted bytes.
        """


@register_blockapi("s3storage-block")
class S3StorageBlock(OrionStorageAPI):
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_session_token: Optional[str] = None
    profile_name: Optional[str] = None
    region_name: Optional[str] = None
    bucket: str

    def block_initialization(self):
        import boto3

        self.aws_session = boto3.Session(
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            aws_session_token=self.aws_session_token,
            profile_name=self.profile_name,
            region_name=self.region_name,
        )

    async def write(self, data: bytes):
        import boto3

        # TODO: make storage nonblocking
        s3_client = self.aws_session.client("s3")
        with io.BytesIO(data) as stream:
            data_location = {"Bucket": self.bucket, "Key": str(uuid4())}
            s3_client.upload_fileobj(Fileobj=stream, **data_location)
        return data_location

    async def read(self, data_location):
        import boto3

        s3_client = self.aws_session.client("s3")
        with io.BytesIO() as stream:
            s3_client.download_fileobj(**data_location, Fileobj=stream)
            stream.seek(0)
            output = stream.read()
        return output


@register_blockapi("tempstorage-block")
class TempStorageBlock(OrionStorageAPI):
    def block_initialization(self) -> None:
        pass

    def basepath(self):
        return Path(gettempdir())

    async def write(self, data):
        import fsspec

        # TODO: make storage nonblocking
        storage_path = str(self.basepath() / str(uuid4()))
        with fsspec.open(storage_path, mode="wb") as fp:
            fp.write(data)
        return storage_path

    async def read(self, storage_path):
        import fsspec

        with fsspec.open(storage_path, mode="rb") as fp:
            return fp.read()


@register_blockapi("localstorage-block")
class LocalStorageBlock(OrionStorageAPI):
    storage_path: Optional[str]

    def block_initialization(self) -> None:
        self._storage_path = (
            self.storage_path
            if self.storage_path is not None
            else Settings().home / "storage"
        )

    def basepath(self):

        return Path(self._storage_path).absolute()

    async def write(self, data):
        import fsspec

        # TODO: make storage nonblocking
        storage_path = str(self.basepath() / str(uuid4()))
        with fsspec.open(storage_path, mode="wb") as fp:
            fp.write(data)
        return storage_path

    async def read(self, storage_path):
        import fsspec

        with fsspec.open(storage_path, mode="rb") as fp:
            return fp.read()


@register_blockapi("orionstorage-block")
class OrionStorageBlock(OrionStorageAPI):
    def block_initialization(self) -> None:
        pass

    async def write(self, data):
        from prefect.client import get_client

        async with get_client() as client:
            response = await client.post("/data/persist", content=data)
            return response.json()

    async def read(self, path_payload):
        from prefect.client import get_client

        async with get_client() as client:
            response = await client.post("/data/retrieve", json=path_payload)
            return response.content


@register_blockapi("googlecloudstorage-block")
class GoogleCloudStorageBlock(OrionStorageAPI):
    bucket: str
    project: Optional[str]
    service_account_file_path: Optional[str]
    service_account_info: Optional[Dict[str, str]]

    def block_initialization(self) -> None:
        if self.service_account_info:
            self.storage_client = gcs.Client(
                project=self.project,
                credentials=service_account.Credentials.from_service_account_info(
                    self.service_account_info
                ),
            )
        elif self.service_account_file_path:
            self.storage_client = gcs.Client(
                project=self.project,
                credentials=service_account.Credentials.from_service_account_file(
                    self.service_account_file_path
                ),
            )
        else:
            self.storage_client = gcs.Client(project=self.project)

    async def read(self, key: str):
        bucket = self.storage_client.bucket(self.bucket)
        blob = bucket.blob(key)
        return await run_sync_in_worker_thread(blob.download_as_bytes)

    async def write(self, data: bytes):
        bucket = self.storage_client.bucket(self.bucket)
        key = str(uuid4())
        blob = bucket.blob(key)
        upload = partial(blob.upload_from_string, data)
        await run_sync_in_worker_thread(upload)
        return key
