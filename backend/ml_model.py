import os
import re
import joblib
import numpy as np
import pandas as pd
import urllib.parse
from datetime import datetime

from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split, StratifiedKFold
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, classification_report
from sklearn.utils import resample

# Try to import XGBoost and Optuna (optional)
try:
    from xgboost import XGBClassifier
    _HAS_XGB = True
except Exception:
    _HAS_XGB = False

try:
    import optuna
    _HAS_OPTUNA = True
except Exception:
    _HAS_OPTUNA = False


# -------------------------
# Feature extractor
# -------------------------
class URLFeatureExtractor:
    """Extract lexical + structural features from a URL."""

    @staticmethod
    def _shannon_entropy(s: str) -> float:
        if not s:
            return 0.0
        probs = [float(s.count(c)) / len(s) for c in dict.fromkeys(list(s))]
        entropy = -sum([p * np.log2(p) for p in probs if p > 0])
        return float(round(entropy, 6))

    @staticmethod
    def _count_file_ext(path: str) -> int:
        # count occurrences of suspicious extensions
        suspicious_exts = ['.exe', '.zip', '.rar', '.scr', '.apk', '.php', '.jsp', '.aspx']
        return sum(path.lower().count(ext) for ext in suspicious_exts)

    @staticmethod
    def extract_features(url: str) -> dict:
        """Return a dict of features for a single URL."""
        url = url.strip()
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc or ""
        path = parsed.path or ""
        query = parsed.query or ""

        features = {}

        # Basic counts
        features['url_length'] = len(url)
        features['domain_length'] = len(domain)
        features['path_length'] = len(path)
        features['query_length'] = len(query)

        features['count_dots'] = url.count('.')
        features['count_hyphens'] = url.count('-')
        features['count_underscores'] = url.count('_')
        features['count_slashes'] = url.count('/')
        features['count_question_marks'] = url.count('?')
        features['count_equal_signs'] = url.count('=')
        features['count_at_signs'] = url.count('@')
        features['count_ampersands'] = url.count('&')

        features['count_digits'] = sum(c.isdigit() for c in url)
        features['count_letters'] = sum(c.isalpha() for c in url)
        features['count_uppercase'] = sum(c.isupper() for c in url)

        # Domain structure
        features['subdomain_count'] = max(0, domain.count('.') - 1) if domain else 0
        features['tld'] = domain.split('.')[-1].lower() if '.' in domain else ''
        features['tld_length'] = len(features['tld']) if features['tld'] else 0

        # Entropy features (domain + tld)
        features['domain_entropy'] = URLFeatureExtractor._shannon_entropy(domain)
        features['tld_entropy'] = URLFeatureExtractor._shannon_entropy(features['tld'])

        # Ratios
        features['digit_ratio'] = features['count_digits'] / max(1, features['url_length'])
        features['letter_ratio'] = features['count_letters'] / max(1, features['url_length'])
        features['uppercase_ratio'] = features['count_uppercase'] / max(1, features['url_length'])

        # Suspicious indicators
        features['has_ip'] = bool(re.match(r'^\d{1,3}(\.\d{1,3}){3}$', domain)) or bool(re.match(r'^\d+\.\d+\.\d+\.\d+', domain))
        features['has_port'] = bool(parsed.port)
        features['has_query'] = bool(query)
        features['is_https'] = url.lower().startswith('https://')
        features['is_shortened'] = int(any(s in url.lower() for s in ['bit.ly', 'tinyurl.com', 't.co', 'goo.gl', 'ow.ly', 'short.link']))
        features['has_encoded'] = int('%' in url)  # percent-encoded sequences

        # Suspicious keywords (expanded list)
        suspicious_words = [
            'login', 'verify', 'account', 'update', 'secure', 'bank', 'paypal',
            'amazon', 'microsoft', 'apple', 'google', 'facebook', 'download',
            'free', 'win', 'prize', 'click', 'here', 'now', 'confirm', 'validate',
            'signin', 'token', 'session', 'verify', 'webscr'
        ]
        features['suspicious_word_count'] = sum(1 for w in suspicious_words if w in url.lower())

        # File extension / executable indicators
        features['suspicious_ext_count'] = URLFeatureExtractor._count_file_ext(path + query)

        # Misleading tokens in domain (e.g., "https-" in domain)
        features['https_in_domain'] = int('https' in domain.lower())
        features['http_in_domain'] = int('http' in domain.lower())

        # double slash after domain (redirect pattern)
        features['double_slash_in_path'] = int(path.count('//') > 0)

        # token count (hyphen/dot splitting)
        tokens = re.split(r'[.\-_/]', domain + path)
        tokens = [t for t in tokens if t]
        features['token_count'] = len(tokens)

        # suspicious tlds
        suspicious_tlds = {'tk', 'ml', 'ga', 'cf', 'gq', 'cn', 'ru'}
        features['has_suspicious_tld'] = int(features['tld'] in suspicious_tlds)

        # remove tld from returned features (we keep derived flags instead)
        features.pop('tld', None)

        # Ensure all values are primitive types
        for k, v in list(features.items()):
            if isinstance(v, (np.floating, np.integer)):
                features[k] = float(v)
            elif isinstance(v, bool):
                features[k] = int(v)

        return features


