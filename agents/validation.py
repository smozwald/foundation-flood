#!/usr/bin/env python3
"""
validation.py — Location-disjoint cross-validation harness for Phase 2.

THE single most important Phase 2 infra piece. Phase 1's headline pixel
AUC of 0.991 was leakage: Leave-One-Event-Out (LOEO) withheld an *event*
but every pixel *location* still appeared in 9 of 10 training folds, so the
model memorised each location's flood propensity rather than generalising
(reports/phase_one_analysis.md, "Why the 0.991 pixel AUC is a red flag").

This module writes the split ONCE so no Phase 2 model rolls its own. The
default path is location-disjoint by zone: no field/zone appears in both
the train and test side of any fold. LOEO is provided only as an explicitly
labelled, known-optimistic upper bound — never the headline.

Design contract (phase_2_plan.md §2 metrics, §5 guardrails, §8.3):
  - Location-disjoint by default, grouping key = zone_id.
  - Budget K (how many fields flood at a given Y%) comes from train/external
    priors only, never held-out truth. Accept a budget_k_fn computed on train.
  - Returns §2 metrics: ranking AUC (target >= 0.65) + a hook to compare
    calibrated field-% RMSE against the uniform-spread-of-Y% null baseline.
  - Pure functions, deterministic seed, no DB writes.
  - Prints fold composition (which zones in test) so disjointness is auditable.

No DB, no GEE, no side effects beyond stdout.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Iterator, Optional

import numpy as np
import pandas as pd

try:
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
except ImportError:  # pragma: no cover - sklearn is a modelling dependency
    roc_auc_score = None
    GroupKFold = None


DEFAULT_SEED = 42


# ---------------------------------------------------------------------------
# Splitters
# ---------------------------------------------------------------------------

def _check_group_col(df: pd.DataFrame, group_col: str) -> None:
    if group_col not in df.columns:
        raise KeyError(
            f"group_col {group_col!r} not in df columns {list(df.columns)}. "
            "Location-disjoint splitting requires a location/zone grouping key."
        )


def _assert_disjoint(df: pd.DataFrame, group_col: str,
                     train_idx: np.ndarray, test_idx: np.ndarray,
                     fold_label: str) -> None:
    """Hard guard: no group (location) may appear in both train and test."""
    train_groups = set(df.iloc[train_idx][group_col].unique())
    test_groups = set(df.iloc[test_idx][group_col].unique())
    overlap = train_groups & test_groups
    if overlap:
        raise AssertionError(
            f"LOCATION LEAKAGE in {fold_label}: groups present in BOTH "
            f"train and test on {group_col!r}: {sorted(overlap)}. "
            "This is exactly the Phase 1 failure mode — aborting."
        )


def _print_fold(fold_label: str, df: pd.DataFrame, group_col: str,
                train_idx: np.ndarray, test_idx: np.ndarray) -> None:
    test_groups = sorted(map(str, df.iloc[test_idx][group_col].unique()))
    n_train_groups = df.iloc[train_idx][group_col].nunique()
    print(
        f"  [{fold_label}] test {group_col}(s)={test_groups} "
        f"| n_test={len(test_idx)} rows / {len(test_groups)} zones "
        f"| n_train={len(train_idx)} rows / {n_train_groups} zones"
    )


def leave_one_zone_out(df: pd.DataFrame,
                       group_col: str = "zone_id",
                       verbose: bool = True) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """
    DEFAULT, location-disjoint split. Each fold withholds one entire zone.

    No zone appears in both train and test of a fold, so the model is forced
    to generalise to a genuinely unseen location — the property Phase 1's
    LOEO lacked. This is the headline split for all Phase 2 metrics.

    Args:
        df: dataframe; must contain `group_col`.
        group_col: location grouping key. Default "zone_id".
        verbose: print fold composition (which zones held out) for audit.

    Yields:
        (train_idx, test_idx) positional integer index arrays into df, one
        per held-out zone, in sorted zone order (deterministic).
    """
    _check_group_col(df, group_col)
    zones = sorted(df[group_col].dropna().unique())
    if verbose:
        print(f"leave_one_zone_out: {len(zones)} zone(s), {len(df)} rows "
              f"[location-disjoint by {group_col}]")
    if len(zones) < 2:
        print(
            f"WARNING: only {len(zones)} zone(s). Leave-one-zone-out cannot "
            "produce a location-disjoint train set with a single zone. Collect "
            "more zones, or this reduces to a non-generalisation check."
        )

    positions = np.arange(len(df))
    group_vals = df[group_col].to_numpy()
    for zone in zones:
        test_mask = group_vals == zone
        test_idx = positions[test_mask]
        train_idx = positions[~test_mask]
        if len(train_idx) == 0:
            continue
        label = f"LOZO holdout={zone}"
        _assert_disjoint(df, group_col, train_idx, test_idx, label)
        if verbose:
            _print_fold(label, df, group_col, train_idx, test_idx)
        yield train_idx, test_idx


def spatial_block_cv(df: pd.DataFrame,
                     group_col: str = "zone_id",
                     n_splits: int = 5,
                     seed: int = DEFAULT_SEED,
                     verbose: bool = True) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """
    Location-disjoint grouped K-fold on zone, for when zones are too few or
    too many for clean leave-one-zone-out. Zones are partitioned into blocks;
    each fold holds out one block of whole zones. Still location-disjoint:
    no zone spans the train/test boundary.

    Args:
        df: dataframe; must contain `group_col`.
        group_col: location grouping key. Default "zone_id".
        n_splits: number of folds. Clamped to the number of distinct zones.
        seed: deterministic seed for zone-to-block assignment.
        verbose: print fold composition for audit.

    Yields:
        (train_idx, test_idx) positional integer index arrays into df.
    """
    _check_group_col(df, group_col)
    n_zones = df[group_col].nunique()
    if n_zones < 2:
        raise ValueError(
            f"spatial_block_cv needs >= 2 zones for a location-disjoint split; "
            f"got {n_zones}. Use more zones or accept there is no held-out location."
        )

    effective_splits = min(n_splits, n_zones)
    if effective_splits < n_splits and verbose:
        print(f"spatial_block_cv: clamping n_splits {n_splits} -> "
              f"{effective_splits} (only {n_zones} zones).")

    groups = df[group_col].to_numpy()

    if GroupKFold is not None:
        # GroupKFold is deterministic and never splits a group across folds.
        # It ignores `seed`; for stable block membership we pre-sort by a
        # seeded shuffle of zone labels so folds are reproducible yet not
        # tied to row order.
        rng = np.random.default_rng(seed)
        zones = sorted(df[group_col].dropna().unique())
        shuffled = list(zones)
        rng.shuffle(shuffled)
        zone_to_rank = {z: i for i, z in enumerate(shuffled)}
        ranked_groups = np.array([zone_to_rank.get(g, -1) for g in groups])
        splitter = GroupKFold(n_splits=effective_splits)
        folds = splitter.split(np.zeros(len(df)), groups=ranked_groups)
    else:  # pragma: no cover - fallback without sklearn
        folds = _manual_group_kfold(groups, effective_splits, seed)

    if verbose:
        print(f"spatial_block_cv: {effective_splits} folds over {n_zones} "
              f"zones, {len(df)} rows [location-disjoint by {group_col}]")

    for i, (train_idx, test_idx) in enumerate(folds):
        train_idx = np.asarray(train_idx)
        test_idx = np.asarray(test_idx)
        label = f"block fold {i + 1}/{effective_splits}"
        _assert_disjoint(df, group_col, train_idx, test_idx, label)
        if verbose:
            _print_fold(label, df, group_col, train_idx, test_idx)
        yield train_idx, test_idx


def _manual_group_kfold(groups: np.ndarray, n_splits: int,
                        seed: int) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Deterministic grouped K-fold without sklearn. Whole zones per block."""
    positions = np.arange(len(groups))
    zones = sorted(set(groups.tolist()))
    rng = np.random.default_rng(seed)
    shuffled = list(zones)
    rng.shuffle(shuffled)
    blocks: list[list] = [[] for _ in range(n_splits)]
    for i, z in enumerate(shuffled):
        blocks[i % n_splits].append(z)
    for block in blocks:
        test_zones = set(block)
        test_mask = np.array([g in test_zones for g in groups])
        yield positions[~test_mask], positions[test_mask]


