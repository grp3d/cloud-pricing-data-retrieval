"""
Storage abstraction contract, run identically against a local directory and moto S3
(research R4, FR-008). Conditional writes back the run claim and latest.json CAS.
"""

import pytest

from src.pipeline.storage import LocalStorage, PreconditionFailed, S3Storage, open_storage


def test_put_get_bytes_roundtrip(store):
    store.put_bytes("aws/manifests/latest.json", b'{"a": 1}')
    assert store.get_bytes("aws/manifests/latest.json") == b'{"a": 1}'


def test_get_missing_raises_file_not_found(store):
    with pytest.raises(FileNotFoundError):
        store.get_bytes("nope.json")


def test_put_file_and_download_file(store, tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"x" * 1000)
    store.put_file("aws/parquet/t/part-1.parquet", str(src))
    dest = tmp_path / "out" / "copy.bin"
    store.download_file("aws/parquet/t/part-1.parquet", str(dest))
    assert dest.read_bytes() == b"x" * 1000


def test_head_returns_size_etag_and_mtime(store):
    store.put_bytes("k/one.txt", b"hello")
    info = store.head("k/one.txt")
    assert info.key == "k/one.txt"
    assert info.size == 5
    assert info.etag
    assert info.last_modified.tzinfo is not None
    assert store.head("k/missing.txt") is None


def test_list_is_recursive_and_root_relative(store):
    for key in ["aws/a/1.txt", "aws/a/b/2.txt", "aws/c.txt", "other/x.txt"]:
        store.put_bytes(key, b"1")
    keys = sorted(o.key for o in store.list("aws/"))
    assert keys == ["aws/a/1.txt", "aws/a/b/2.txt", "aws/c.txt"]
    assert list(store.list("missing/")) == []


def test_delete_and_delete_missing_is_noop(store):
    store.put_bytes("k/del.txt", b"1")
    store.delete("k/del.txt")
    assert store.head("k/del.txt") is None
    store.delete("k/del.txt")  # no error


def test_put_if_absent(store):
    store.put_if_absent("claims/2026-10-05.json", b"first")
    with pytest.raises(PreconditionFailed):
        store.put_if_absent("claims/2026-10-05.json", b"second")
    assert store.get_bytes("claims/2026-10-05.json") == b"first"


def test_put_if_match(store):
    store.put_bytes("latest.json", b"v1")
    etag = store.head("latest.json").etag
    store.put_if_match("latest.json", b"v2", etag)
    assert store.get_bytes("latest.json") == b"v2"
    with pytest.raises(PreconditionFailed):
        store.put_if_match("latest.json", b"v3", etag)  # stale etag
    assert store.get_bytes("latest.json") == b"v2"


def test_put_if_match_on_missing_key_fails(store):
    with pytest.raises(PreconditionFailed):
        store.put_if_match("missing.json", b"v", "etag")


def test_open_storage_parses_uris(tmp_path, aws_env):
    local = open_storage(f"file://{tmp_path}")
    assert isinstance(local, LocalStorage)
    s3 = open_storage("s3://my-bucket/some/prefix")
    assert isinstance(s3, S3Storage)
    assert s3.bucket == "my-bucket"
    assert s3.prefix == "some/prefix/"
    assert open_storage("s3://my-bucket").prefix == ""
    with pytest.raises(ValueError):
        open_storage("gs://bucket")


def test_s3_prefix_is_applied_transparently(s3_store):
    import boto3

    prefixed = open_storage("s3://test-data/root/")
    prefixed.put_bytes("aws/x.txt", b"1")
    raw = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="test-data")
    assert [o["Key"] for o in raw["Contents"]] == ["root/aws/x.txt"]
    assert [o.key for o in prefixed.list("aws/")] == ["aws/x.txt"]


def test_is_local_flag(local_store, s3_store):
    assert local_store.is_local is True
    assert s3_store.is_local is False