# -------------------------
# Classifier
# -------------------------
class URLClassifier:
    """Optimized Hybrid (RandomForest + XGBoost) URL classifier with stacking meta-learner and optional Optuna tuning."""

    def __init__(self, model_path: str = 'models/url_classifier_hybrid_opt.joblib', tune: bool = False):

        self.rf_model = RandomForestClassifier(
            n_estimators=150,
            max_depth=20,
            min_samples_split=4,
            n_jobs=-1,
            random_state=42,
            class_weight='balanced'
        )

        if _HAS_XGB:
            self.xgb_model = XGBClassifier(
                n_estimators=180,
                max_depth=10,
                learning_rate=0.08,
                subsample=0.85,
                colsample_bytree=0.85,
                tree_method='hist',  # faster
                use_label_encoder=False,
                eval_metric='logloss',
                n_jobs=-1,
                random_state=42,
                verbosity=0
            )
        else:
            # Fallback: if XGBoost not installed, use a second RandomForest with different hyperparams
            self.xgb_model = RandomForestClassifier(
                n_estimators=200,
                max_depth=25,
                min_samples_split=6,
                n_jobs=-1,
                random_state=52,
                class_weight='balanced'
            )

        # Meta learner learns optimal weighting of the two base model probabilities
        self.meta_model = LogisticRegression(max_iter=200, solver='lbfgs')

        self.feature_extractor = URLFeatureExtractor()
        self.feature_names = None
        self.scaler = StandardScaler()
        self.model_path = model_path
        self.tune = tune  # whether to run Optuna tuning

    def prepare_data(self, malicious_urls, valid_urls):
        """Prepare features and labels from raw URL lists"""
        data = []
        labels = []

        for url in malicious_urls:
            data.append(self.feature_extractor.extract_features(url))
            labels.append(1)

        for url in valid_urls:
            data.append(self.feature_extractor.extract_features(url))
            labels.append(0)

        df = pd.DataFrame(data).fillna(0)
        self.feature_names = df.columns.tolist()
        X = df.values.astype(float)
        y = np.array(labels).astype(int)
        return X, y

    def _optuna_tune(self, X_train, y_train, n_trials=30, timeout=None):
        """Optuna tuning for XGBoost and RandomForest (limited search for speed)."""
        if not _HAS_OPTUNA:
            print("Optuna not installed; skipping hyperparameter tuning.")
            return

        def objective(trial):
            # sample hyperparameters for xgb
            if _HAS_XGB:
                param = {
                    'n_estimators': trial.suggest_categorical('n_estimators', [100, 150, 180, 220]),
                    'max_depth': trial.suggest_int('max_depth', 6, 12),
                    'learning_rate': trial.suggest_loguniform('lr', 0.03, 0.2),
                    'subsample': trial.suggest_uniform('subsample', 0.6, 0.95),
                    'colsample_bytree': trial.suggest_uniform('colsample_bytree', 0.6, 0.95)
                }
                model = XGBClassifier(
                    use_label_encoder=False, eval_metric='logloss',
                    tree_method='hist', n_jobs=-1, random_state=42, verbosity=0, **param
                )
            else:
                # tune RF as fallback
                param = {
                    'n_estimators': trial.suggest_categorical('n_estimators', [100, 150, 200]),
                    'max_depth': trial.suggest_int('max_depth', 12, 30)
                }
                model = RandomForestClassifier(n_jobs=-1, random_state=42, **param)

            # quick CV (3-fold) on a sample for speed
            skf = StratifiedKFold(n_splits=3, shuffle=True, random_state=42)
            scores = []
            for train_idx, val_idx in skf.split(X_train, y_train):
                model.fit(X_train[train_idx], y_train[train_idx])
                pred = model.predict(X_train[val_idx])
                scores.append(accuracy_score(y_train[val_idx], pred))

            return np.mean(scores)

        study = optuna.create_study(direction='maximize')
        study.optimize(objective, n_trials=n_trials, timeout=timeout)
        print("Optuna best params:", study.best_params)
        # Apply best params (only to xgb_model for simplicity)
        best = study.best_params
        if _HAS_XGB:
            # map back keys for XGB
            xgb_args = {
                'n_estimators': best.get('n_estimators', self.xgb_model.get_params().get('n_estimators')),
                'max_depth': best.get('max_depth', self.xgb_model.get_params().get('max_depth')),
                'learning_rate': best.get('lr', self.xgb_model.get_params().get('learning_rate', 0.08)),
                'subsample': best.get('subsample', self.xgb_model.get_params().get('subsample', 0.85)),
                'colsample_bytree': best.get('colsample_bytree', self.xgb_model.get_params().get('colsample_bytree', 0.85))
            }
            self.xgb_model.set_params(**xgb_args)
        else:
            rf_args = {
                'n_estimators': best.get('n_estimators', 150),
                'max_depth': best.get('max_depth', 20)
            }
            self.xgb_model.set_params(**rf_args)

    def train(self, malicious_urls, valid_urls, tune_first=False, sample_frac=1.0):
        """Train the hybrid model. 
        tune_first: if True and Optuna present, run quick tuning.
        sample_frac: fraction of data to use for faster iteration (0<sample_frac<=1)
        """
        print("Preparing training data...")
        X, y = self.prepare_data(malicious_urls, valid_urls)
        print(f"Total samples before sampling: {len(X)} (malicious: {sum(y)}, safe: {len(y)-sum(y)})")

        # Optional subsample for faster iterations (user can set sample_frac=0.5)
        if sample_frac < 1.0:
            df = pd.DataFrame(X, columns=self.feature_names)
            df['label'] = y
            df_sampled = df.sample(frac=sample_frac, stratify=df['label'], random_state=42)
            y = df_sampled['label'].values
            X = df_sampled.drop(columns=['label']).values
            print(f"Using sampled dataset: {len(X)} samples (frac={sample_frac})")

        # Train/validation split
        X_train, X_val, y_train, y_val = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )

        # Scale numeric features for stable training of meta learner
        self.scaler.fit(X_train)
        X_train_scaled = self.scaler.transform(X_train)
        X_val_scaled = self.scaler.transform(X_val)

        # Optional tuning
        if self.tune or tune_first:
            print("Running Optuna tuning (limited) to find better hyperparameters...")
            self._optuna_tune(X_train, y_train, n_trials=20, timeout=600)

        # Train base learners
        print("Training RandomForest base model...")
        self.rf_model.fit(X_train, y_train)

        print("Training XGBoost base model...")
        self.xgb_model.fit(X_train, y_train)

        # Produce out-of-fold probabilities for meta learner training
        print("Building stacking features for meta-learner...")
        rf_val_proba = self.rf_model.predict_proba(X_val)[:, 1]
        xgb_val_proba = self.xgb_model.predict_proba(X_val)[:, 1]

        stack_X = np.vstack([rf_val_proba, xgb_val_proba]).T

        # Train meta learner (logistic regression) on the validation set probabilities
        print("Training meta-learner (LogisticRegression) to combine base models...")
        self.meta_model.fit(stack_X, y_val)

        # Validate hybrid model on held-out validation set
        hybrid_val_proba = self.meta_model.predict_proba(stack_X)[:, 1]
        hybrid_pred = (hybrid_val_proba >= 0.5).astype(int)
        accuracy = accuracy_score(y_val, hybrid_pred)

        print(f"\n✅ Hybrid Model Validation Accuracy: {accuracy:.4f}")
        print("\nClassification Report:")
        print(classification_report(y_val, hybrid_pred, target_names=['Safe', 'Malicious']))

        # Save model components
        os.makedirs(os.path.dirname(self.model_path) or '.', exist_ok=True)
        joblib.dump({
            'rf_model': self.rf_model,
            'xgb_model': self.xgb_model,
            'meta_model': self.meta_model,
            'scaler': self.scaler,
            'feature_names': self.feature_names,
            'accuracy': accuracy,
            'training_date': datetime.now().isoformat()
        }, self.model_path)

        print(f"\n📦 Hybrid model saved to {self.model_path}")
        return accuracy

    def load_model(self):
        """Load trained model from disk."""
        if os.path.exists(self.model_path):
            model_data = joblib.load(self.model_path)
            self.rf_model = model_data['rf_model']
            self.xgb_model = model_data['xgb_model']
            self.meta_model = model_data.get('meta_model', self.meta_model)
            self.scaler = model_data.get('scaler', self.scaler)
            self.feature_names = model_data.get('feature_names', self.feature_names)
            return True
        return False

    def predict(self, url: str):
        """Predict single URL and return explanation + probabilities."""
        if not self.feature_names and not self.load_model():
            return {'prediction': 'unknown', 'confidence': 0.0, 'reason': 'Model not trained'}

        features = self.feature_extractor.extract_features(url)
        vector = [features.get(f, 0) for f in self.feature_names]
        vector_arr = np.array(vector).reshape(1, -1)
        vector_scaled = self.scaler.transform(vector_arr)

        # Base probabilities
        rf_prob = float(self.rf_model.predict_proba(vector_arr)[0][1])
        xgb_prob = float(self.xgb_model.predict_proba(vector_arr)[0][1])

        # Meta model combines base probabilities
        meta_input = np.array([[rf_prob, xgb_prob]])
        final_prob = float(self.meta_model.predict_proba(meta_input)[0][1])
        prediction = int(final_prob >= 0.5)

        # Build reason/explanation (human-friendly)
        reasons = []
        if features.get('has_suspicious_tld', 0):
            reasons.append("Suspicious TLD")
        if features.get('suspicious_word_count', 0) > 1:
            reasons.append("Suspicious keywords")
        if features.get('url_length', 0) > 100:
            reasons.append("Long URL")
        if features.get('has_ip', 0):
            reasons.append("IP in domain")
        if features.get('is_shortened', 0):
            reasons.append("Shortened URL")
        if not features.get('is_https', 1):
            reasons.append("Non-HTTPS")
        if features.get('suspicious_ext_count', 0) > 0:
            reasons.append("Executable / suspicious extension")

        reason_text = '; '.join(reasons) if reasons else "No strong lexical indicators; model signals moderate risk" if final_prob > 0.5 else "URL appears safe based on lexical signals and models"

        return {
            'prediction': 'malicious' if prediction == 1 else 'safe',
            'confidence': float(final_prob if prediction == 1 else 1 - final_prob),
            'malicious_probability': float(final_prob),
            'safe_probability': float(1 - final_prob),
            'explain': {
                'rf_prob': rf_prob,
                'xgb_prob': xgb_prob,
                'meta_prob': final_prob,
                'top_reasons': reasons[:5]
            },
            'reason': reason_text
        }


# -------------------------
# Single global classifier
# -------------------------
classifier = URLClassifier()