def leave_one_event_out_OPTIMISTIC(
        df: pd.DataFrame,
        event_col: str = "flood_event_id",
        verbose: bool = True) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """
    KNOWN-OPTIMISTIC UPPER BOUND — NOT a headline metric.

    Leave-One-Event-Out: each fold withholds one event but every LOCATION
    (zone/field) still appears on both sides of the boundary. This is the
    exact split that leaked in Phase 1 (0.991 pixel AUC). It is provided ONLY
    so a model can report an optimistic ceiling alongside the honest
    location-disjoint number. The function name carries _OPTIMISTIC, and
    evaluate_ranking flags any result from it as optimistic_upper_bound=True.

    DO NOT use this as the success-criterion split. Use leave_one_zone_out.

    Yields:
        (train_idx, test_idx) per held-out event.
    """
    if event_col not in df.columns:
        raise KeyError(
            f"event_col {event_col!r} not in df columns {list(df.columns)}.")
    print("WARNING: leave_one_event_out_OPTIMISTIC is a KNOWN-LEAKY upper "
          "bound (locations span train/test). NOT a headline metric. "
          "Use leave_one_zone_out for the contract metrics.")
    positions = np.arange(len(df))
    event_vals = df[event_col].to_numpy()
    events = sorted(df[event_col].dropna().unique(), key=str)
    for ev in events:
        test_mask = event_vals == ev
        test_idx = positions[test_mask]
        train_idx = positions[~test_mask]
        if len(train_idx) == 0 or len(test_idx) == 0:
            continue
        if verbose:
            print(f"  [LOEO* holdout event={ev}] (LEAKY: locations shared) "
                  f"n_test={len(test_idx)} n_train={len(train_idx)}")
        yield train_idx, test_idx


