import abc
import glob
import json
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

import anyio
import fsspec
from pydantic import Field, SecretStr, validator

from prefect.blocks.core import Block
from prefect.utilities.asyncutils import run_sync_in_worker_thread
from prefect.utilities.filesystem import filter_files


class ReadableFileSystem(Block, abc.ABC):
    _block_schema_capabilities = ["read-path"]

    @abc.abstractmethod
    async def read_path(self, path: str) -> bytes:
        pass


class WritableFileSystem(Block, abc.ABC):
    _block_schema_capabilities = ["write-path"]

    @abc.abstractmethod
    async def write_path(self, path: str, content: bytes) -> None:
        pass


class LocalFileSystem(ReadableFileSystem, WritableFileSystem):
    """
    Store data as a file on a local file system.

    Example:
        Load stored local file system config:
        ```python
        from prefect.filesystems import LocalFileSystem

        local_file_system_block = LocalFileSystem.load("BLOCK_NAME")
        ```
    """

    _block_type_name = "Local File System"
    _logo_url = "https://images.ctfassets.net/gm98wzqotmnx/EVKjxM7fNyi4NGUSkeTEE/95c958c5dd5a56c59ea5033e919c1a63/image1.png?h=250"

    basepath: Optional[str] = None

    @validator("basepath", pre=True)
    def cast_pathlib(cls, value):
        if isinstance(value, Path):
            return str(value)
        return value

    def _resolve_path(self, path: str) -> Path:
        # Only resolve the base path at runtime, default to the current directory
        basepath = (
            Path(self.basepath).expanduser().resolve()
            if self.basepath
            else Path(".").resolve()
        )

        # Determine the path to access relative to the base path, ensuring that paths
        # outside of the base path are off limits
        path: Path = Path(path).expanduser()
        if not path.is_absolute():
            path = basepath / path
        else:
            path = path.resolve()
            if not basepath in path.parents:
                raise ValueError(
                    f"Attempted to write to path {path} outside of the base path {basepath}."
                )

        return path

    async def get_directory(
        self, from_path: str = None, local_path: str = None
    ) -> None:
        """
        Copies a directory from one place to another on the local filesystem.

        Defaults to copying the entire contents of the block's basepath to the current working directory.
        """
        if from_path is None:
            from_path = Path(self.basepath).expanduser()

        if local_path is None:
            local_path = Path(".").absolute()

        if sys.version_info < (3, 8):
            shutil.copytree(from_path, local_path)
        else:
            shutil.copytree(from_path, local_path, dirs_exist_ok=True)

    async def _get_ignore_func(self, local_path: str, ignore_file: str):
        with open(ignore_file, "r") as f:
            ignore_patterns = f.readlines()

        included_files = filter_files(local_path, ignore_patterns)

        def ignore_func(directory, files):
            return_val = [f for f in files if f not in included_files]
            return return_val

        return ignore_func

    async def put_directory(
        self, local_path: str = None, to_path: str = None, ignore_file: str = None
    ) -> None:
        """
        Copies a directory from one place to another on the local filesystem.

        Defaults to copying the entire contents of the current working directory to the block's basepath.

        An `ignore_file` path may be provided that can include gitignore style expressions for filepaths to ignore.
        """
        if to_path is None:
            to_path = Path(self.basepath).expanduser()

        if local_path is None:
            local_path = Path(".").absolute()

        if ignore_file:
            ignore_func = await self._get_ignore_func(local_path, ignore_file)
        else:
            ignore_func = None
        if local_path == to_path:
            pass
        else:
            if sys.version_info < (3, 8):
                shutil.copytree(local_path, to_path, ignore=ignore_func)
            else:
                shutil.copytree(
                    local_path, to_path, dirs_exist_ok=True, ignore=ignore_func
                )

    async def read_path(self, path: str) -> bytes:
        path: Path = self._resolve_path(path)

        # Check if the path exists
        if not path.exists():
            raise ValueError(f"Path {path} does not exist.")

        # Validate that its a file
        if not path.is_file():
            raise ValueError(f"Path {path} is not a file.")

        async with await anyio.open_file(str(path), mode="rb") as f:
            content = await f.read()

        return content

    async def write_path(self, path: str, content: bytes) -> str:
        path: Path = self._resolve_path(path)

        # Construct the path if it does not exist
        path.parent.mkdir(exist_ok=True, parents=True)

        # Check if the file already exists
        if path.exists() and not path.is_file():
            raise ValueError(f"Path {path} already exists and is not a file.")

        async with await anyio.open_file(path, mode="wb") as f:
            await f.write(content)


