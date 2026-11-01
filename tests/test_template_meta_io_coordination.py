"""Metadata readers and atomic replacement cannot overlap on Windows.

The lock covers one file's short I/O only, not template processing/model calls.
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from services import template_session as module
from services.template_session import TemplateSession


def _session(tmp_path, name):
    sess = TemplateSession(name, str(tmp_path))
    sess.save_meta()
    return sess


def test_atomic_meta_replace_waits_for_same_file_reader(tmp_path, monkeypatch):
    sess = _session(tmp_path, 'same-file')
    read_entered = threading.Event()
    release_reader = threading.Event()
    write_flushed = threading.Event()
    replace_attempted = threading.Event()
    collided = []
    real_load, real_fsync, real_replace = module.json.load, os.fsync, os.replace

    def held_load(stream, *args, **kwargs):
        if os.path.normcase(stream.name) == os.path.normcase(sess.meta_path):
            read_entered.set()
            assert release_reader.wait(5)
        return real_load(stream, *args, **kwargs)

    def flushed(fd):
        result = real_fsync(fd)
        write_flushed.set()
        return result

    def replacing(source, target):
        if os.path.normcase(target) == os.path.normcase(sess.meta_path):
            replace_attempted.set()
            if not release_reader.is_set():
                collided.append(target)
                raise PermissionError('synthetic Windows reader/replace sharing conflict')
        return real_replace(source, target)

    monkeypatch.setattr(module.json, 'load', held_load)
    monkeypatch.setattr(module.os, 'fsync', flushed)
    monkeypatch.setattr(module.os, 'replace', replacing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(TemplateSession.load_meta, sess.template_id, str(tmp_path))
        try:
            assert read_entered.wait(5)
            writing = pool.submit(sess.save_meta)
            assert write_flushed.wait(5)
            # Writer is at the atomic-replace boundary. A held read must block
            # the first attempt, not provoke retries until the reader finishes.
            assert not replace_attempted.wait(.2)
        finally:
            release_reader.set()
        reading.result(timeout=5)
        writing.result(timeout=5)
    assert collided == []
    assert replace_attempted.is_set()


def test_meta_io_does_not_serialize_unrelated_templates(tmp_path, monkeypatch):
    first = _session(tmp_path, 'first')
    second = _session(tmp_path, 'second')
    entered, release = threading.Event(), threading.Event()
    real_load = module.json.load

    def held_load(stream, *args, **kwargs):
        if os.path.normcase(stream.name) == os.path.normcase(first.meta_path):
            entered.set()
            assert release.wait(5)
        return real_load(stream, *args, **kwargs)

    monkeypatch.setattr(module.json, 'load', held_load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(TemplateSession.load_meta, first.template_id, str(tmp_path))
        try:
            assert entered.wait(5)
            pool.submit(second.save_meta).result(timeout=2)
            pool.submit(TemplateSession.load_meta, second.template_id, str(tmp_path)).result(timeout=2)
        finally:
            release.set()
        reading.result(timeout=5)


def test_meta_read_does_not_hide_persistent_permission_error(tmp_path, monkeypatch):
    sess = _session(tmp_path, 'unreadable')

    def denied(*args, **kwargs):
        raise PermissionError('persistent access denied')

    monkeypatch.setattr(module, 'open', denied, raising=False)
    with pytest.raises(PermissionError, match='persistent access denied'):
        TemplateSession.load_meta(sess.template_id, str(tmp_path))
