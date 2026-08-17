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
from utils.db import get_connection, read_sql
from utils.tenancy import get_tenant, DEFAULT_TENANT_ID


def train_all(verbose: bool = True, company_id: str | None = None) -> dict:
    """Train all three models using data from the specified company."""
    cid = company_id or DEFAULT_TENANT_ID

    if verbose:
        print("=" * 54)
        print("  ACIA — ML Model Training")
        print(f"  Company: {cid}")
        print("=" * 54)

    churn_art  = train_churn_model(verbose=verbose, company_id=cid)
    conv_art   = train_conversion_model(verbose=verbose, company_id=cid)
    seg_art    = train_segmentation_model(verbose=verbose, company_id=cid)

    if verbose:
        print("✅ All models trained and saved.\n")

    return {
        "churn":        churn_art,
        "conversion":   conv_art,
        "segmentation": seg_art,
    }


def run_inference_and_store(company_id: str | None = None, verbose: bool = True) -> pd.DataFrame:
    """
    Runs all three models on every customer and writes to ml_predictions table.
    Scoped to the specified company_id.
    """
    cid = company_id or get_tenant().company_id
    df  = build_customer_features(company_id=cid)

    if df.empty:
        if verbose:
            print(f"  ⚠️  No customers found for company_id={cid}")
        return pd.DataFrame()

    churn_preds = predict_churn(df)
    conv_preds  = predict_conversion(df)
    seg_preds   = predict_segments(df)

    # Merge on customer_id
    merged = churn_preds.merge(
        conv_preds[["customer_id", "conversion_score"]],
        on="customer_id", how="left"
    )
    merged = merged.merge(
        seg_preds[["customer_id", "segment"]],
        on="customer_id", how="left"
    )

    # Write to DB — 7 columns: pred_id, customer_id, churn_score, conversion_score, segment, predicted_at, company_id
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for _, row in merged.iterrows():
        rows.append((
            str(uuid.uuid4()),
            str(row["customer_id"]),
            float(row["churn_score"]) if pd.notna(row.get("churn_score")) else 0.5,
            float(row["conversion_score"]) if pd.notna(row.get("conversion_score")) else 0.5,
            str(row["segment"]) if pd.notna(row.get("segment")) else "Unknown",
            now,
            cid,
        ))

    conn = get_connection()
    # Delete existing predictions for this company
    conn.execute("DELETE FROM ml_predictions WHERE company_id = ?", (cid,))
    conn.executemany(
        "INSERT INTO ml_predictions "
        "(pred_id, customer_id, churn_score, conversion_score, segment, predicted_at, company_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows
    )
    conn.commit()
    conn.close()

    if verbose:
        print(f"\n── Inference Results ────────────────────────────────")
        print(f"  Company          : {cid}")
        print(f"  Customers scored : {len(merged)}")
        if len(merged) > 0:
            print(f"  Avg churn score  : {merged['churn_score'].mean():.3f}")
            print(f"  High-risk (>0.6) : {(merged['churn_score'] > 0.6).sum()}")
        print(f"  Written to DB    : {len(rows)} predictions\n")

    return merged


def get_predictions(company_id: str | None = None) -> pd.DataFrame:
    """Load latest predictions from DB."""
    if company_id:
        return read_sql("SELECT * FROM ml_predictions WHERE company_id = ?", params=(company_id,))
    return read_sql("SELECT * FROM ml_predictions")


if __name__ == "__main__":
    train_all()
    preds = run_inference_and_store()
    if not preds.empty:
        print("\nTop 10 highest churn risk:")
        top = preds.sort_values("churn_score", ascending=False).head(10)
        print(top[["customer_id", "churn_score", "segment"]].to_string(index=False))
