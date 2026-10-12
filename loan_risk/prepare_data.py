import pandas as pd
from sklearn.model_selection import train_test_split

# Load dataset
df = pd.read_csv("data.csv")

# Separate features and target
X = df.drop("Bankrupt?", axis=1)
y = df["Bankrupt?"]

# Split into training and testing data
X_train, X_test, y_train, y_test = train_test_split(
    X,
    y,
    test_size=0.20,
    random_state=42,
    stratify=y
)

print("Training features:", X_train.shape)
print("Testing features:", X_test.shape)

print("\nTraining target:")
print(y_train.value_counts())

print("\nTesting target:")
print(y_test.value_counts())