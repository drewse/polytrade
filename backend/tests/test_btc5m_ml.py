"""Pure-Python ML toolkit tests for the BTC 5M Reversal Lab."""
from __future__ import annotations

import random

from app import btc5m_ml as ml


def _separable(n=120, seed=0):
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        a, b, c = rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)
        label = 1 if (1.5 * a - 1.0 * b + 0.3 > 0) else 0
        if rng.random() < 0.08:
            label ^= 1                       # label noise
        X.append([a, b, c]); y.append(label)
    return X, y


def test_evaluate_metrics():
    m = ml.evaluate([1, 1, 0, 0], [1, 0, 0, 1])
    assert m["tp"] == 1 and m["fp"] == 1 and m["fn"] == 1 and m["tn"] == 1
    assert m["accuracy"] == 0.5 and m["precision"] == 0.5 and m["recall"] == 0.5


def test_train_and_compare_learns_and_beats_baseline():
    X, y = _separable()
    res = ml.train_and_compare(X, y, feature_names=["a", "b", "c"])
    assert res["trainable"] is True
    champ = next(m for m in res["models"] if m["name"] == res["champion"])
    base = next(m for m in res["models"] if m["name"] == "baseline_majority")
    assert champ["name"] != "baseline_majority"
    assert champ["f1"] > base["f1"]                 # a real model beats the majority floor
    assert champ["accuracy"] >= 0.75                # recovers the linear signal
    # the informative features (a, b) outrank the noise feature (c)
    fi = {d["feature"]: d["importance"] for d in champ["feature_importance"]}
    assert fi.get("a", 0) >= fi.get("c", 0)


def test_all_model_families_present():
    X, y = _separable()
    res = ml.train_and_compare(X, y)
    names = {m["name"] for m in res["models"]}
    assert names == {"baseline_majority", "logistic_regression", "decision_tree",
                     "random_forest", "gradient_boosting"}


def test_guards_degenerate_data():
    assert ml.train_and_compare([[1], [2]], [0, 1])["trainable"] is False        # too few rows
    assert ml.train_and_compare([[i] for i in range(8)], [1] * 8)["trainable"] is False  # one class


def test_cross_val_and_split_are_deterministic():
    X, y = _separable(60, seed=3)
    a = ml.cross_val_f1(ml.LogisticRegression, X, y, seed=1)
    b = ml.cross_val_f1(ml.LogisticRegression, X, y, seed=1)
    assert a == b                                   # deterministic
    Xtr, ytr, Xte, yte = ml.train_test_split(X, y, seed=42)
    assert len(Xtr) + len(Xte) == len(X) and len(Xte) >= 1


def test_models_predict_proba_in_range():
    X, y = _separable(40)
    for factory in ml.MODEL_FACTORIES.values():
        model = factory().fit(X, y)
        probs = model.predict_proba(X)
        assert all(0.0 <= p <= 1.0 for p in probs)
        preds = model.predict(X)
        assert set(preds) <= {0, 1}
