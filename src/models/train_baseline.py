"""Train a baseline XGBoost model for O/U 2.5 market using generated features.

Produces a model artifact (`models/baseline_xgb.json`) and a small evaluation
report saved to `models/metrics.txt`.
"""
import pandas as pd
import xgboost as xgb
from pathlib import Path
import logging
from uuid import UUID, uuid4
from sklearn.metrics import log_loss, brier_score_loss, roc_auc_score

from src.db.ingestion import persist_model_version
from src.db.session import session_scope


def prepare_training_data(features_path: Path, labels_path: Path):
    """Merge and chronologically sort features and labels as one frame."""
    feats = pd.read_parquet(features_path)
    labels = pd.read_csv(labels_path)
    lab = labels[labels["market"] == "O/U_2.5"][["match_id", "label_value"]]
    df = feats.merge(lab, on="match_id", how="inner")
    return df.sort_values(["match_date_utc", "match_id"], kind="mergesort").reset_index(drop=True)


def register_model_version(
    database_url: str | None,
    *,
    model_id: UUID,
    model_version: str,
    feature_version: str,
    training_run_id: UUID,
    features_path: Path,
    labels_path: Path,
    artifact_path: Path,
    configuration_reference: dict,
):
    with session_scope(database_url) as session:
        return persist_model_version(
            session,
            model_id=model_id,
            model_version=model_version,
            model_type="xgboost",
            feature_version=feature_version,
            training_run_id=training_run_id,
            training_data_reference={
                "features_path": str(features_path.resolve()),
                "labels_path": str(labels_path.resolve()),
            },
            artifact_reference=str(artifact_path.resolve()),
            configuration_reference=configuration_reference,
            evaluation_status="unevaluated",
        )


