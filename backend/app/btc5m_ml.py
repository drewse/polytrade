"""Pure-Python ML toolkit for the BTC 5M Reversal Lab.

NO third-party ML deps (no numpy/sklearn) — keeps the Railway deploy lightweight
and the research module fully self-contained. Implements a small family of binary
classifiers so the Strategy Lab can "start simple, then compare":

  * MajorityBaseline   — predicts the training-majority class (a floor to beat)
  * LogisticRegression — L2-regularised batch gradient descent on standardised X
  * DecisionTree       — CART, gini split, depth/leaf limits
  * RandomForest       — bagged trees + per-split feature subsampling
  * GradientBoosting   — boosted depth-limited trees on logistic-loss residuals

Everything is deterministic (seeded) and read-only — this module trains models in
memory from feature rows and NEVER touches trading, orders, or production state.

Labels are binary {0, 1}. Multi-action BUY/SELL/NO_TRADE is layered on top by the
shadow strategy via probability thresholds, not here.
"""
from __future__ import annotations

import math
import random
from typing import Callable

Vector = list[float]
Matrix = list[Vector]


# ---------------------------------------------------------------------------
# data utilities
# ---------------------------------------------------------------------------
def train_test_split(X: Matrix, y: list[int], *, test_frac: float = 0.3,
                     seed: int = 1337) -> tuple[Matrix, list[int], Matrix, list[int]]:
    """Deterministic shuffle-split. Guarantees at least one row on each side when
    there are >= 2 rows so evaluation never divides by zero."""
    idx = list(range(len(X)))
    random.Random(seed).shuffle(idx)
    n_test = max(1, int(round(len(X) * test_frac))) if len(X) >= 2 else 0
    n_test = min(n_test, len(X) - 1) if len(X) >= 2 else 0
    test_i, train_i = idx[:n_test], idx[n_test:]
    return ([X[i] for i in train_i], [y[i] for i in train_i],
            [X[i] for i in test_i], [y[i] for i in test_i])


def _col_stats(X: Matrix) -> tuple[Vector, Vector]:
    n = len(X)
    d = len(X[0]) if X else 0
    mean = [0.0] * d
    for row in X:
        for j in range(d):
            mean[j] += row[j]
    mean = [m / n for m in mean] if n else mean
    std = [0.0] * d
    for row in X:
        for j in range(d):
            std[j] += (row[j] - mean[j]) ** 2
    std = [math.sqrt(s / n) if n else 1.0 for s in std]
    std = [s if s > 1e-9 else 1.0 for s in std]
    return mean, std


def standardize(X: Matrix, mean: Vector, std: Vector) -> Matrix:
    return [[(row[j] - mean[j]) / std[j] for j in range(len(row))] for row in X]


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def evaluate(y_true: list[int], y_pred: list[int]) -> dict:
    """Binary classification metrics for the positive class (label 1)."""
    tp = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 1)
    tn = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 0)
    fp = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 1)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 0)
    n = len(y_true) or 1
    acc = (tp + tn) / n
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0
    return {"accuracy": round(acc, 4), "precision": round(prec, 4),
            "recall": round(rec, 4), "f1": round(f1, 4),
            "tp": tp, "tn": tn, "fp": fp, "fn": fn, "n": len(y_true)}


# ---------------------------------------------------------------------------
# models — every model implements fit / predict / predict_proba /
# feature_importance / to_params, and carries a .name
# ---------------------------------------------------------------------------
class MajorityBaseline:
    name = "baseline_majority"

    def __init__(self):
        self.major = 0

    def fit(self, X: Matrix, y: list[int]):
        self.major = 1 if sum(y) * 2 >= len(y) else 0
        self._p = (sum(y) / len(y)) if y else 0.0
        return self

    def predict_proba(self, X: Matrix) -> Vector:
        return [self._p for _ in X]

    def predict(self, X: Matrix) -> list[int]:
        return [self.major for _ in X]

    def feature_importance(self) -> Vector:
        return []

    def to_params(self) -> dict:
        return {"major": self.major, "p": round(getattr(self, "_p", 0.0), 4)}


