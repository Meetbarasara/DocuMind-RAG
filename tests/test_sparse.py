"""L1: stateless sparse (lexical) encoder for Pinecone native hybrid."""

from src.components.sparse import _hash, convex_scale, encode_text


def test_shared_terms_land_on_overlapping_indices():
    doc = encode_text("reinforcement learning for traffic signals")
    q = encode_text("traffic signal reinforcement learning")
    assert set(doc["indices"]) & set(q["indices"])           # shared keywords overlap
    assert len(doc["indices"]) == len(doc["values"])


def test_stopwords_and_empty_yield_empty_sparse():
    assert encode_text("the a of to and is") == {"indices": [], "values": []}
    assert encode_text("") == {"indices": [], "values": []}


def test_sublinear_tf_weights_repeats_higher():
    once = encode_text("apple banana")
    twice = encode_text("apple apple banana")
    ai = _hash("apple")
    w_once = dict(zip(once["indices"], once["values"]))[ai]
    w_twice = dict(zip(twice["indices"], twice["values"]))[ai]
    assert w_twice > w_once  # 1 + log(2) > 1.0


def test_convex_scale_weights_both_vectors():
    d, s = convex_scale([1.0, 2.0], {"indices": [3], "values": [4.0]}, alpha=0.25)
    assert d == [0.25, 0.5]        # dense * alpha
    assert s == {"indices": [3], "values": [3.0]}  # sparse * (1 - alpha)


def test_convex_scale_clamps_alpha():
    d, s = convex_scale([1.0], {"indices": [0], "values": [2.0]}, alpha=2.0)
    assert d == [1.0] and s["values"] == [0.0]  # alpha clamps to 1 -> sparse zeroed
