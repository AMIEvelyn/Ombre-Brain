# ============================================================
# Tests for MemoryEdgeStore.delete_for_buckets (2026-07-19): bulk-deleting
# buckets from the Dashboard felt slow because delete_for_bucket() does a
# full read+rewrite of the whole JSONL file per call -- delete_for_buckets
# does it once for a whole batch instead. delete_for_bucket() itself is now
# just a one-id wrapper around it, so this also covers the old behavior.
# ============================================================

import os
import tempfile

from memory_edges import MemoryEdgeStore


def _store():
    tmp = tempfile.mkdtemp()
    return MemoryEdgeStore({"state_dir": tmp})


def test_delete_for_buckets_removes_edges_touching_any_id_in_one_pass():
    s = _store()
    s.add_edge("a", "b", "causes")
    s.add_edge("c", "d", "causes")
    s.add_edge("e", "f", "causes")
    s.add_edge("x", "y", "causes")  # untouched by the batch below

    deleted = s.delete_for_buckets(["a", "c", "e"])
    assert deleted == 3

    remaining = s.list_edges()
    assert len(remaining) == 1
    assert remaining[0]["source"] == "x" and remaining[0]["target"] == "y"


def test_delete_for_buckets_matches_by_either_source_or_target():
    s = _store()
    s.add_edge("a", "target_bucket", "causes")
    s.add_edge("source_bucket", "b", "causes")
    s.add_edge("unrelated_1", "unrelated_2", "causes")

    deleted = s.delete_for_buckets(["target_bucket", "source_bucket"])
    assert deleted == 2
    assert len(s.list_edges()) == 1


def test_delete_for_buckets_empty_input_is_a_noop():
    s = _store()
    s.add_edge("a", "b", "causes")
    assert s.delete_for_buckets([]) == 0
    assert s.delete_for_buckets(["", "  "]) == 0
    assert len(s.list_edges()) == 1


def test_delete_for_bucket_single_id_still_works_as_before():
    s = _store()
    s.add_edge("a", "b", "causes")
    s.add_edge("c", "d", "causes")
    assert s.delete_for_bucket("a") == 1
    assert len(s.list_edges()) == 1


def test_delete_for_buckets_skips_rewrite_when_nothing_matches():
    s = _store()
    s.add_edge("a", "b", "causes")
    path = s.path
    before_mtime = os.path.getmtime(path)
    assert s.delete_for_buckets(["no_such_bucket"]) == 0
    assert os.path.getmtime(path) == before_mtime  # file untouched, no rewrite