class RemoteFileSystem(ReadableFileSystem, WritableFileSystem):
    """
    Store data as a file on a remote file system.

    Supports any remote file system supported by `fsspec`. The file system is specified
    using a protocol. For example, "s3://my-bucket/my-folder/" will use S3.

    Example:
        Load stored remote file system config:
        ```python
        from prefect.filesystems import RemoteFileSystem

        remote_file_system_block = RemoteFileSystem.load("BLOCK_NAME")
        ```
    """

    _block_type_name = "Remote File System"
    _logo_url = "https://images.ctfassets.net/gm98wzqotmnx/4CxjycqILlT9S9YchI7o1q/ee62e2089dfceb19072245c62f0c69d2/image12.png?h=250"

    basepath: str
    settings: dict = Field(default_factory=dict)

    # Cache for the configured fsspec file system used for access
    _filesystem: fsspec.AbstractFileSystem = None

    @validator("basepath")
    def check_basepath(cls, value):
        scheme, netloc, _, _, _ = urllib.parse.urlsplit(value)

        if not scheme:
            raise ValueError(f"Base path must start with a scheme. Got {value!r}.")

        if not netloc:
            raise ValueError(
                f"Base path must include a location after the scheme. Got {value!r}."
            )

        if scheme == "file":
            raise ValueError(
                "Base path scheme cannot be 'file'. Use `LocalFileSystem` instead for local file access."
            )

        return value

    def _resolve_path(self, path: str) -> str:
        base_scheme, base_netloc, base_urlpath, _, _ = urllib.parse.urlsplit(
            self.basepath
        )
        scheme, netloc, urlpath, _, _ = urllib.parse.urlsplit(path)

        # Confirm that absolute paths are valid
        if scheme:
            if scheme != base_scheme:
                raise ValueError(
                    f"Path {path!r} with scheme {scheme!r} must use the same scheme as the base path {base_scheme!r}."
                )

        if netloc:
            if (netloc != base_netloc) or not urlpath.startswith(base_urlpath):
                raise ValueError(
                    f"Path {path!r} is outside of the base path {self.basepath!r}."
                )

        return f"{self.basepath.rstrip('/')}/{urlpath.lstrip('/')}"

    async def get_directory(
        self, from_path: Optional[str] = None, local_path: Optional[str] = None
    ) -> None:
        """
        Downloads a directory from a given remote path to a local direcotry.

        Defaults to downloading the entire contents of the block's basepath to the current working directory.
        """
        if from_path is None:
            from_path = str(self.basepath)

        if local_path is None:
            local_path = Path(".").absolute()

        return self.filesystem.get(from_path, local_path, recursive=True)

    async def put_directory(
        self,
        local_path: Optional[str] = None,
        to_path: Optional[str] = None,
        ignore_file: Optional[str] = None,
        overwrite: bool = True,
    ) -> int:
        """
        Uploads a directory from a given local path to a remote direcotry.

        Defaults to uploading the entire contents of the current working directory to the block's basepath.
        """
        if to_path is None:
            to_path = str(self.basepath)

        if local_path is None:
            local_path = "."

        included_files = None
        if ignore_file:
            with open(ignore_file, "r") as f:
                ignore_patterns = f.readlines()

            included_files = filter_files(local_path, ignore_patterns)

        counter = 0
        for f in glob.glob("**", recursive=True):
            if ignore_file and f not in included_files:
                continue
            if to_path.endswith("/"):
                fpath = to_path + f
            else:
                fpath = to_path + "/" + f
            if Path(f).is_dir():
                pass
            else:
                if overwrite:
                    self.filesystem.put_file(f, fpath, overwrite=True)
                else:
                    self.filesystem.put_file(f, fpath)
            counter += 1
        return counter

    async def read_path(self, path: str) -> bytes:
        path = self._resolve_path(path)

        with self.filesystem.open(path, "rb") as file:
            content = await run_sync_in_worker_thread(file.read)

        return content

    async def write_path(self, path: str, content: bytes) -> str:
        path = self._resolve_path(path)
        dirpath = path[: path.rindex("/")]

        self.filesystem.makedirs(dirpath, exist_ok=True)

        with self.filesystem.open(path, "wb") as file:
            await run_sync_in_worker_thread(file.write, content)

    @property
    def filesystem(self) -> fsspec.AbstractFileSystem:
        if not self._filesystem:
            scheme, _, _, _, _ = urllib.parse.urlsplit(self.basepath)

            try:
                self._filesystem = fsspec.filesystem(scheme, **self.settings)
            except ImportError as exc:
                # The path is a remote file system that uses a lib that is not installed
                raise RuntimeError(
                    f"File system created with scheme {scheme!r} from base path "
                    f"{self.basepath!r} could not be created. "
                    "You are likely missing a Python module required to use the given "
                    "storage protocol."
                ) from exc

        return self._filesystem


