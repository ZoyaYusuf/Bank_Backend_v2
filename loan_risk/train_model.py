import pandas as pd
import numpy as np
import tensorflow as tf

from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score
from sklearn.metrics import precision_score, recall_score, f1_score
from sklearn.utils.class_weight import compute_class_weight

# 1. Load data
df = pd.read_csv("data.csv")

# 2. Separate features and target
X = df.drop("Bankrupt?", axis=1)
y = df["Bankrupt?"]

# 3. Train/test split
X_train, X_test, y_train, y_test = train_test_split(
    X,
    y,
    test_size=0.20,
    random_state=42,
    stratify=y
)

# 4. Scale features
scaler = StandardScaler()

X_train = scaler.fit_transform(X_train)
X_test = scaler.transform(X_test)

# 5. Create neural network
model = tf.keras.Sequential([
    tf.keras.layers.Input(shape=(95,)),

    tf.keras.layers.Dense(64, activation="relu"),
    tf.keras.layers.Dropout(0.3),

    tf.keras.layers.Dense(32, activation="relu"),
    tf.keras.layers.Dropout(0.3),

    tf.keras.layers.Dense(1, activation="sigmoid")
])

# 6. Configure training
model.compile(
    optimizer="adam",
    loss="binary_crossentropy",
    metrics=["accuracy"]
)

# Calculate class weights
class_weights = compute_class_weight(
    class_weight="balanced",
    classes=np.array([0, 1]),
    y=y_train
)

class_weight_dict = {
    0: class_weights[0],
    1: class_weights[1]
}

# 7. Train
model.fit(
    X_train,
    y_train,
    epochs=30,
    batch_size=32,
    validation_split=0.2,
    class_weight=class_weight_dict
)

# 8. Test
loss, accuracy = model.evaluate(X_test, y_test)

print("\nTest accuracy:", accuracy)

# Get prediction probabilities
y_probability = model.predict(X_test)

for threshold in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7]:

    y_prediction = (y_probability >= threshold).astype(int).ravel()

    precision = precision_score(y_test, y_prediction)
    recall = recall_score(y_test, y_prediction)
    f1 = f1_score(y_test, y_prediction)

    print(f"\nThreshold: {threshold}")
    print(f"Precision: {precision:.2f}")
    print(f"Recall:    {recall:.2f}")
    print(f"F1 Score:  {f1:.2f}")

print("\nConfusion Matrix:")
print(confusion_matrix(y_test, y_prediction))

print("\nClassification Report:")
print(classification_report(y_test, y_prediction))

print("\nROC-AUC:")
print(roc_auc_score(y_test, y_probability))

print("Class weights:", class_weight_dict)