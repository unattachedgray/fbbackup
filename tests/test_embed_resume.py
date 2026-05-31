"""Resumable-checkpoint behaviour for fbbackup.embed.

A whole-archive embed that dies mid-run must resume from where it stopped, not
re-embed everything. We force the local provider (no key/network), mock the
per-batch embedder to fail after the first batch, then resume and assert the
final output is complete and the second run only embedded the leftover rows.
Pure stdlib + numpy + mock — no fastembed, no HTTP.
"""
import json
import os
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
from fbbackup import embed as E  # noqa: E402

NROWS = 260  # > one local batch (256) so resume crosses a batch boundary
DIM = 4


def _make_corpus(tmp_path: Path) -> Path:
    base = tmp_path / "spaces" / "default" / "2020"
    base.mkdir(parents=True)
    for i in range(NROWS):
        (base / f"{i:03d}.md").write_text(f"hello world {i}", encoding="utf-8")
    return tmp_path / "spaces"


def _fake_vecs(texts):
    return [[1.0, 2.0, 3.0, 4.0] for _ in texts]


def test_embed_resumes_from_checkpoint(tmp_path, monkeypatch):
    spaces = _make_corpus(tmp_path)
    out = tmp_path / "index"
    monkeypatch.setenv("FBBACKUP_EMBED_PROVIDER", "local")  # no key, no fastembed call

    # First run: succeed on batch 1 (256 rows, checkpointed), blow up on batch 2.
    calls = {"n": 0}
    def flaky(provider, texts, key):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated interruption")
        return _fake_vecs(texts)
    monkeypatch.setattr(E, "_embed_batch", flaky)

    with pytest.raises(RuntimeError):
        E.embed(spaces, out)

    ckpt_npy, ckpt_js = E._ckpt_paths(out)
    assert ckpt_npy.exists() and ckpt_js.exists(), "checkpoint should survive the crash"
    assert json.loads(ckpt_js.read_text())["done"] == 256, "first full batch persisted"
    assert not (out / "embeddings.npy").exists(), "no final output yet"

    # Second run: count how many rows the embedder is asked for — must be only
    # the 4 leftovers, proving resume (not a from-scratch re-embed).
    embedded = {"rows": 0}
    def counting(provider, texts, key):
        embedded["rows"] += len(texts)
        return _fake_vecs(texts)
    monkeypatch.setattr(E, "_embed_batch", counting)

    res = E.embed(spaces, out)

    assert embedded["rows"] == NROWS - 256, "resume only embedded the leftover rows"
    assert res["count"] == NROWS
    arr = np.load(out / "embeddings.npy")
    assert arr.shape == (NROWS, DIM)
    assert len(json.loads((out / "embed-ids.json").read_text())) == NROWS
    assert not ckpt_npy.exists() and not ckpt_js.exists(), "checkpoint cleared on success"


def test_checkpoint_invalidated_when_corpus_changes(tmp_path):
    out = tmp_path / "index"
    out.mkdir()
    fp_a = E._fingerprint("local", "m", ["default/2020/a", "default/2020/b"])
    fp_b = E._fingerprint("local", "m", ["default/2020/a", "default/2020/c"])
    assert fp_a != fp_b  # different ids → different fingerprint
    E._save_ckpt(out, fp_a, [[1.0, 2.0]], "local", "m")
    assert E._load_ckpt(out, fp_a) == [[1.0, 2.0]]   # matching fp resumes
    assert E._load_ckpt(out, fp_b) == []             # mismatched fp starts clean


def test_gpu_device_opt_in(monkeypatch):
    # No FBBACKUP_EMBED_DEVICE → CPU (no providers kwarg); =gpu → CUDA providers.
    seen = {}
    class FakeTE:
        def __init__(self, model, **kw):
            seen.update(kw)
    monkeypatch.setattr(E, "_LOCAL", None)
    monkeypatch.setitem(os.environ, "FBBACKUP_EMBED_DEVICE", "")
    import sys
    import types
    fake_mod = types.ModuleType("fastembed")
    fake_mod.TextEmbedding = FakeTE
    monkeypatch.setitem(sys.modules, "fastembed", fake_mod)
    E._local_model()
    assert "providers" not in seen
    E._LOCAL = None
    seen.clear()
    monkeypatch.setitem(os.environ, "FBBACKUP_EMBED_DEVICE", "gpu")
    E._local_model()
    assert seen.get("providers") == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    E._LOCAL = None
