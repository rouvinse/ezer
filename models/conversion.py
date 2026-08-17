"""
ACIA — Conversion Scoring Model
Predicts probability that a customer upgrades their plan.
Targets Free → paid and Starter → Pro/Enterprise conversions.
"""

import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import roc_auc_score, classification_report

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from data.features import build_customer_features, get_all_feature_cols

MODEL_DIR  = Path(__file__).parent / "artifacts"
MODEL_DIR.mkdir(exist_ok=True)
CONV_MODEL_PATH = MODEL_DIR / "conversion_model.pkl"


def _build_conversion_label(df: pd.DataFrame) -> pd.Series:
    """
    Conversion = customer is on Pro or Enterprise (i.e. already converted),
    or showed upgrade intent signals.
    We use plan_rank >= 2 as a proxy for "converted" customers to train on.
    """
    return (df["plan_rank"] >= 2).astype(int)


def train_conversion_model(verbose: bool = True, company_id: str | None = None) -> dict:
    df = build_customer_features(company_id=company_id)

    # Only score non-Enterprise (they've already maxed out)
    df_target = df[df["plan_rank"] < 3].copy()
    y = _build_conversion_label(df_target)

    feat_cols = get_all_feature_cols()
    # Drop plan_rank to avoid data leakage
    feat_cols_clean = [c for c in feat_cols if c != "plan_rank"]
    X = df_target[feat_cols_clean].values

    if y.sum() < 10:
        raise ValueError("Not enough positive conversion samples to train.")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    candidates = {
        "logistic": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(C=0.5, max_iter=500, random_state=42))
        ]),
        "random_forest": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", RandomForestClassifier(
                n_estimators=150, max_depth=6,
                min_samples_leaf=5, random_state=42
            ))
        ]),
        "gradient_boost": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", GradientBoostingClassifier(
                n_estimators=100, max_depth=3,
                learning_rate=0.1, random_state=42
            ))
        ]),
    }

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    best_name, best_model, best_auc = None, None, 0
    results = {}

    for name, pipe in candidates.items():
        scores = cross_val_score(pipe, X_train, y_train, cv=cv, scoring="roc_auc")
        results[name] = scores.mean()
        if scores.mean() > best_auc:
            best_auc  = scores.mean()
            best_name = name
            best_model = pipe

    best_model.fit(X_train, y_train)
    y_prob = best_model.predict_proba(X_test)[:, 1]
    test_auc = roc_auc_score(y_test, y_prob)
    report   = classification_report(y_test, (y_prob >= 0.5).astype(int), output_dict=True)

    # Feature importances
    clf = best_model.named_steps["clf"]
    feat_imp = {}
    if hasattr(clf, "feature_importances_"):
        imp = clf.feature_importances_
        feat_imp = dict(sorted(
            zip(feat_cols_clean, imp), key=lambda x: x[1], reverse=True
        )[:10])

    artifact = {
        "model":         best_model,
        "model_name":    best_name,
        "feature_cols":  feat_cols_clean,
        "trained_at":    datetime.now().isoformat(),
        "cv_results":    results,
        "test_auc":      test_auc,
        "report":        report,
        "feat_importance": feat_imp,
    }
    with open(CONV_MODEL_PATH, "wb") as f:
        pickle.dump(artifact, f)

    if verbose:
        print(f"\n── Conversion Model Training ────────────────────────")
        print(f"  CV ROC-AUC scores:")
        for n, s in results.items():
            marker = " ← selected" if n == best_name else ""
            print(f"    {n:20s}: {s:.4f}{marker}")
        print(f"  Test ROC-AUC   : {test_auc:.4f}")
        print(f"  Conversion rate: {y.mean():.1%}")
        if feat_imp:
            print(f"  Top features   :")
            for f, v in list(feat_imp.items())[:5]:
                print(f"    {f:30s}: {v:.4f}")
        print(f"  Saved → {CONV_MODEL_PATH}\n")

    return artifact


def load_conversion_model() -> dict:
    if not CONV_MODEL_PATH.exists():
        train_conversion_model(verbose=False)
    with open(CONV_MODEL_PATH, "rb") as f:
        return pickle.load(f)


def predict_conversion(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Returns customer_id + conversion_score (0–1) + conversion_tier label.
    Only scores Free and Starter customers (others already converted).
    """
    artifact = load_conversion_model()
    model    = artifact["model"]
    cols     = artifact["feature_cols"]

    if df is None:
        df = build_customer_features()

    # Score all non-Enterprise
    df_score = df[df["plan_rank"] < 3].copy()
    X = df_score[cols].values
    scores = model.predict_proba(X)[:, 1]

    tier = pd.cut(scores, bins=[0, 0.35, 0.65, 1.0],
                  labels=["Cold", "Warm", "Hot"])

    result = pd.DataFrame({
        "customer_id":       df_score["customer_id"].values,
        "conversion_score":  scores.round(4),
        "conversion_tier":   tier,
        "current_plan":      df_score["plan"].values,
    })

    # Fill in Enterprise customers with score=1 (already converted)
    enterprise = df[df["plan_rank"] == 3][["customer_id", "plan"]].copy()
    enterprise["conversion_score"] = 1.0
    enterprise["conversion_tier"]  = "Hot"
    enterprise.rename(columns={"plan": "current_plan"}, inplace=True)

    return pd.concat([result, enterprise], ignore_index=True)


if __name__ == "__main__":
    art = train_conversion_model()
    preds = predict_conversion()
    print(preds.head(10).to_string(index=False))
    print(f"\nConversion tier distribution:\n{preds['conversion_tier'].value_counts().to_string()}")