class LogisticRegression:
    name = "logistic_regression"

    def __init__(self, lr: float = 0.1, epochs: int = 300, l2: float = 0.001):
        self.lr, self.epochs, self.l2 = lr, epochs, l2
        self.w: Vector = []
        self.b = 0.0
        self.mean: Vector = []
        self.std: Vector = []

    @staticmethod
    def _sig(z: float) -> float:
        if z < -30:
            return 0.0
        if z > 30:
            return 1.0
        return 1.0 / (1.0 + math.exp(-z))

    def fit(self, X: Matrix, y: list[int]):
        if not X:
            return self
        self.mean, self.std = _col_stats(X)
        Xs = standardize(X, self.mean, self.std)
        d = len(Xs[0])
        self.w = [0.0] * d
        self.b = 0.0
        n = len(Xs)
        for _ in range(self.epochs):
            gw = [0.0] * d
            gb = 0.0
            for row, target in zip(Xs, y):
                z = self.b + sum(self.w[j] * row[j] for j in range(d))
                err = self._sig(z) - target
                for j in range(d):
                    gw[j] += err * row[j]
                gb += err
            for j in range(d):
                self.w[j] -= self.lr * (gw[j] / n + self.l2 * self.w[j])
            self.b -= self.lr * (gb / n)
        return self

    def predict_proba(self, X: Matrix) -> Vector:
        if not self.w:
            return [0.5 for _ in X]
        Xs = standardize(X, self.mean, self.std)
        return [self._sig(self.b + sum(self.w[j] * row[j] for j in range(len(row)))) for row in Xs]

    def predict(self, X: Matrix) -> list[int]:
        return [1 if p >= 0.5 else 0 for p in self.predict_proba(X)]

    def feature_importance(self) -> Vector:
        tot = sum(abs(w) for w in self.w) or 1.0
        return [round(abs(w) / tot, 4) for w in self.w]

    def to_params(self) -> dict:
        return {"w": [round(w, 4) for w in self.w], "b": round(self.b, 4)}


class _TreeNode:
    __slots__ = ("feat", "thr", "left", "right", "value")

    def __init__(self):
        self.feat = -1
        self.thr = 0.0
        self.left = None
        self.right = None
        self.value = 0.0   # P(y=1) at a leaf


def _gini(y: list[int]) -> float:
    if not y:
        return 0.0
    p = sum(y) / len(y)
    return 1.0 - (p * p + (1 - p) * (1 - p))


class DecisionTree:
    name = "decision_tree"

    def __init__(self, max_depth: int = 4, min_leaf: int = 3, feat_subset: int | None = None,
                 seed: int = 7):
        self.max_depth, self.min_leaf = max_depth, min_leaf
        self.feat_subset, self.seed = feat_subset, seed
        self.root: _TreeNode | None = None
        self.n_feats = 0
        self._imp: Vector = []

    def fit(self, X: Matrix, y: list[int]):
        self.n_feats = len(X[0]) if X else 0
        self._imp = [0.0] * self.n_feats
        self._rng = random.Random(self.seed)
        self.root = self._build(X, y, 0)
        tot = sum(self._imp) or 1.0
        self._imp = [v / tot for v in self._imp]
        return self

    def _leaf(self, y: list[int]) -> _TreeNode:
        node = _TreeNode()
        node.value = (sum(y) / len(y)) if y else 0.0
        return node

    def _build(self, X: Matrix, y: list[int], depth: int) -> _TreeNode:
        n = len(y)
        if depth >= self.max_depth or n < 2 * self.min_leaf or len(set(y)) == 1:
            return self._leaf(y)
        feats = list(range(self.n_feats))
        if self.feat_subset and self.feat_subset < self.n_feats:
            feats = self._rng.sample(feats, self.feat_subset)
        total_pos = sum(y)
        base = _gini(y) * n
        # Efficient CART split: per feature, sort once and sweep thresholds with
        # running class counts -> O(d * n log n) per node (no O(n^2) rescans).
        best = None  # (impurity, feat, thr)
        for f in feats:
            order = sorted(range(n), key=lambda i: X[i][f])
            vals = [X[i][f] for i in order]
            labs = [y[i] for i in order]
            left_pos = left_n = 0
            for k in range(n - 1):
                left_pos += labs[k]
                left_n += 1
                if vals[k] == vals[k + 1]:
                    continue
                right_n = n - left_n
                if left_n < self.min_leaf or right_n < self.min_leaf:
                    continue
                right_pos = total_pos - left_pos
                pl = left_pos / left_n
                pr = right_pos / right_n
                imp = (1 - pl * pl - (1 - pl) ** 2) * left_n + (1 - pr * pr - (1 - pr) ** 2) * right_n
                if best is None or imp < best[0]:
                    best = (imp, f, (vals[k] + vals[k + 1]) / 2.0)
        if best is None or (base - best[0]) <= 1e-12:
            return self._leaf(y)
        gain, f, thr = base - best[0], best[1], best[2]
        self._imp[f] += gain
        li = [i for i in range(n) if X[i][f] <= thr]
        ri = [i for i in range(n) if X[i][f] > thr]
        node = _TreeNode()
        node.feat, node.thr = f, thr
        node.left = self._build([X[i] for i in li], [y[i] for i in li], depth + 1)
        node.right = self._build([X[i] for i in ri], [y[i] for i in ri], depth + 1)
        return node

    def _prob_one(self, row: Vector) -> float:
        node = self.root
        while node and node.feat >= 0:
            node = node.left if row[node.feat] <= node.thr else node.right
        return node.value if node else 0.0

    def predict_proba(self, X: Matrix) -> Vector:
        return [self._prob_one(row) for row in X]

    def predict(self, X: Matrix) -> list[int]:
        return [1 if p >= 0.5 else 0 for p in self.predict_proba(X)]

    def feature_importance(self) -> Vector:
        return [round(v, 4) for v in self._imp]

    def to_params(self) -> dict:
        return {"max_depth": self.max_depth, "min_leaf": self.min_leaf}