# ---------------------------------------------------------------------------
# Budget K helpers (train/external priors only — never held-out truth, §5)
# ---------------------------------------------------------------------------

def budget_k_from_train_rate(label_col: str,
                             threshold: float = 0.5) -> Callable[..., int]:
    """
    Build a budget_k_fn that derives K (number of fields expected to flood)
    from the TRAINING fold's prior flood rate only.

    K = round(train_positive_rate * n_test). The test labels are never read.
    This honours §5: budget comes from train/external priors, not held-out truth.

    Args:
        label_col: continuous flood_pct column used to define a positive
            (flood_pct >= threshold) for rate estimation.
        threshold: flood_pct cutoff counting a field as "flooded" for the prior.

    Returns:
        budget_k_fn(train_df, test_df) -> int
    """
    def _fn(train_df: pd.DataFrame, test_df: pd.DataFrame) -> int:
        rate = float((train_df[label_col].to_numpy() >= threshold).mean())
        return int(round(rate * len(test_df)))
    return _fn


def budget_k_from_external_y(y_fraction: float) -> Callable[..., int]:
    """
    Build a budget_k_fn from an EXTERNAL Y% (e.g. JRC/Flood Hub area-flooded
    for a chosen return period). K = round(y_fraction * n_test). The external
    magnitude is the architecture's source of "how much" (plan §1); held-out
    truth is never consulted.

    Args:
        y_fraction: expected flooded area fraction in [0, 1] from the external
            hazard model for the chosen return period.
    """
    def _fn(train_df: pd.DataFrame, test_df: pd.DataFrame) -> int:
        return int(round(y_fraction * len(test_df)))
    return _fn


# ---------------------------------------------------------------------------
# Metrics (plan §2 contract)
# ---------------------------------------------------------------------------

def _binarise(y: np.ndarray, threshold: float) -> np.ndarray:
    return (np.asarray(y, dtype=float) >= threshold).astype(int)


def _safe_auc(y_true_bin: np.ndarray, scores: np.ndarray) -> Optional[float]:
    """ROC AUC, returns None when a single class is present (undefined)."""
    if roc_auc_score is None:
        raise ImportError("scikit-learn is required for ranking AUC.")
    if len(np.unique(y_true_bin)) < 2:
        return None
    return float(roc_auc_score(y_true_bin, scores))