def train(
    features_path: Path,
    labels_path: Path,
    out_dir: Path,
    *,
    register_model: bool = False,
    database_url: str | None = None,
    model_id: UUID | None = None,
    model_version: str | None = None,
    feature_version: str | None = None,
):
    if register_model and (model_id is None or not model_version or not feature_version):
        raise ValueError("model registration requires model_id, model_version, and feature_version")

    df = prepare_training_data(features_path, labels_path)
    # Simple features
    X = df[["match_id", "match_date_utc", "home_elo", "away_elo", "home_form_last3", "away_form_last3"]].copy()
    y = df["label_value"].astype(int)

    # Chronological split: sort by date and take the last 20% as test set
    X = X.reset_index(drop=True)
    y = y.reset_index(drop=True)
    test_frac = 0.2
    split_at = int(len(X) * (1.0 - test_frac))
    X_train = X.iloc[:split_at].drop(columns=["match_id", "match_date_utc"]).fillna(0)
    X_test = X.iloc[split_at:].drop(columns=["match_id", "match_date_utc"]).fillna(0)
    y_train = y.iloc[:split_at]
    y_test = y.iloc[split_at:]

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dtest = xgb.DMatrix(X_test, label=y_test)

    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "eta": 0.1,
        "max_depth": 4,
    }

    bst = xgb.train(params, dtrain, num_boost_round=100, evals=[(dtest, "test")], early_stopping_rounds=10)

    best_iteration = getattr(bst, "best_iteration", None)
    iteration_range = (0, best_iteration + 1) if best_iteration is not None else (0, 0)
    preds = bst.predict(dtest, iteration_range=iteration_range)
    ll = log_loss(y_test, preds)
    bs = brier_score_loss(y_test, preds)
    try:
        auc = roc_auc_score(y_test, preds)
    except Exception:
        auc = float("nan")

    # Bookmaker baseline: try to read football-data.co.uk CSVs and evaluate Over 2.5 odds if available
    try:
        from pathlib import Path as _P
        fd_dir = _P("data/raw/football-data-co-uk")
        if fd_dir.exists():
            import glob
            fpaths = list(fd_dir.glob("*.csv"))
            if fpaths:
                fd = pd.concat([pd.read_csv(p, dayfirst=True) for p in fpaths], ignore_index=True)
                # normalise column names
                fd_cols = {c.lower(): c for c in fd.columns}
                # expected columns: Date, HomeTeam, AwayTeam, B365>2.5, B365<2.5, Avg>2.5, Avg<2.5
                # find over/under odds column
                over_col = None
                under_col = None
                for k in fd.columns:
                    if k.lower().replace(" ", "") in ("b365>2.5", "b365>2.5"):
                        over_col = k
                    if k.lower().replace(" ", "") in ("b365<2.5", "b365<2.5"):
                        under_col = k
                if over_col is None or under_col is None:
                    # try avg columns
                    for k in fd.columns:
                        if ">2.5" in k and "avg" in k.lower():
                            over_col = over_col or k
                        if "<2.5" in k and "avg" in k.lower():
                            under_col = under_col or k

                if over_col and under_col:
                    # prepare fd mapping: parse date and canonicalize team names
                    fd["Date"] = pd.to_datetime(fd["Date"], dayfirst=True, errors="coerce")
                    # canonicalization helper (import local function)
                    try:
                        from ..normalization.team_aliases import to_canonical
                    except Exception:
                        def to_canonical(x):
                            return x

                    fd["home_team_canon"] = fd["HomeTeam"].apply(to_canonical)
                    fd["away_team_canon"] = fd["AwayTeam"].apply(to_canonical)

                    # build mapping from matches (by date, home, away) to match_id
                    matches = pd.read_csv("data/processed/matches_canonical.csv", parse_dates=["match_date_utc"]) 
                    matches["d_date"] = pd.to_datetime(matches["match_date_utc"]).dt.normalize()
                    fd["d_date"] = fd["Date"].dt.normalize()
                    merge = fd.merge(matches, left_on=["d_date", "home_team_canon", "away_team_canon"], right_on=["d_date", "home_team", "away_team"], how="inner")
                    if not merge.empty:
                        # compute implied prob for Over 2.5
                        def safe_inv(x):
                            try:
                                x = float(x)
                                return 1.0 / x if x > 0 else None
                            except Exception:
                                return None

                        merge["imp_over"] = merge[over_col].apply(safe_inv)
                        merge["imp_under"] = merge[under_col].apply(safe_inv)
                        merge = merge.dropna(subset=["imp_over", "imp_under"])
                        if not merge.empty:
                            merge["sum_imp"] = merge["imp_over"] + merge["imp_under"]
                            merge["book_over_prob"] = merge["imp_over"] / merge["sum_imp"]
                            # align with our test set match_ids
                            test_match_ids = X.iloc[split_at:]["match_id"].tolist()
                            bk = merge[merge["match_id"].isin(test_match_ids)][["match_id", "book_over_prob"]].set_index("match_id")
                            if not bk.empty:
                                # get y_test aligned
                                y_test_series = y_test.reset_index(drop=True)
                                X_test_ids = X.iloc[split_at:]["match_id"].reset_index(drop=True)
                                prob_list = X_test_ids.map(bk["book_over_prob"]).to_list()
                                # filter only where bookmaker prob exists
                                import numpy as _np
                                idx = [i for i, v in enumerate(prob_list) if not (_np.isnan(v) or v is None)]
                                if idx:
                                    y_test_arr = y_test_series.iloc[idx]
                                    probs = _np.array([prob_list[i] for i in idx], dtype=float)
                                    ll_book = log_loss(y_test_arr, probs)
                                    try:
                                        auc_book = roc_auc_score(y_test_arr, probs)
                                    except Exception:
                                        auc_book = float("nan")
                                else:
                                    ll_book = float("nan")
                                    auc_book = float("nan")
                            else:
                                ll_book = float("nan")
                                auc_book = float("nan")
                        else:
                            ll_book = float("nan")
                            auc_book = float("nan")
                    else:
                        ll_book = float("nan")
                        auc_book = float("nan")
                else:
                    ll_book = float("nan")
                    auc_book = float("nan")
            else:
                ll_book = float("nan")
                auc_book = float("nan")
        else:
            ll_book = float("nan")
            auc_book = float("nan")
    except Exception:
        ll_book = float("nan")
        auc_book = float("nan")

    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "baseline_xgb.json"
    bst.save_model(str(model_path))
    metrics_path = out_dir / "metrics.txt"
    with metrics_path.open("w") as f:
        f.write(f"log_loss: {ll}\n")
        f.write(f"brier_score: {bs}\n")
        f.write(f"roc_auc: {auc}\n")
        f.write(f"book_log_loss: {ll_book}\n")
        f.write(f"book_roc_auc: {auc_book}\n")

    if register_model:
        register_model_version(
            database_url,
            model_id=model_id,
            model_version=model_version,
            feature_version=feature_version,
            training_run_id=uuid4(),
            features_path=features_path,
            labels_path=labels_path,
            artifact_path=model_path,
            configuration_reference={
                **params,
                "num_boost_round": 100,
                "early_stopping_rounds": 10,
                "best_iteration": int(best_iteration) if best_iteration is not None else None,
                "missing_value_strategy": "replace with zero",
            },
        )

    logging.info("Trained model saved to %s; metrics saved to %s", model_path, metrics_path)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--features", default="data/processed/features_batch.parquet")
    p.add_argument("--labels", default="data/processed/labels.csv")
    p.add_argument("--out", default="models")
    p.add_argument("--register-model", action="store_true")
    p.add_argument("--database-url", default=None)
    p.add_argument("--model-id", type=UUID, default=None)
    p.add_argument("--model-version", default=None)
    p.add_argument("--feature-version", default=None)
    args = p.parse_args()
    if not args.register_model and any(
        value is not None for value in (args.database_url, args.model_id, args.model_version, args.feature_version)
    ):
        p.error("--database-url and model metadata require --register-model")
    try:
        train(
            Path(args.features),
            Path(args.labels),
            Path(args.out),
            register_model=args.register_model,
            database_url=args.database_url,
            model_id=args.model_id,
            model_version=args.model_version,
            feature_version=args.feature_version,
        )
    except ValueError as error:
        p.error(str(error))
