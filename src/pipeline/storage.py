"""
Storage abstraction over a local directory or an S3 bucket/prefix (FR-008, research R4).

Keys are always `/`-separated and relative to the storage root. Both backends support
the conditional writes the run claim and latest.json compare-and-swap rely on:
`put_if_absent` (S3 If-None-Match: *) and `put_if_match` (S3 If-Match: <etag>).
"""

import datetime as dt
import fcntl
import hashlib
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol
from urllib.parse import urlparse


class PreconditionFailed(Exception):
    """A conditional write lost: the key exists (put_if_absent) or changed (put_if_match)."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str
    last_modified: dt.datetime


class Storage(Protocol):
    is_local: bool
    uri: str

    def put_bytes(self, key: str, data: bytes) -> None: ...
    def put_file(self, key: str, path: str) -> None: ...
    def put_if_absent(self, key: str, data: bytes) -> None: ...
    def put_if_match(self, key: str, data: bytes, etag: str) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def download_file(self, key: str, path: str) -> None: ...
    def head(self, key: str) -> Optional[ObjectInfo]: ...
    def list(self, prefix: str) -> Iterator[ObjectInfo]: ...
    def delete(self, key: str) -> None: ...
    def server_time(self) -> Optional[dt.datetime]: ...


def _check_key(key: str) -> str:
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError(f"Invalid storage key {key!r}")
    return key


class LocalStorage:
    """A directory tree. Atomic writes via temp file + rename; CAS under a flock."""

    is_local = True

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.uri = f"file://{self.root}"
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *_check_key(key).split("/"))

    @staticmethod
    def _etag(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _write_atomic(self, path: str, data: bytes) -> str:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return tmp

    @contextmanager
    def _cas_lock(self) -> Iterator[None]:
        fd = os.open(os.path.join(self.root, ".storage.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def put_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.replace(self._write_atomic(path, data), path)

    def put_file(self, key: str, path: str) -> None:
        target = self._path(key)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".tmp-")
        os.close(fd)
        shutil.copyfile(path, tmp)
        os.replace(tmp, target)

    def put_if_absent(self, key: str, data: bytes) -> None:
        path = self._path(key)
        tmp = self._write_atomic(path, data)
        try:
            os.link(tmp, path)  # fails atomically if the key already exists
        except FileExistsError:
            raise PreconditionFailed(key)
        finally:
            os.remove(tmp)

    def put_if_match(self, key: str, data: bytes, etag: str) -> None:
        path = self._path(key)
        with self._cas_lock():
            try:
                with open(path, "rb") as fh:
                    current = self._etag(fh.read())
            except FileNotFoundError:
                raise PreconditionFailed(key)
            if current != etag:
                raise PreconditionFailed(key)
            os.replace(self._write_atomic(path, data), path)

    def get_bytes(self, key: str) -> bytes:
        with open(self._path(key), "rb") as fh:
            return fh.read()

    def download_file(self, key: str, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        shutil.copyfile(self._path(key), path)

    def head(self, key: str) -> Optional[ObjectInfo]:
        path = self._path(key)
        try:
            stat = os.stat(path)
            with open(path, "rb") as fh:
                etag = self._etag(fh.read())
        except FileNotFoundError:
            return None
        return ObjectInfo(
            key=key,
            size=stat.st_size,
            etag=etag,
            last_modified=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
        )

    def list(self, prefix: str) -> Iterator[ObjectInfo]:
        base = self._path(prefix.rstrip("/")) if prefix.strip("/") else self.root
        if os.path.isfile(base):
            info = self.head(prefix)
            if info:
                yield info
            return
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for name in sorted(filenames):
                if name.startswith(".tmp-") or name == ".storage.lock":
                    continue
                full = os.path.join(dirpath, name)
                key = os.path.relpath(full, self.root).replace(os.sep, "/")
                if not key.startswith(prefix):
                    continue
                stat = os.stat(full)
                yield ObjectInfo(
                    key=key,
                    size=stat.st_size,
                    etag="",  # computed lazily via head() when needed
                    last_modified=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
                )

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        # Tidy up empty parent folders, but never the root.
        parent = os.path.dirname(path)
        while parent != self.root and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
            parent = os.path.dirname(parent)

    def server_time(self) -> Optional[dt.datetime]:
        return None


class S3Storage:
    """An S3 bucket, optionally under a key prefix."""

    is_local = False

    def __init__(self, bucket: str, prefix: str = "", client=None):
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.uri = f"s3://{bucket}/{self.prefix}"
        self._client = client or boto3.client("s3")

    def _key(self, key: str) -> str:
        return self.prefix + _check_key(key)

    def _strip(self, full_key: str) -> str:
        return full_key[len(self.prefix):]

    @staticmethod
    def _lost_condition(error) -> bool:
        """True if a conditional write failed its precondition (412) or raced (409)."""
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = error.response.get("Error", {}).get("Code")
        return status in (409, 412) or code in ("PreconditionFailed", "ConditionalRequestConflict")

    @staticmethod
    def _not_found(error) -> bool:
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = error.response.get("Error", {}).get("Code")
        return status == 404 or code in ("NoSuchKey", "NotFound", "404")

    def put_bytes(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def put_file(self, key: str, path: str) -> None:
        self._client.upload_file(path, self.bucket, self._key(key))

    def put_if_absent(self, key: str, data: bytes) -> None:
        from botocore.exceptions import ClientError

        try:
            self._client.put_object(
                Bucket=self.bucket, Key=self._key(key), Body=data, IfNoneMatch="*"
            )
        except ClientError as e:
            if self._lost_condition(e):
                raise PreconditionFailed(key)
            raise

    def put_if_match(self, key: str, data: bytes, etag: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self._client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, IfMatch=etag)
        except ClientError as e:
            if self._lost_condition(e) or self._not_found(e):
                raise PreconditionFailed(key)
            raise

    def get_bytes(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except ClientError as e:
            if self._not_found(e):
                raise FileNotFoundError(key)
            raise

    def download_file(self, key: str, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._client.download_file(self.bucket, self._key(key), path)

    def head(self, key: str) -> Optional[ObjectInfo]:
        from botocore.exceptions import ClientError

        try:
            r = self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as e:
            if self._not_found(e):
                return None
            raise
        return ObjectInfo(key=key, size=r["ContentLength"], etag=r["ETag"], last_modified=r["LastModified"])

    def list(self, prefix: str) -> Iterator[ObjectInfo]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            for obj in page.get("Contents", []):
                yield ObjectInfo(
                    key=self._strip(obj["Key"]),
                    size=obj["Size"],
                    etag=obj["ETag"],
                    last_modified=obj["LastModified"],
                )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def server_time(self) -> Optional[dt.datetime]:
        from email.utils import parsedate_to_datetime

        r = self._client.head_bucket(Bucket=self.bucket)
        date = r.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("date")
        return parsedate_to_datetime(date) if date else None


def open_storage(uri: str) -> Storage:
    """`file:///abs/path` → LocalStorage; `s3://bucket[/prefix]` → S3Storage."""
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        path = (parsed.netloc + parsed.path) if parsed.netloc else parsed.path
        return LocalStorage(path or ".")
    if parsed.scheme == "s3":
        if not parsed.netloc:
            raise ValueError(f"S3 URI needs a bucket: {uri!r}")
        return S3Storage(parsed.netloc, parsed.path)
    raise ValueError(f"Unsupported storage URI {uri!r}; use file:// or s3://")