def uniform_spread_rmse(y_true_pct: np.ndarray, y_fraction: float) -> float:
    """
    RMSE of the NULL architecture: spread external Y% uniformly across all
    fields (every field predicted = y_fraction). This is the baseline the
    calibrated, rank-based field-% must beat (plan §2, row 2).

    Args:
        y_true_pct: true flood_pct per field, in [0, 1].
        y_fraction: external Y% expressed as a fraction in [0, 1].
    """
    y_true = np.asarray(y_true_pct, dtype=float)
    pred = np.full_like(y_true, fill_value=float(y_fraction))
    return float(np.sqrt(np.mean((y_true - pred) ** 2)))


def calibrated_rmse_from_rank(y_true_pct: np.ndarray,
                              scores: np.ndarray,
                              y_fraction: float) -> float:
    """
    RMSE of rank-calibrated field-% against truth. Distributes the external
    Y% budget across fields by model rank, constrained so the field-average
    equals Y% (plan §1 calibration step), then scores against truth.

    Top-K fields (K = round(y_fraction * n)) receive the budget; here we use a
    simple rank-proportional spread normalised to mean = y_fraction so the
    constraint holds exactly. This is a *hook* for §2 row 2 — calibration
    itself is owned by the calibration brief; this gives the harness a
    comparable number against uniform_spread_rmse.

    Args:
        y_true_pct: true flood_pct per field, in [0, 1].
        scores: model vulnerability scores (higher = floods first).
        y_fraction: external Y% as a fraction in [0, 1].
    """
    y_true = np.asarray(y_true_pct, dtype=float)
    s = np.asarray(scores, dtype=float)
    n = len(s)
    if n == 0:
        return float("nan")
    # Rank in [0, 1]; ties broken by order (deterministic via argsort).
    order = np.argsort(np.argsort(s))
    rank01 = order / max(n - 1, 1)
    raw = rank01
    mean_raw = raw.mean()
    if mean_raw <= 0:
        pred = np.full(n, float(y_fraction))
    else:
        pred = raw * (float(y_fraction) / mean_raw)
        pred = np.clip(pred, 0.0, 1.0)
    return float(np.sqrt(np.mean((y_true - pred) ** 2)))