class S3(ReadableFileSystem, WritableFileSystem):
    """
    Store data as a file on AWS S3.

    Example:
        Load stored S3 config:
        ```python
        from prefect.filesystems import S3

        s3_block = S3.load("BLOCK_NAME")
        ```
    """

    _block_type_name = "S3"
    _logo_url = "https://images.ctfassets.net/gm98wzqotmnx/1jbV4lceHOjGgunX15lUwT/db88e184d727f721575aeb054a37e277/aws.png?h=250"

    bucket_path: str = Field(
        ..., description="An S3 bucket path", example="my-bucket/a-directory-within"
    )
    aws_access_key_id: SecretStr = Field(None, title="AWS Access Key ID")
    aws_secret_access_key: SecretStr = Field(None, title="AWS Secret Access Key")

    _remote_file_system: RemoteFileSystem = None

    @property
    def basepath(self) -> str:
        return f"s3://{self.bucket_path}"

    @property
    def filesystem(self) -> RemoteFileSystem:
        settings = {}
        if self.aws_access_key_id:
            settings["key"] = self.aws_access_key_id.get_secret_value()
        if self.aws_secret_access_key:
            settings["secret"] = self.aws_secret_access_key.get_secret_value()
        self._remote_file_system = RemoteFileSystem(
            basepath=f"s3://{self.bucket_path}", settings=settings
        )
        return self._remote_file_system

    async def get_directory(
        self, from_path: Optional[str] = None, local_path: Optional[str] = None
    ) -> bytes:
        """
        Downloads a directory from a given remote path to a local direcotry.

        Defaults to downloading the entire contents of the block's basepath to the current working directory.
        """
        return await self.filesystem.get_directory(
            from_path=from_path, local_path=local_path
        )

    async def put_directory(
        self,
        local_path: Optional[str] = None,
        to_path: Optional[str] = None,
        ignore_file: Optional[str] = None,
    ) -> int:
        """
        Uploads a directory from a given local path to a remote direcotry.

        Defaults to uploading the entire contents of the current working directory to the block's basepath.
        """
        return await self.filesystem.put_directory(
            local_path=local_path, to_path=to_path, ignore_file=ignore_file
        )

    async def read_path(self, path: str) -> bytes:
        return await self.filesystem.read_path(path)

    async def write_path(self, path: str, content: bytes) -> str:
        return await self.filesystem.write_path(path=path, content=content)


