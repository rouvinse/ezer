"""
ACIA — Churn Prediction Model
Trains a churn classifier and persists it for inference.
Outputs a churn_score (0–1) per customer.
"""

import sqlite3
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import (
    classification_report, roc_auc_score,
    precision_recall_curve, confusion_matrix
)

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from data.features import build_customer_features, get_all_feature_cols

MODEL_DIR = Path(__file__).parent / "artifacts"
MODEL_DIR.mkdir(exist_ok=True)
CHURN_MODEL_PATH = MODEL_DIR / "churn_model.pkl"


# ── Training ──────────────────────────────────────────────────────────────────
def train_churn_model(verbose: bool = True) -> dict:
    df = build_customer_features()
    feat_cols = get_all_feature_cols()

    X = df[feat_cols].values
    y = df["is_churned"].values

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    # Three candidate models — pick best by ROC-AUC
    candidates = {
        "logistic": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(C=1.0, max_iter=500, random_state=42))
        ]),
        "random_forest": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", RandomForestClassifier(
                n_estimators=200, max_depth=8,
                min_samples_leaf=3, random_state=42
            ))
        ]),
        "gradient_boost": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", GradientBoostingClassifier(
                n_estimators=150, max_depth=4,
                learning_rate=0.08, random_state=42
            ))
        ]),
    }

    cv  = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    best_name, best_model, best_auc = None, None, 0

    results = {}
    for name, pipe in candidates.items():
        scores = cross_val_score(pipe, X_train, y_train, cv=cv, scoring="roc_auc")
        results[name] = scores.mean()
        if scores.mean() > best_auc:
            best_auc   = scores.mean()
            best_name  = name
            best_model = pipe

    best_model.fit(X_train, y_train)
    y_prob = best_model.predict_proba(X_test)[:, 1]
    y_pred = (y_prob >= 0.5).astype(int)

    test_auc = roc_auc_score(y_test, y_prob)
    report   = classification_report(y_test, y_pred, output_dict=True)
    cm       = confusion_matrix(y_test, y_pred)

    # Feature importances (for RF / GB)
    clf = best_model.named_steps["clf"]
    feat_imp = {}
    if hasattr(clf, "feature_importances_"):
        imp = clf.feature_importances_
        feat_imp = dict(sorted(
            zip(feat_cols, imp), key=lambda x: x[1], reverse=True
        )[:10])

    # Persist
    artifact = {
        "model":       best_model,
        "model_name":  best_name,
        "feature_cols": feat_cols,
        "trained_at":  datetime.now().isoformat(),
        "cv_results":  results,
        "test_auc":    test_auc,
        "report":      report,
        "feat_importance": feat_imp,
    }
    with open(CHURN_MODEL_PATH, "wb") as f:
        pickle.dump(artifact, f)

    if verbose:
        print(f"\n── Churn Model Training ─────────────────────────────")
        print(f"  CV ROC-AUC scores:")
        for n, s in results.items():
            marker = " ← selected" if n == best_name else ""
            print(f"    {n:20s}: {s:.4f}{marker}")
        print(f"  Test ROC-AUC   : {test_auc:.4f}")
        print(f"  Precision (1)  : {report['1']['precision']:.3f}")
        print(f"  Recall    (1)  : {report['1']['recall']:.3f}")
        print(f"  Confusion matrix:\n    TN={cm[0,0]}  FP={cm[0,1]}\n    FN={cm[1,0]}  TP={cm[1,1]}")
        if feat_imp:
            print(f"  Top features   :")
            for f, v in list(feat_imp.items())[:5]:
                print(f"    {f:30s}: {v:.4f}")
        print(f"  Saved → {CHURN_MODEL_PATH}\n")

    return artifact


# ── Inference ─────────────────────────────────────────────────────────────────
def load_churn_model() -> dict:
    if not CHURN_MODEL_PATH.exists():
        train_churn_model(verbose=False)
    with open(CHURN_MODEL_PATH, "rb") as f:
        return pickle.load(f)


def predict_churn(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Returns DataFrame with customer_id and churn_score (0–1).
    If df is None, scores all customers in the database.
    """
    artifact = load_churn_model()
    model    = artifact["model"]
    cols     = artifact["feature_cols"]

    if df is None:
        df = build_customer_features()

    X          = df[cols].values
    scores     = model.predict_proba(X)[:, 1]
    risk_label = pd.cut(scores, bins=[0, 0.3, 0.6, 1.0],
                        labels=["Low", "Medium", "High"])

    return pd.DataFrame({
        "customer_id": df["customer_id"].values,
        "churn_score": scores.round(4),
        "churn_risk":  risk_label,
    })


if __name__ == "__main__":
    art = train_churn_model()
    preds = predict_churn()
    print(preds.head(10).to_string(index=False))
    print(f"\nRisk distribution:\n{preds['churn_risk'].value_counts().to_string()}")
