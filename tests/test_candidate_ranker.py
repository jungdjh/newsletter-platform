"""Tests for candidate_ranker.rank_candidates (offline — fake anthropic client)."""

from scripts.candidate_ranker import rank_candidates


def _cands(n):
    return [{"title": f"T{i}", "source_domain": "x.com", "url": f"https://x/{i}",
             "summary": "", "published_date": None} for i in range(n)]


class _Block:
    def __init__(self, type, name=None, input=None):
        self.type, self.name, self.input = type, name, input


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeMessages:
    def __init__(self, resp=None, raise_exc=None):
        self._resp, self._raise = resp, raise_exc

    def create(self, **kw):
        if self._raise:
            raise self._raise
        return self._resp


class _FakeClient:
    def __init__(self, resp=None, raise_exc=None):
        self.messages = _FakeMessages(resp, raise_exc)


def _ranking_resp(rankings):
    return _Resp([_Block("tool_use", "rank", {"rankings": rankings})])


def test_ranks_sorts_and_caps_topk():
    cands = _cands(4)
    resp = _ranking_resp([
        {"index": 0, "score": 10, "reason": "low"},
        {"index": 1, "score": 90, "reason": "high"},
        {"index": 2, "score": 50, "reason": "mid"},
        {"index": 3, "score": 30, "reason": "meh"},
    ])
    out = rank_candidates(cands, "brief", top_k=2, client=_FakeClient(resp))
    assert [c["title"] for c in out] == ["T1", "T2"]  # 90, then 50
    assert out[0]["rank_score"] == 90 and out[0]["rank_reason"] == "high"


def test_unscored_candidate_defaults_zero_and_sinks():
    cands = _cands(3)
    resp = _ranking_resp([
        {"index": 0, "score": 80, "reason": "a"},
        {"index": 2, "score": 40, "reason": "c"},  # index 1 omitted by the model
    ])
    out = rank_candidates(cands, "brief", top_k=3, client=_FakeClient(resp))
    assert out[0]["title"] == "T0"               # 80 first
    assert out[-1]["title"] == "T1" and out[-1]["rank_score"] == 0  # omitted -> 0, last


def test_fallback_to_recency_on_exception():
    cands = _cands(3)
    out = rank_candidates(cands, "brief", top_k=2, client=_FakeClient(raise_exc=RuntimeError("boom")))
    assert [c["title"] for c in out] == ["T0", "T1"]   # original order preserved
    assert out[0]["rank_score"] is None                 # fallback marker


def test_fallback_when_no_tool_use_block():
    out = rank_candidates(_cands(2), "brief", top_k=2, client=_FakeClient(_Resp([_Block("text")])))
    assert [c["title"] for c in out] == ["T0", "T1"] and out[0]["rank_score"] is None


def test_empty_returns_empty():
    assert rank_candidates([], "brief", client=_FakeClient()) == []