class GCS(ReadableFileSystem, WritableFileSystem):
    """
    Store data as a file on Google Cloud Storage.

    Example:
        Load stored GCS config:
        ```python
        from prefect.filesystems import GCS

        gcs_block = GCS.load("BLOCK_NAME")
        ```
    """

    _logo_url = "https://images.ctfassets.net/gm98wzqotmnx/4CD4wwbiIKPkZDt4U3TEuW/c112fe85653da054b6d5334ef662bec4/gcp.png?h=250"

    bucket_path: str = Field(
        ..., description="A GCS bucket path", example="my-bucket/a-directory-within"
    )
    service_account_info: Optional[SecretStr] = Field(
        None, description="The contents of a service account keyfile as a JSON string."
    )
    project: Optional[str] = Field(
        None,
        description="The project the GCS bucket resides in. If not provided, the project will be inferred from the credentials or environment.",
    )

    @property
    def basepath(self) -> str:
        return f"gcs://{self.bucket_path}"

    @property
    def filesystem(self) -> RemoteFileSystem:
        settings = {}
        if self.service_account_info:
            try:
                settings["token"] = json.loads(
                    self.service_account_info.get_secret_value()
                )
            except json.JSONDecodeError:
                raise ValueError(
                    "Unable to load provided service_account_info. Please make sure that the provided value is a valid JSON string."
                )
        remote_file_system = RemoteFileSystem(
            basepath=f"gcs://{self.bucket_path}", settings=settings
        )
        return remote_file_system

    async def get_directory(
        self, from_path: Optional[str] = None, local_path: Optional[str] = None
    ) -> bytes:
        """
        Downloads a directory from a given remote path to a local directory.

        Defaults to downloading the entire contents of the block's basepath to the current working directory.
        """
        return await self.filesystem.get_directory(
            from_path=from_path, local_path=local_path
        )

    async def put_directory(
        self,
        local_path: Optional[str] = None,
        to_path: Optional[str] = None,
        ignore_file: Optional[str] = None,
    ) -> int:
        """
        Uploads a directory from a given local path to a remote directory.

        Defaults to uploading the entire contents of the current working directory to the block's basepath.
        """
        return await self.filesystem.put_directory(
            local_path=local_path, to_path=to_path, ignore_file=ignore_file
        )

    async def read_path(self, path: str) -> bytes:
        return await self.filesystem.read_path(path)

    async def write_path(self, path: str, content: bytes) -> str:
        return await self.filesystem.write_path(path=path, content=content)


class Azure(ReadableFileSystem, WritableFileSystem):
    """
    Store data as a file on Azure Datalake and Azure Blob Storage.

    Example:
        Load stored Azure config:
        ```python
        from prefect.filesystems import Azure

        az_block = Azure.load("BLOCK_NAME")
        ```
    """

    _block_type_name = "Azure"
    _logo_url = "https://images.ctfassets.net/gm98wzqotmnx/6AiQ6HRIft8TspZH7AfyZg/39fd82bdbb186db85560f688746c8cdd/azure.png?h=250"

    bucket_path: str = Field(
        ...,
        description="An Azure storage bucket path",
        example="my-bucket/a-directory-within",
    )
    azure_storage_connection_string: Optional[SecretStr] = Field(
        None,
        title="Azure storage connection string",
        description="Equivalent to the AZURE_STORAGE_CONNECTION_STRING environment variable",
    )
    azure_storage_account_name: Optional[SecretStr] = Field(
        None,
        title="Azure storage account name",
        description="Equivalent to the AZURE_STORAGE_ACCOUNT_NAME environment variable",
    )
    azure_storage_account_key: Optional[SecretStr] = Field(
        None,
        title="Azure storage account key",
        description="Equivalent to the AZURE_STORAGE_ACCOUNT_KEY environment variable",
    )
    _remote_file_system: RemoteFileSystem = None

    @property
    def basepath(self) -> str:
        return f"az://{self.bucket_path}"

    @property
    def filesystem(self) -> RemoteFileSystem:
        settings = {}
        if self.azure_storage_connection_string:
            settings[
                "connection_string"
            ] = self.azure_storage_connection_string.get_secret_value()
        if self.azure_storage_account_name:
            settings[
                "account_name"
            ] = self.azure_storage_account_name.get_secret_value()
        if self.azure_storage_account_key:
            settings["account_key"] = self.azure_storage_account_key.get_secret_value()
        self._remote_file_system = RemoteFileSystem(
            basepath=f"az://{self.bucket_path}", settings=settings
        )
        return self._remote_file_system

    async def get_directory(
        self, from_path: Optional[str] = None, local_path: Optional[str] = None
    ) -> bytes:
        """
        Downloads a directory from a given remote path to a local direcotry.

        Defaults to downloading the entire contents of the block's basepath to the current working directory.
        """
        return await self.filesystem.get_directory(
            from_path=from_path, local_path=local_path
        )

    async def put_directory(
        self,
        local_path: Optional[str] = None,
        to_path: Optional[str] = None,
        ignore_file: Optional[str] = None,
    ) -> int:
        """
        Uploads a directory from a given local path to a remote directory.

        Defaults to uploading the entire contents of the current working directory to the block's basepath.
        """
        return await self.filesystem.put_directory(
            local_path=local_path, to_path=to_path, ignore_file=ignore_file
        )

    async def read_path(self, path: str) -> bytes:
        return await self.filesystem.read_path(path)

    async def write_path(self, path: str, content: bytes) -> str:
        return await self.filesystem.write_path(path=path, content=content)