class RandomForest:
    name = "random_forest"

    def __init__(self, n_trees: int = 9, max_depth: int = 4, min_leaf: int = 2, seed: int = 11):
        self.n_trees, self.max_depth, self.min_leaf, self.seed = n_trees, max_depth, min_leaf, seed
        self.trees: list[DecisionTree] = []
        self.n_feats = 0

    def fit(self, X: Matrix, y: list[int]):
        self.n_feats = len(X[0]) if X else 0
        sub = max(1, int(round(math.sqrt(self.n_feats)))) if self.n_feats else 1
        rng = random.Random(self.seed)
        self.trees = []
        n = len(X)
        for t in range(self.n_trees):
            bi = [rng.randrange(n) for _ in range(n)]      # bootstrap sample
            bx = [X[i] for i in bi]
            by = [y[i] for i in bi]
            tree = DecisionTree(max_depth=self.max_depth, min_leaf=self.min_leaf,
                                feat_subset=sub, seed=self.seed + t + 1)
            tree.fit(bx, by)
            self.trees.append(tree)
        return self

    def predict_proba(self, X: Matrix) -> Vector:
        if not self.trees:
            return [0.5 for _ in X]
        acc = [0.0] * len(X)
        for tree in self.trees:
            for i, p in enumerate(tree.predict_proba(X)):
                acc[i] += p
        return [a / len(self.trees) for a in acc]

    def predict(self, X: Matrix) -> list[int]:
        return [1 if p >= 0.5 else 0 for p in self.predict_proba(X)]

    def feature_importance(self) -> Vector:
        if not self.trees:
            return []
        agg = [0.0] * self.n_feats
        for tree in self.trees:
            imp = tree.feature_importance()
            for j in range(min(self.n_feats, len(imp))):
                agg[j] += imp[j]
        tot = sum(agg) or 1.0
        return [round(v / tot, 4) for v in agg]

    def to_params(self) -> dict:
        return {"n_trees": self.n_trees, "max_depth": self.max_depth}


class _RegStump:
    """Depth-limited regression tree on residuals (gradient boosting base learner)."""
    __slots__ = ("feat", "thr", "left", "right", "value", "lo", "hi")

    def __init__(self):
        self.feat = -1
        self.thr = 0.0
        self.left = None
        self.right = None
        self.value = 0.0