def _spearman(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    """Spearman rho via Pearson on ranks; no scipy dependency."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) < 2:
        return None
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def evaluate_ranking(model_factory: Callable[[], object],
                     df: pd.DataFrame,
                     features: list[str],
                     label_col: str,
                     splitter: Callable[..., Iterator[tuple[np.ndarray, np.ndarray]]] = leave_one_zone_out,
                     budget_k_fn: Optional[Callable[..., int]] = None,
                     group_col: str = "zone_id",
                     flood_threshold: float = 0.5,
                     external_y_fraction: Optional[float] = None,
                     seed: int = DEFAULT_SEED,
                     verbose: bool = True) -> dict:
    """
    Fit `model_factory()` per fold and return the plan §2 contract metrics
    under a location-disjoint split (by default).

    The model is asked only to RANK fields (vulnerability / floods-first), not
    to predict absolute %. Magnitude comes from the external Y% (plan §1).

    Args:
        model_factory: zero-arg callable returning a fresh estimator each fold.
            Must support .fit(X, y) and produce per-row scores via
            predict_proba (binary) or predict / decision_function.
        df: field_dataset-style frame; one row per (field x event). Must
            contain `features`, `label_col`, and `group_col`.
        features: pinned feature names (see agents/feature_config.py).
        label_col: continuous flood_pct column in [0, 1].
        splitter: a splitter from this module. DEFAULT leave_one_zone_out
            (location-disjoint). Pass leave_one_event_out_OPTIMISTIC only to
            report the known-optimistic upper bound — the result is flagged.
        budget_k_fn: callable(train_df, test_df) -> int giving the flood budget
            K from TRAIN/EXTERNAL priors only (§5). If None, derived from the
            train flood rate (never from held-out truth).
        group_col: location grouping key for disjointness (default "zone_id").
        flood_threshold: flood_pct cutoff defining a positive for AUC.
        external_y_fraction: external Y% (fraction) for the calibrated-vs-uniform
            RMSE comparison (§2 row 2). If None, taken from the train flood rate
            per fold (still never held-out truth).
        seed: deterministic seed; passed to the model factory if it accepts one
            is the caller's responsibility — we seed numpy here for determinism.
        verbose: print fold composition + per-fold metrics.

    Returns:
        dict with pooled and per-fold §2 metrics:
          ranking_auc_pooled, ranking_auc_mean, ranking_auc_per_fold,
          meets_auc_target (>= 0.65), calibrated_rmse, uniform_rmse,
          beats_uniform_spread, rank_stability_spearman (Cat-1 vs Cat-5 hook),
          split, group_col, optimistic_upper_bound, n_folds, fold_composition.
    """
    if roc_auc_score is None:
        raise ImportError("scikit-learn is required for evaluate_ranking.")
    missing = [f for f in features if f not in df.columns]
    if missing:
        raise KeyError(f"features missing from df: {missing}")
    if label_col not in df.columns:
        raise KeyError(f"label_col {label_col!r} not in df.")

    np.random.seed(seed)
    split_name = getattr(splitter, "__name__", str(splitter))
    optimistic = "OPTIMISTIC" in split_name
    if optimistic:
        print("NOTE: evaluating under a KNOWN-OPTIMISTIC split "
              f"({split_name}). Result flagged optimistic_upper_bound=True; "
              "do NOT report as the headline contract metric.")
    if budget_k_fn is None:
        budget_k_fn = budget_k_from_train_rate(label_col, flood_threshold)

    # Build the splitter generator with the right signature.
    if split_name == "leave_one_event_out_OPTIMISTIC":
        folds = splitter(df, verbose=verbose)
    elif split_name == "spatial_block_cv":
        folds = splitter(df, group_col=group_col, seed=seed, verbose=verbose)
    else:
        folds = splitter(df, group_col=group_col, verbose=verbose)

    all_true_bin: list[np.ndarray] = []
    all_scores: list[np.ndarray] = []
    auc_per_fold: list[Optional[float]] = []
    calib_rmses: list[float] = []
    unif_rmses: list[float] = []
    budgets_k: list[int] = []
    fold_composition: list[dict] = []

    for fi, (train_idx, test_idx) in enumerate(folds):
        train_df = df.iloc[train_idx]
        test_df = df.iloc[test_idx]

        X_train = train_df[features].to_numpy()
        X_test = test_df[features].to_numpy()
        y_train_cont = train_df[label_col].to_numpy(dtype=float)
        y_test_cont = test_df[label_col].to_numpy(dtype=float)
        y_train_bin = _binarise(y_train_cont, flood_threshold)
        y_test_bin = _binarise(y_test_cont, flood_threshold)

        model = model_factory()
        # Classifiers want binary labels; regressors want continuous. Try
        # binary first (ranking is a classification-style task), fall back.
        try:
            model.fit(X_train, y_train_bin)
        except Exception:
            model.fit(X_train, y_train_cont)
        scores = _model_scores(model, X_test)

        auc = _safe_auc(y_test_bin, scores)
        auc_per_fold.append(auc)
        all_true_bin.append(y_test_bin)
        all_scores.append(scores)

        # Budget K from train/external priors only.
        k = int(budget_k_fn(train_df, test_df))
        budgets_k.append(k)

        # Calibrated-vs-uniform RMSE hook (§2 row 2).
        if external_y_fraction is not None:
            yf = float(external_y_fraction)
        else:
            yf = float((y_train_cont >= flood_threshold).mean())  # train prior
        calib_rmses.append(calibrated_rmse_from_rank(y_test_cont, scores, yf))
        unif_rmses.append(uniform_spread_rmse(y_test_cont, yf))

        comp = {
            "fold": fi,
            "test_zones": sorted(map(str, test_df[group_col].unique()))
            if group_col in test_df.columns else None,
            "n_test": int(len(test_idx)),
            "n_train": int(len(train_idx)),
            "budget_k": k,
            "auc": auc,
        }
        fold_composition.append(comp)
        if verbose:
            auc_str = f"{auc:.3f}" if auc is not None else "n/a(1 class)"
            print(f"  -> fold {fi}: AUC={auc_str} budget_K={k} "
                  f"calib_RMSE={calib_rmses[-1]:.4f} "
                  f"unif_RMSE={unif_rmses[-1]:.4f}")

    if not all_scores:
        raise ValueError("No folds produced — check zones/events in df.")

    pooled_true = np.concatenate(all_true_bin)
    pooled_scores = np.concatenate(all_scores)
    pooled_auc = _safe_auc(pooled_true, pooled_scores)
    valid_aucs = [a for a in auc_per_fold if a is not None]
    auc_mean = float(np.mean(valid_aucs)) if valid_aucs else None

    calib_rmse = float(np.mean(calib_rmses)) if calib_rmses else float("nan")
    unif_rmse = float(np.mean(unif_rmses)) if unif_rmses else float("nan")

    stability = _rank_stability_hook(df, label_col, group_col)

    headline_auc = pooled_auc if pooled_auc is not None else auc_mean
    result = {
        "split": split_name,
        "group_col": group_col,
        "optimistic_upper_bound": optimistic,
        "n_folds": len(all_scores),
        "ranking_auc_pooled": pooled_auc,
        "ranking_auc_mean": auc_mean,
        "ranking_auc_per_fold": auc_per_fold,
        "meets_auc_target": (headline_auc is not None and headline_auc >= 0.65
                             and not optimistic),
        "auc_target": 0.65,
        "calibrated_rmse": calib_rmse,
        "uniform_rmse": unif_rmse,
        "beats_uniform_spread": (calib_rmse < unif_rmse),
        "rank_stability_spearman": stability,
        "rank_stability_target": 0.6,
        "budget_k_per_fold": budgets_k,
        "fold_composition": fold_composition,
        "seed": seed,
    }

    if verbose:
        _print_summary(result)
    return result


def _model_scores(model: object, X: np.ndarray) -> np.ndarray:
    """Extract a per-row ranking score from a fitted estimator."""
    if hasattr(model, "predict_proba"):
        proba = model.predict_proba(X)
        proba = np.asarray(proba)
        if proba.ndim == 2 and proba.shape[1] >= 2:
            return proba[:, -1]
        return proba.ravel()
    if hasattr(model, "decision_function"):
        return np.asarray(model.decision_function(X)).ravel()
    return np.asarray(model.predict(X)).ravel()


def _rank_stability_hook(df: pd.DataFrame, label_col: str,
                         group_col: str) -> Optional[float]:
    """
    Cat-1 vs Cat-5 rank-stability hook (plan §2 row 3, target rho >= 0.6).

    Correlates per-field mean flood_pct between low- and high-magnitude
    events when a category column is present. Returns None if the data does
    not carry the category split (the model brief supplies it). This is a
    hook so the harness exposes the metric slot; full computation needs the
    event-category labels.
    """
    cat_col = next((c for c in ("category", "event_category", "magnitude_cat")
                    if c in df.columns), None)
    field_col = next((c for c in ("field_id", "field") if c in df.columns), None)
    if cat_col is None or field_col is None:
        return None
    cats = df[cat_col].astype(str)
    is_high = cats.str.contains("5") | cats.str.lower().str.contains("cat-5")
    low = df[~is_high].groupby(field_col)[label_col].mean()
    high = df[is_high].groupby(field_col)[label_col].mean()
    common = low.index.intersection(high.index)
    if len(common) < 2:
        return None
    return _spearman(low.loc[common].to_numpy(), high.loc[common].to_numpy())


def _print_summary(result: dict) -> None:
    print("\n=== evaluate_ranking summary (plan §2 contract) ===")
    print(f"  split                 : {result['split']}"
          + ("  [OPTIMISTIC UPPER BOUND — not headline]"
             if result["optimistic_upper_bound"] else "  [location-disjoint]"))
    print(f"  group_col             : {result['group_col']}")
    print(f"  n_folds               : {result['n_folds']}")
    pooled = result["ranking_auc_pooled"]
    mean = result["ranking_auc_mean"]
    print(f"  ranking AUC (pooled)  : "
          f"{pooled:.3f}" if pooled is not None else "  ranking AUC (pooled)  : n/a")
    print(f"  ranking AUC (mean)    : "
          f"{mean:.3f}" if mean is not None else "  ranking AUC (mean)    : n/a")
    print(f"  meets AUC>=0.65       : {result['meets_auc_target']}")
    print(f"  calibrated field-RMSE : {result['calibrated_rmse']:.4f}")
    print(f"  uniform-spread RMSE   : {result['uniform_rmse']:.4f}")
    print(f"  beats uniform spread  : {result['beats_uniform_spread']}")
    rs = result["rank_stability_spearman"]
    print(f"  rank stability (rho)  : "
          + (f"{rs:.3f}" if rs is not None else "n/a (no category col)"))
    print("====================================================\n")


# ---------------------------------------------------------------------------
# Self-test (synthetic data; no DB)
# ---------------------------------------------------------------------------

def _make_synthetic(n_zones: int = 4, fields_per_zone: int = 40,
                    events_per_zone: int = 3, seed: int = DEFAULT_SEED) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for z in range(n_zones):
        zone = f"zone_{z:03d}"
        for f in range(fields_per_zone):
            field = f"{zone}_field_{f:03d}"
            # A real terrain signal: low elevation / low hand floods first.
            elev = rng.normal(100, 10)
            hand = rng.exponential(5)
            for e in range(events_per_zone):
                event = f"{zone}_event_{e}"
                cat = "cat-5" if e == 0 else "cat-1"
                base = 1.0 / (1.0 + np.exp(0.3 * (hand - 5) + 0.05 * (elev - 100)))
                noise = rng.normal(0, 0.1)
                flood = float(np.clip(base + noise, 0, 1))
                rows.append({
                    "field_id": field,
                    "flood_event_id": event,
                    "zone_id": zone,
                    "category": cat,
                    "elevation": elev,
                    "hand": hand,
                    "slope": rng.normal(3, 1),
                    "flood_pct": flood,
                })
    return pd.DataFrame(rows)


def _self_test(single: bool) -> int:
    print("validation.py self-test (synthetic field_dataset)\n")
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        print("ERROR: scikit-learn not installed; cannot run self-test.")
        return 1

    n_zones = 2 if single else 4
    df = _make_synthetic(n_zones=n_zones)
    features = ["elevation", "hand", "slope"]

    print("--- leave_one_zone_out (DEFAULT, location-disjoint) ---")
    factory = lambda: RandomForestClassifier(
        n_estimators=80, max_depth=6, min_samples_leaf=5, random_state=DEFAULT_SEED)
    res = evaluate_ranking(factory, df, features, "flood_pct",
                           splitter=leave_one_zone_out,
                           external_y_fraction=0.3)
    assert res["split"] == "leave_one_zone_out"
    assert res["optimistic_upper_bound"] is False
    for comp in res["fold_composition"]:
        assert len(comp["test_zones"]) == 1, "LOZO test fold must be one zone"

    if not single:
        print("--- spatial_block_cv (location-disjoint grouped K-fold) ---")
        res_b = evaluate_ranking(factory, df, features, "flood_pct",
                                 splitter=spatial_block_cv,
                                 external_y_fraction=0.3)
        assert res_b["split"] == "spatial_block_cv"

    print("--- leave_one_event_out_OPTIMISTIC (labelled upper bound) ---")
    res_o = evaluate_ranking(factory, df, features, "flood_pct",
                             splitter=leave_one_event_out_OPTIMISTIC,
                             external_y_fraction=0.3)
    assert res_o["optimistic_upper_bound"] is True
    assert res_o["meets_auc_target"] is False, \
        "optimistic split must never report meets_auc_target=True"

    print("--- disjointness guard fires on a leaky split ---")
    leaked = False
    try:
        bad_train = np.arange(len(df))           # all rows
        bad_test = np.arange(10)                  # overlaps train
        _assert_disjoint(df, "zone_id", bad_train, bad_test, "deliberate-leak")
    except AssertionError:
        leaked = True
    assert leaked, "disjointness guard failed to catch overlap"

    print("\nAll self-test assertions passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Location-disjoint validation harness (Phase 2 T4). "
                    "Run with --test for a synthetic self-test.")
    parser.add_argument("--test", action="store_true",
                        help="Run a single-zone-pair synthetic self-test.")
    parser.add_argument("--self-test", action="store_true",
                        help="Run the full multi-zone synthetic self-test.")
    args = parser.parse_args()
    try:
        return _self_test(single=args.test and not args.self_test)
    except AssertionError as exc:
        print(f"SELF-TEST FAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