class SMB(ReadableFileSystem, WritableFileSystem):
    """
    Store data as a file on a SMB share.

    Example:

        Load stored SMB config:

        ```python
        from prefect.filesystems import SMB
        smb_block = SMB.load("BLOCK_NAME")
        ```
    """

    _block_type_name = "SMB"

    share_path: str = Field(
        ...,
        description="SMB target (requires <SHARE>, followed by <PATH>)",
        example="/SHARE/dir/subdir",
    )
    smb_username: Optional[SecretStr] = Field(
        None,
        title="SMB Username",
        description="Username with access to the target SMB SHARE",
    )
    smb_password: Optional[SecretStr] = Field(
        None,
        title="SMB Password",
    )
    smb_host: str = Field(
        ..., tile="SMB server/hostname", description="SMB server/hostname"
    )
    smb_port: Optional[int] = Field(
        None, title="SMB port", description="SMB port (default: 445)"
    )

    _remote_file_system: RemoteFileSystem = None

    @property
    def basepath(self) -> str:
        return f"smb://{self.smb_host.rstrip('/')}/{self.share_path.lstrip('/')}"

    @property
    def filesystem(self) -> RemoteFileSystem:
        settings = {}
        if self.smb_username:
            settings["username"] = self.smb_username.get_secret_value()
        if self.smb_password:
            settings["password"] = self.smb_password.get_secret_value()
        if self.smb_host:
            settings["host"] = self.smb_host
        if self.smb_port:
            settings["port"] = self.smb_port
        self._remote_file_system = RemoteFileSystem(
            basepath=f"smb://{self.smb_host.rstrip('/')}/{self.share_path.lstrip('/')}",
            settings=settings,
        )
        return self._remote_file_system

    async def get_directory(
        self, from_path: Optional[str] = None, local_path: Optional[str] = None
    ) -> bytes:
        """
        Downloads a directory from a given remote path to a local directory.
        Defaults to downloading the entire contents of the block's basepath to the current working directory.
        """
        return await self.filesystem.get_directory(
            from_path=from_path, local_path=local_path
        )

    async def put_directory(
        self,
        local_path: Optional[str] = None,
        to_path: Optional[str] = None,
        ignore_file: Optional[str] = None,
    ) -> int:
        """
        Uploads a directory from a given local path to a remote directory.
        Defaults to uploading the entire contents of the current working directory to the block's basepath.
        """
        return await self.filesystem.put_directory(
            local_path=local_path,
            to_path=to_path,
            ignore_file=ignore_file,
            overwrite=False,
        )

    async def read_path(self, path: str) -> bytes:
        return await self.filesystem.read_path(path)

    async def write_path(self, path: str, content: bytes) -> str:
        return await self.filesystem.write_path(path=path, content=content)