class GradientBoosting:
    name = "gradient_boosting"

    def __init__(self, n_rounds: int = 30, lr: float = 0.3, max_depth: int = 2, seed: int = 5):
        self.n_rounds, self.lr, self.max_depth, self.seed = n_rounds, lr, max_depth, seed
        self.init = 0.0
        self.trees: list[_RegStump] = []
        self.n_feats = 0
        self._imp: Vector = []

    @staticmethod
    def _sig(z: float) -> float:
        if z < -30:
            return 0.0
        if z > 30:
            return 1.0
        return 1.0 / (1.0 + math.exp(-z))

    def _fit_reg(self, X: Matrix, idx: list[int], resid: list[float], depth: int) -> _RegStump:
        node = _RegStump()
        m = len(idx)
        s_all = sum(resid[i] for i in idx)
        node.value = s_all / m if m else 0.0
        if depth >= self.max_depth or m < 4:
            return node
        sq_all = sum(resid[i] * resid[i] for i in idx)
        cur = sq_all - (s_all * s_all / m)        # total SSE at this node
        best = None
        # Efficient regression split: sort per feature, sweep with running
        # sum/sum-of-squares -> SSE in O(1) per threshold (O(d * n log n) per node).
        for f in range(self.n_feats):
            order = sorted(idx, key=lambda i: X[i][f])
            vals = [X[i][f] for i in order]
            rs = [resid[i] for i in order]
            sl = sql = 0.0
            nl = 0
            for k in range(m - 1):
                sl += rs[k]
                sql += rs[k] * rs[k]
                nl += 1
                if vals[k] == vals[k + 1] or nl < 2 or (m - nl) < 2:
                    continue
                nr = m - nl
                sr = s_all - sl
                sqr = sq_all - sql
                sse = (sql - sl * sl / nl) + (sqr - sr * sr / nr)
                if sse < cur and (best is None or sse < best[0]):
                    best = (sse, f, (vals[k] + vals[k + 1]) / 2.0)
        if best is None:
            return node
        sse, f, thr = best
        self._imp[f] += (cur - sse)
        li = [i for i in idx if X[i][f] <= thr]
        ri = [i for i in idx if X[i][f] > thr]
        node.feat, node.thr = f, thr
        node.left = self._fit_reg(X, li, resid, depth + 1)
        node.right = self._fit_reg(X, ri, resid, depth + 1)
        return node

    @staticmethod
    def _reg_pred(node: _RegStump, row: Vector) -> float:
        while node and node.feat >= 0:
            node = node.left if row[node.feat] <= node.thr else node.right
        return node.value if node else 0.0

    def fit(self, X: Matrix, y: list[int]):
        self.n_feats = len(X[0]) if X else 0
        self._imp = [0.0] * self.n_feats
        p = max(1e-6, min(1 - 1e-6, (sum(y) / len(y)) if y else 0.5))
        self.init = math.log(p / (1 - p))
        f = [self.init] * len(X)
        idx_all = list(range(len(X)))
        self.trees = []
        for _ in range(self.n_rounds):
            resid = [y[i] - self._sig(f[i]) for i in range(len(X))]
            tree = self._fit_reg(X, idx_all, resid, 0)
            self.trees.append(tree)
            for i in range(len(X)):
                f[i] += self.lr * self._reg_pred(tree, X[i])
        tot = sum(self._imp) or 1.0
        self._imp = [v / tot for v in self._imp]
        return self

    def predict_proba(self, X: Matrix) -> Vector:
        out = []
        for row in X:
            f = self.init + self.lr * sum(self._reg_pred(t, row) for t in self.trees)
            out.append(self._sig(f))
        return out

    def predict(self, X: Matrix) -> list[int]:
        return [1 if p >= 0.5 else 0 for p in self.predict_proba(X)]

    def feature_importance(self) -> Vector:
        return [round(v, 4) for v in self._imp]

    def to_params(self) -> dict:
        return {"n_rounds": self.n_rounds, "lr": self.lr, "max_depth": self.max_depth}


# Registry of model factories — the Strategy Lab trains and compares ALL of them.
MODEL_FACTORIES: dict[str, Callable[[], object]] = {
    "baseline_majority": MajorityBaseline,
    "logistic_regression": LogisticRegression,
    "decision_tree": DecisionTree,
    "random_forest": RandomForest,
    "gradient_boosting": GradientBoosting,
}


