"""Local copy vs. S3, file by file (datacenter/cloudiff.py): with the download's manifest each status is exact; without
one (a copy downloaded before manifests) the file times decide, tolerant of S3's second-cut LastModified."""

from dataviz.bucket import cloudiff
from dataviz.bucket.cloudiff import UNKNOWN, status_of

T = 1000.0                                   # the end of the download
OLD = (10, 500.0)                            # what S3 held then (size, LastModified)


def test_with_a_manifest():
    assert status_of((10, 500.0), (10, 500.0), T, OLD) == 'same'
    assert status_of((11, 2000.0), (10, 500.0), T, OLD) == 'edited_local'
    assert status_of((10, 500.0), (12, 1500.0), T, OLD) == 'changed_cloud'
    assert status_of((11, 2000.0), (12, 1500.0), T, OLD) == 'both'
    assert status_of((10, 500.0), None, T, OLD) == 'removed_cloud'
    assert status_of((4, 400.0), None, T, None) == 'added_local'          # even when older than the download
    assert status_of(None, (10, 500.0), T, OLD) == 'deleted_local'
    assert status_of(None, (10, 1500.0), T, None) == 'new_cloud'


def test_without_a_manifest_the_times_decide():
    assert status_of((11, 2000.0), (10, 500.0), T, UNKNOWN) == 'edited_local'
    assert status_of((10, 500.0), (12, 1500.0), T) == 'changed_cloud'
    assert status_of((11, 2000.0), (12, 1500.0), T) == 'both'
    assert status_of((4, 2000.0), None, T) == 'added_local' and status_of((4, 400.0), None, T) == 'removed_cloud'
    assert status_of(None, (10, 1001.0), T) == 'new_cloud' and status_of(None, (10, 999.0), T) == 'deleted_local'
    # in both but not in the manifest (both sides got it after it was written): the times decide too
    assert status_of((11, 2000.0), (12, 1500.0), T, None) == 'both'


def test_single_part_etag_only():
    assert cloudiff.single_md5('"d41d8cd98f00b204e9800998ecf8427e"') == 'd41d8cd98f00b204e9800998ecf8427e'
    assert cloudiff.single_md5('"d41d8cd98f00b204e9800998ecf8427e-3"') == '' and cloudiff.single_md5(None) == ''


def test_manifest_replaces_a_folder_and_drops_a_failed_download(tmp_path):
    p = tmp_path / 'm.sqlite'
    with cloudiff.Manifest(p, '') as m:
        m.add('a/1', 1, 1.0)
        m.add('b/1', 1, 1.0)
    with cloudiff.Manifest(p, 'a') as m:                    # a re-download of a/ replaces only a/
        m.add('a/2', 2, 2.0)
    get = cloudiff._known(p)
    assert get('a/1') is None and get('a/2') == (2, 2.0) and get('b/1') == (1, 1.0)
    get.close()
    try:
        with cloudiff.Manifest(p, 'b') as m:
            m.add('b/9', 9, 9.0)
            raise RuntimeError('stopped')
    except RuntimeError:
        pass
    get = cloudiff._known(p)
    assert get('b/1') is None and get('b/9') is None and get('a/2') == (2, 2.0)
    get.close()
    cloudiff.forget(tmp_path, 'x', 'a')                      # no manifest for that bucket: nothing to do
