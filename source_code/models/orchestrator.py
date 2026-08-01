"""
ACIA — ML Orchestrator
Trains all models, runs inference on all customers,
and writes predictions to the ml_predictions table.
"""

import uuid
import pandas as pd
from pathlib import Path
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from data.features import build_customer_features
from models.churn import train_churn_model, predict_churn
from models.conversion import train_conversion_model, predict_conversion
from models.segmentation import train_segmentation_model, predict_segments

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_connection, read_sql
from utils.tenancy import get_tenant, DEFAULT_TENANT_ID


def train_all(verbose: bool = True) -> dict:
    """Train all three models and return their artifacts."""
    print("=" * 54)
    print("  ACIA — ML Model Training")
    print("=" * 54)

    churn_art  = train_churn_model(verbose=verbose)
    conv_art   = train_conversion_model(verbose=verbose)
    seg_art    = train_segmentation_model(verbose=verbose)

    print("✅ All models trained and saved.\n")
    return {
        "churn":      churn_art,
        "conversion": conv_art,
        "segmentation": seg_art,
    }


def run_inference_and_store(company_id: str | None = None, verbose: bool = True) -> pd.DataFrame:
    """
    Runs all three models on every customer and writes to ml_predictions table.
    Scoped to the current tenant. Returns merged predictions DataFrame.
    """
    cid = company_id or get_tenant().company_id
    df  = build_customer_features(company_id=cid)

    churn_preds = predict_churn(df)
    conv_preds  = predict_conversion(df)
    seg_preds   = predict_segments(df)

    # Merge on customer_id
    merged = churn_preds.merge(conv_preds[["customer_id","conversion_score","conversion_tier"]],
                               on="customer_id", how="left")
    merged = merged.merge(seg_preds[["customer_id","segment","segment_confidence"]],
                          on="customer_id", how="left")

    # Estimate LTV: MRR * predicted tenure (inverse churn) * 12
    cust_base = df[["customer_id","mrr","tenure_days"]].copy()
    merged = merged.merge(cust_base, on="customer_id", how="left")
    merged["ltv_estimate"] = (
        merged["mrr"] * (1 - merged["churn_score"]) * 24
    ).round(2)

    # Write to DB
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for _, row in merged.iterrows():
        rows.append((
            str(uuid.uuid4()),
            row["customer_id"],
            float(row["churn_score"]),
            float(row["conversion_score"]) if pd.notna(row["conversion_score"]) else 0.5,
            str(row["segment"]) if pd.notna(row["segment"]) else "Unknown",
            float(row["ltv_estimate"]),
            now,
            cid,
        ))

    conn = get_connection()
    from utils.db import DB_ENGINE
    conn.execute("DELETE FROM ml_predictions")
    if DB_ENGINE == "postgresql":
        conn.executemany(
            "INSERT INTO ml_predictions VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            rows
        )
    else:
        conn.executemany(
            "INSERT INTO ml_predictions VALUES (?,?,?,?,?,?,?,?)",
            rows
        )
    conn.commit()
    conn.close()

    if verbose:
        print(f"\n── Inference Results ────────────────────────────────")
        print(f"  Customers scored  : {len(merged)}")
        print(f"  Avg churn score   : {merged['churn_score'].mean():.3f}")
        print(f"  High-risk (>0.6)  : {(merged['churn_score'] > 0.6).sum()}")
        print(f"  Hot conversions   : {(merged['conversion_tier'] == 'Hot').sum()}")
        print(f"  Segment counts    :")
        for seg, cnt in merged["segment"].value_counts().items():
            print(f"    {seg:15s}: {cnt}")
        print(f"  Total LTV est.    : ${merged['ltv_estimate'].sum():,.0f}")
        print(f"  Written to DB     : ml_predictions ({len(rows)} rows)\n")

    return merged


def get_predictions() -> pd.DataFrame:
    """Load latest predictions from DB."""
    df   = read_sql("SELECT * FROM ml_predictions")
    return df


if __name__ == "__main__":
    train_all()
    preds = run_inference_and_store()
    print("\nTop 10 highest churn risk:")
    top = preds.sort_values("churn_score", ascending=False).head(10)
    print(top[["customer_id","churn_score","churn_risk","segment","ltv_estimate"]].to_string(index=False))
    print("\nTop 10 hottest conversion opportunities:")
    hot = preds[preds["conversion_tier"] == "Hot"].sort_values("conversion_score", ascending=False).head(10)
    print(hot[["customer_id","conversion_score","conversion_tier","segment","ltv_estimate"]].to_string(index=False))
