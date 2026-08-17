"""
ACIA — Customer Segmentation Model
RFM (Recency, Frequency, Monetary) + KMeans clustering.
Assigns each customer to a named segment used by the decision engine.
"""

import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

from sklearn.cluster import KMeans
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import silhouette_score

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from data.features import build_customer_features

MODEL_DIR   = Path(__file__).parent / "artifacts"
MODEL_DIR.mkdir(exist_ok=True)
SEG_MODEL_PATH = MODEL_DIR / "segmentation_model.pkl"

# Segment name mapping — assigned after cluster profiling
SEGMENT_NAMES = {
    0: "Champion",      # High RFM, high spend, recent activity
    1: "Loyal",         # Consistent, moderate spend, long tenure
    2: "At-Risk",       # Declining engagement, medium spend
    3: "Lost",          # No recent activity, potential churn
    4: "Prospect",      # New, low spend, high potential
}

N_CLUSTERS = 5


def _build_rfm(df: pd.DataFrame) -> pd.DataFrame:
    """Extract RFM signals from feature matrix."""
    rfm = pd.DataFrame({
        "customer_id": df["customer_id"],
        # Recency: invert so higher = more recent
        "recency":     100 - df["days_since_last_event"].clip(upper=100),
        # Frequency: total events + transactions
        "frequency":   df["event_count_total"] + df["txn_count"],
        # Monetary: MRR + total spend
        "monetary":    df["mrr"] + df["total_spend"].clip(lower=0),
        # Bonus signals
        "engagement":  df["engagement_index"],
        "health":      df["health_score"],
        "tenure":      df["tenure_days"],
    })
    return rfm


def train_segmentation_model(verbose: bool = True, company_id: str | None = None) -> dict:
    df  = build_customer_features()
    rfm = _build_rfm(df)

    feature_cols = ["recency", "frequency", "monetary", "engagement", "health", "tenure"]
    X_raw = rfm[feature_cols].values

    scaler = MinMaxScaler()
    X = scaler.fit_transform(X_raw)

    # Fit KMeans
    kmeans = KMeans(n_clusters=N_CLUSTERS, random_state=42, n_init=20, max_iter=300)
    labels = kmeans.fit_predict(X)

    sil_score = silhouette_score(X, labels)

    # Profile each cluster to assign human names
    rfm["cluster"] = labels
    profile = rfm.groupby("cluster")[feature_cols].mean()

    # Sort clusters by combined score to assign names deterministically
    profile["score"] = (
        profile["recency"]   * 0.25 +
        profile["monetary"]  * 0.30 +
        profile["frequency"] * 0.20 +
        profile["engagement"]* 0.15 +
        profile["health"]    * 0.10
    )
    rank_order = profile["score"].rank(ascending=False).astype(int)

    # Map rank to segment name
    # rank 1 = Champion, 2 = Loyal, 3 = At-Risk, 4 = Prospect, 5 = Lost
    rank_to_segment = {1: "Champion", 2: "Loyal", 3: "At-Risk", 4: "Prospect", 5: "Lost"}
    cluster_to_segment = {cluster: rank_to_segment[rank] for cluster, rank in rank_order.items()}

    artifact = {
        "kmeans":            kmeans,
        "scaler":            scaler,
        "feature_cols":      feature_cols,
        "cluster_to_segment": cluster_to_segment,
        "silhouette_score":  sil_score,
        "cluster_profiles":  profile.to_dict(),
        "trained_at":        datetime.now().isoformat(),
    }
    with open(SEG_MODEL_PATH, "wb") as f:
        pickle.dump(artifact, f)

    if verbose:
        print(f"\n── Segmentation Model Training ──────────────────────")
        print(f"  Clusters       : {N_CLUSTERS}")
        print(f"  Silhouette     : {sil_score:.4f}")
        print(f"  Cluster → Segment mapping:")
        for cluster, seg in sorted(cluster_to_segment.items()):
            n = (labels == cluster).sum()
            print(f"    Cluster {cluster} → {seg:12s}  (n={n})")
        print(f"  Cluster profiles (mean RFM):")
        print(profile[feature_cols].round(1).to_string())
        print(f"  Saved → {SEG_MODEL_PATH}\n")

    return artifact


def load_segmentation_model() -> dict:
    if not SEG_MODEL_PATH.exists():
        train_segmentation_model(verbose=False)
    with open(SEG_MODEL_PATH, "rb") as f:
        return pickle.load(f)


def predict_segments(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Returns customer_id + segment name for every customer.
    """
    artifact   = load_segmentation_model()
    kmeans     = artifact["kmeans"]
    scaler     = artifact["scaler"]
    feat_cols  = artifact["feature_cols"]
    c2s        = artifact["cluster_to_segment"]

    if df is None:
        df = build_customer_features(company_id=company_id)

    rfm = _build_rfm(df)
    X   = scaler.transform(rfm[feat_cols].values)
    clusters  = kmeans.predict(X)
    segments  = [c2s[c] for c in clusters]

    # Distance to cluster centre → confidence
    distances = kmeans.transform(X)
    min_dist  = distances[np.arange(len(clusters)), clusters]
    max_dist  = distances.max(axis=1) + 1e-9
    confidence = (1 - min_dist / max_dist).round(3)

    return pd.DataFrame({
        "customer_id":         df["customer_id"].values,
        "segment":             segments,
        "segment_confidence":  confidence,
        "cluster_id":          clusters,
    })


if __name__ == "__main__":
    art  = train_segmentation_model()
    preds = predict_segments()
    print(preds.head(10).to_string(index=False))
    print(f"\nSegment distribution:\n{preds['segment'].value_counts().to_string()}")