def cross_val_f1(factory: Callable[[], object], X: Matrix, y: list[int], *,
                 k: int = 3, seed: int = 99) -> float:
    """Mean held-out F1 across k folds (guards tiny datasets by reducing k)."""
    n = len(X)
    k = max(2, min(k, n))
    if n < 4 or len(set(y)) < 2:
        return 0.0
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    folds = [idx[i::k] for i in range(k)]
    scores = []
    for f in range(k):
        test_i = folds[f]
        train_i = [i for i in idx if i not in set(test_i)]
        if not test_i or not train_i or len(set(y[i] for i in train_i)) < 2:
            continue
        model = factory()
        model.fit([X[i] for i in train_i], [y[i] for i in train_i])
        pred = model.predict([X[i] for i in test_i])
        scores.append(evaluate([y[i] for i in test_i], pred)["f1"])
    return round(sum(scores) / len(scores), 4) if scores else 0.0


def train_and_compare(X: Matrix, y: list[int], *, feature_names: list[str] | None = None,
                      seed: int = 1337) -> dict:
    """Train every model, score on a held-out split + cross-validation, and pick a
    champion by held-out F1 (tie-break accuracy then CV). Returns the full
    leaderboard + champion — never overfits to train accuracy alone.

    Returns {} when the data can't support supervised learning (too few rows or a
    single class), so callers can fail closed."""
    if len(X) < 6 or len(set(y)) < 2:
        return {"trainable": False, "reason": "need >= 6 rows and both classes",
                "n_rows": len(X), "classes": sorted(set(y)), "models": [], "champion": None}
    # Cap rows for tractable pure-Python training (deterministic subsample). Keeps
    # the research batch fast on Railway without materially changing held-out scores.
    MAX_ROWS = 700
    n_full = len(X)
    if len(X) > MAX_ROWS:
        keep = list(range(len(X)))
        random.Random(seed).shuffle(keep)
        keep = keep[:MAX_ROWS]
        X = [X[i] for i in keep]
        y = [y[i] for i in keep]
    Xtr, ytr, Xte, yte = train_test_split(X, y, test_frac=0.3, seed=seed)
    if len(set(ytr)) < 2 or len(set(yte)) < 2:
        # reshuffle once with a different seed to try to get both classes per side
        Xtr, ytr, Xte, yte = train_test_split(X, y, test_frac=0.4, seed=seed + 1)
    results = []
    for mname, factory in MODEL_FACTORIES.items():
        model = factory()
        model.fit(Xtr, ytr)
        m_test = evaluate(yte, model.predict(Xte))
        m_train = evaluate(ytr, model.predict(Xtr))
        cv = cross_val_f1(factory, X, y, seed=seed)
        imp = model.feature_importance()
        fi = []
        if imp and feature_names and len(imp) == len(feature_names):
            fi = sorted([{"feature": feature_names[j], "importance": imp[j]}
                         for j in range(len(imp))], key=lambda d: -d["importance"])
        results.append({
            "name": mname,
            "accuracy": m_test["accuracy"], "precision": m_test["precision"],
            "recall": m_test["recall"], "f1": m_test["f1"],
            "train_accuracy": m_train["accuracy"], "cv_f1": cv,
            "n_train": len(ytr), "n_test": len(yte),
            "params": model.to_params(), "feature_importance": fi,
            # overfit gap: a big train-minus-test accuracy gap is a red flag
            "overfit_gap": round(m_train["accuracy"] - m_test["accuracy"], 4),
        })
    # The champion is the best LEARNED model (baseline is kept on the leaderboard
    # purely as a floor to beat — it never drives research/shadow predictions).
    base_f1 = next((r["f1"] for r in results if r["name"] == "baseline_majority"), 0.0)
    learned = [r for r in results if r["name"] != "baseline_majority"]
    champ = max(learned or results, key=lambda r: (r["f1"], r["accuracy"], r["cv_f1"]))
    return {"trainable": True, "models": results, "champion": champ["name"],
            "baseline_f1": base_f1, "champion_f1": champ["f1"],
            "beats_baseline": champ["f1"] > base_f1,
            "n_rows": len(X), "n_rows_full": n_full, "n_features": len(X[0])}
